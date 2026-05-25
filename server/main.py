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
from tts_soniox import SonioxConnection, SonioxStream
from use_cases import CASES, get_case

TtsProvider = str  # "openai" | "soniox"

app = FastAPI(title="PassportCard Pay – Service Agent Demo")

_web_origin = os.environ.get("WEB_ORIGIN", "").strip()
_allowed_origins = [o.strip() for o in _web_origin.split(",") if o.strip()] if _web_origin else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
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
    ttsProvider: str = "openai"  # "openai" | "soniox" (voice only)


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
    tts = (req.ttsProvider or "openai").lower()
    if tts not in ("openai", "soniox"):
        raise HTTPException(400, f"Unknown ttsProvider '{tts}'")
    if tts == "soniox" and not os.environ.get("SONIOX_API_KEY"):
        raise HTTPException(400, "SONIOX_API_KEY not set in .env — cannot use Soniox TTS")

    public_base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    twiml_url = (
        f"{public_base}/twiml?case={quote(case['id'])}"
        f"&name={quote(contact['name'])}&tts={quote(tts)}"
    )
    call = twilio().calls.create(
        to=contact["phone"],
        from_=os.environ["TWILIO_FROM_NUMBER"],
        url=twiml_url,
    )
    CALL_STATE[call.sid] = {"customer_name": contact["name"], "case": case, "tts": tts}
    return {
        "channel": "voice",
        "callSid": call.sid,
        "contact": contact,
        "case": _case_summary(case),
        "tts": tts,
    }


@app.api_route("/twiml", methods=["GET", "POST"])
def twiml(request: Request, case: str, name: str, tts: str = "openai"):
    public_base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    host = urlparse(public_base).netloc
    ws_url = f"wss://{host}/ws"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}">
      <Parameter name="case" value="{case}"/>
      <Parameter name="name" value="{name}"/>
      <Parameter name="tts" value="{tts}"/>
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
        tts_param = (params.get("tts") or "openai").lower()
        stream_sid = first["start"]["streamSid"]
        call_sid = first["start"].get("callSid")
        if call_sid and call_sid in CALL_STATE:
            ctx = CALL_STATE[call_sid]
        else:
            ctx = {"customer_name": customer_name, "case": case, "tts": tts_param}
        ctx.setdefault("tts", tts_param)

        if ctx.get("tts") == "soniox":
            await _run_openai_text_soniox(twilio_ws, ctx, stream_sid)
        else:
            await _run_openai_audio(twilio_ws, ctx, stream_sid)
    except WebSocketDisconnect:
        return


async def _run_openai_audio(twilio_ws: WebSocket, ctx: dict, stream_sid: str):
    """Twilio <-> OpenAI Realtime bridge (audio in, audio out)."""
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
        # Trigger the agent's opening line. No `response.instructions` override —
        # that field REPLACES the session prompt for the response, which would leave
        # the cold-open with no system context and produce off-prompt filler.
        await oai_ws.send(json.dumps({"type": "response.create"}))

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


