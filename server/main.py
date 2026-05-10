import json
import os
import random
from pathlib import Path
from urllib.parse import urlparse, quote

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient

from bridge import openai_realtime_url, openai_headers
from use_cases import CASES, get_case

app = FastAPI(title="PassportCard Pay – Service Agent Demo")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CONTACTS_PATH = Path(__file__).resolve().parent / "contacts.json"
CALL_STATE: dict[str, dict] = {}  # callSid -> {customer_name, case}
PENDING_BY_NAME: dict[str, dict] = {}  # name -> {customer_name, case} (used until callSid is known)


def load_contacts() -> list[dict]:
    return json.loads(CONTACTS_PATH.read_text(encoding="utf-8"))


def twilio() -> TwilioClient:
    return TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


@app.get("/api/contacts")
def list_contacts():
    return load_contacts()


@app.get("/api/cases")
def list_cases():
    return [{"id": c["id"], "rc": c["rc"], "rc_description": c["rc_description"], "failure_reason": c["failure_reason"]} for c in CASES]


class CallRequest(BaseModel):
    contactId: str
    channel: str = "voice"  # "voice" | "whatsapp"


def _case_summary(case: dict) -> dict:
    return {
        "id": case["id"],
        "rc": case["rc"],
        "rc_description": case["rc_description"],
        "failure_reason": case["failure_reason"],
    }


@app.post("/api/call")
def initiate_call(req: CallRequest):
    contact = next((c for c in load_contacts() if c["id"] == req.contactId), None)
    if not contact:
        raise HTTPException(404, "Unknown contact")
    if not contact.get("phone"):
        raise HTTPException(400, f"No phone number set for {contact['name']} in contacts.json")

    case = random.choice(CASES)
    channel = (req.channel or "voice").lower()

    if channel == "whatsapp":
        wa_from = os.environ.get("TWILIO_WHATSAPP_FROM")
        if not wa_from:
            raise HTTPException(500, "TWILIO_WHATSAPP_FROM not set in .env")
        body = (case.get("script") or "").replace("[שם הלקוח]", contact["name"]).strip()
        if not body:
            raise HTTPException(500, f"Use case '{case['failure_reason']}' has no WhatsApp script")
        msg = twilio().messages.create(
            from_=wa_from,
            to=f"whatsapp:{contact['phone']}",
            body=body,
        )
        return {
            "channel": "whatsapp",
            "messageSid": msg.sid,
            "contact": contact,
            "case": _case_summary(case),
            "preview": body,
        }

    # Voice
    public_base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    twiml_url = f"{public_base}/twiml?case={quote(case['id'])}&name={quote(contact['name'])}"
    call = twilio().calls.create(
        to=contact["phone"],
        from_=os.environ["TWILIO_FROM_NUMBER"],
        url=twiml_url,
    )
    CALL_STATE[call.sid] = {"customer_name": contact["name"], "case": case}
    return {
        "channel": "voice",
        "callSid": call.sid,
        "contact": contact,
        "case": _case_summary(case),
    }


@app.api_route("/twiml", methods=["GET", "POST"])
def twiml(request: Request, case: str, name: str):
    public_base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    host = urlparse(public_base).netloc
    ws_url = f"wss://{host}/ws"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}">
      <Parameter name="case" value="{case}"/>
      <Parameter name="name" value="{name}"/>
    </Stream>
  </Connect>
</Response>"""
    return Response(content=xml, media_type="application/xml")


@app.websocket("/ws")
async def ws_bridge(twilio_ws: WebSocket):
    await twilio_ws.accept()
    customer_name = "לקוח"
    case = None
    # Read messages until we see the start event with custom params.
    try:
        first_msg = await twilio_ws.receive_text()
        first = json.loads(first_msg)
        # Twilio may send "connected" first; loop until "start"
        while first.get("event") != "start":
            first_msg = await twilio_ws.receive_text()
            first = json.loads(first_msg)
        raw_params = first["start"].get("customParameters", {})
        if isinstance(raw_params, dict):
            params = raw_params
        else:
            params = {p["name"]: p["value"] for p in raw_params}
        customer_name = params.get("name", customer_name)
        case_id = params.get("case", "")
        case = get_case(case_id) or (CASES[0] if CASES else None)
        stream_sid = first["start"]["streamSid"]
        call_sid = first["start"].get("callSid")
        if call_sid and call_sid in CALL_STATE:
            ctx = CALL_STATE[call_sid]
        else:
            ctx = {"customer_name": customer_name, "case": case}

        # Re-inject the start event into a queue-like flow: bridge expects to read media events,
        # so we pass an already-consumed-start scenario by giving it a wrapped websocket.
        # Simpler: directly run the bridge but also pass the streamSid via a side channel.
        await _run(twilio_ws, ctx, stream_sid)
    except WebSocketDisconnect:
        return


async def _run(twilio_ws: WebSocket, ctx: dict, stream_sid: str):
    """Twilio <-> OpenAI Realtime bridge. We've already consumed Twilio's `start` event."""
    import asyncio, json, os
    import websockets
    from prompt import build_instructions

    voice = os.environ.get("OPENAI_REALTIME_VOICE", "marin")
    model = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2")
    instructions = build_instructions(ctx["customer_name"], ctx["case"])
    last_assistant_item: str | None = None

    async with websockets.connect(
        openai_realtime_url(),
        additional_headers=openai_headers(),
        max_size=None,
    ) as oai_ws:
        # GA session shape: nested audio.input/output, MIME-style format strings, output_modalities.
        await oai_ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": model,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                            "create_response": True,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcmu"},
                        "voice": voice,
                    },
                },
                "instructions": instructions,
            },
        }))
        # Trigger the agent's opening line.
        await oai_ws.send(json.dumps({
            "type": "response.create",
            "response": {"instructions": "פתח את השיחה עכשיו לפי ההנחיות."},
        }))

        async def twilio_to_openai():
            try:
                while True:
                    msg = await twilio_ws.receive_text()
                    data = json.loads(msg)
                    ev = data.get("event")
                    if ev == "media":
                        await oai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"],
                        }))
                    elif ev == "stop":
                        break
            except WebSocketDisconnect:
                pass

        async def openai_to_twilio():
            nonlocal last_assistant_item
            async for raw in oai_ws:
                ev = json.loads(raw)
                t = ev.get("type")
                # GA renamed audio delta to response.output_audio.delta. Accept legacy too.
                if t in ("response.output_audio.delta", "response.audio.delta"):
                    last_assistant_item = ev.get("item_id", last_assistant_item)
                    await twilio_ws.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": ev["delta"]},
                    }))
                elif t == "input_audio_buffer.speech_started":
                    # Barge-in: cancel model + clear Twilio's outbound buffer.
                    if last_assistant_item:
                        try:
                            await oai_ws.send(json.dumps({"type": "response.cancel"}))
                            await oai_ws.send(json.dumps({
                                "type": "conversation.item.truncate",
                                "item_id": last_assistant_item,
                                "content_index": 0,
                                "audio_end_ms": 0,
                            }))
                        except Exception:
                            pass
                        last_assistant_item = None
                    await twilio_ws.send_text(json.dumps({
                        "event": "clear",
                        "streamSid": stream_sid,
                    }))
                elif t == "error":
                    print("OpenAI realtime error:", ev)

        await asyncio.gather(twilio_to_openai(), openai_to_twilio(), return_exceptions=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