async def _run_openai_text_soniox(twilio_ws: WebSocket, ctx: dict, stream_sid: str):
    """Twilio <-> OpenAI Realtime (text-only) <-> Soniox TTS bridge.

    OpenAI does STT + LLM + server_vad turn-taking and emits text deltas.
    We pipe those into a per-turn Soniox WebSocket which streams pcm_mulaw @ 8 kHz
    base64 chunks back; we forward them to Twilio as media frames.
    """
    import asyncio, json, os
    import websockets
    from prompt import build_instructions

    voice = os.environ.get("OPENAI_REALTIME_VOICE", "marin")
    model = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2")
    instructions = build_instructions(ctx["customer_name"], ctx["case"])

    # Per-call Soniox state. A single SonioxConnection hosts every assistant
    # turn (Soniox supports up to 5 concurrent streams on one WS and runs
    # a keepalive heartbeat). Each turn is a SonioxStream.
    current_stream: SonioxStream | None = None
    current_audio_task: asyncio.Task | None = None
    # Last OpenAI assistant item — needed to truncate on barge-in.
    last_assistant_item: str | None = None

    async def cancel_current_stream():
        """Barge-in: cancel the current per-turn stream; WS stays open."""
        nonlocal current_stream, current_audio_task
        if current_stream is None:
            return
        s, t = current_stream, current_audio_task
        current_stream = None
        current_audio_task = None
        try:
            await s.cancel()
        except Exception:
            pass
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def forward_stream_audio(stream: SonioxStream):
        """Pump Soniox audio to Twilio in 20 ms frames (Twilio Media Streams
        expects ~160-byte µ-law @ 8 kHz; larger frames misbehave on some
        carriers, so we re-frame here)."""
        import base64
        nonlocal current_stream, current_audio_task
        FRAME = 160  # bytes = 20 ms of pcm_mulaw at 8 kHz
        frames_sent = 0
        try:
            async for payload in stream.audio():
                raw = base64.b64decode(payload)
                for off in range(0, len(raw), FRAME):
                    chunk = raw[off:off + FRAME]
                    await twilio_ws.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": base64.b64encode(chunk).decode("ascii")},
                    }))
                    frames_sent += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[soniox] forward error:", e)
        finally:
            print(f"[soniox] {stream.stream_id} forwarded {frames_sent} frames ({frames_sent*20}ms)")
            if current_stream is stream:
                current_stream = None
                current_audio_task = None

    async with SonioxConnection() as soniox_conn, websockets.connect(
        openai_realtime_url(),
        additional_headers=openai_headers(),
        max_size=None,
    ) as oai_ws:
        # Text-only. Dual modality ("audio","text") was tried but reliably
        # breaks output (no audio at all in production). Until we can figure
        # out what GA Realtime is rejecting, keep this safe and use the prompt
        # to drive short sentences + acknowledgments for conversational feel.
        session_msg = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": model,
                "output_modalities": ["text"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                            "create_response": True,
                            "interrupt_response": False,
                        },
                    },
                },
                "instructions": instructions,
            },
        }
        print(f"[oai] session.update sending output_modalities={session_msg['session']['output_modalities']}")
        await oai_ws.send(json.dumps(session_msg))
        await oai_ws.send(json.dumps({"type": "response.create"}))

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

        async def openai_to_soniox():
            nonlocal current_stream, current_audio_task, last_assistant_item
            delta_count = 0
            # Events we don't act on but want to see in logs while debugging.
            VERBOSE_EVENTS = {
                "session.created", "session.updated", "response.created",
                "response.done", "response.output_item.added",
                "response.output_text.delta", "response.output_text.done",
                "response.audio_transcript.delta", "response.audio_transcript.done",
                "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                "conversation.item.created", "rate_limits.updated", "error",
            }
            async for raw in oai_ws:
                ev = json.loads(raw)
                t = ev.get("type")
                if t in VERBOSE_EVENTS:
                    if t == "error":
                        print(f"[oai] ERROR: {ev}")
                    elif t in ("response.output_text.delta", "response.text.delta"):
                        pass  # logged separately as a count
                    else:
                        print(f"[oai] {t}")

                if t == "response.created":
                    if current_stream is not None:
                        print("[oai] response.created while prior Soniox stream still active — cancelling")
                        await cancel_current_stream()
                    resp_id = ev.get("response", {}).get("id") or "resp"
                    print(f"[oai] response.created id={resp_id}")
                    stream = await soniox_conn.start_stream(resp_id)
                    current_stream = stream
                    current_audio_task = asyncio.create_task(forward_stream_audio(stream))
                    delta_count = 0
                elif t in ("response.output_text.delta", "response.text.delta"):
                    if current_stream is not None:
                        delta = ev.get("delta", "")
                        if delta:
                            delta_count += 1
                            try:
                                await current_stream.send_text(delta)
                            except Exception as e:
                                print("[soniox] send_text error:", e)
                elif t == "response.done":
                    if current_stream is not None:
                        print(f"[oai] response.done after {delta_count} text deltas — flushing text_end")
                        try:
                            await current_stream.send_text("", end=True)
                        except Exception as e:
                            print("[soniox] text_end error:", e)
                    last_assistant_item = None
                elif t == "input_audio_buffer.speech_started":
                    # interrupt_response=False — OpenAI buffers the customer's
                    # speech and creates a new response when they're done. No
                    # mid-utterance barge-in in this mode, but the conversation
                    # still pivots on speech end.
                    pass

        try:
            await asyncio.gather(twilio_to_openai(), openai_to_soniox(), return_exceptions=True)
        finally:
            await cancel_current_stream()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
