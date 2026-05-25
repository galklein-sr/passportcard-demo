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
from tts_soniox import SonioxSession
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

    model = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2")
    instructions = build_instructions(ctx["customer_name"], ctx["case"])

    # Per-turn Soniox state. Only one turn is active at a time.
    soniox: SonioxSession | None = None
    soniox_audio_task: asyncio.Task | None = None

    async def cancel_soniox_turn():
        """Barge-in: tear down the current Soniox session immediately."""
        nonlocal soniox, soniox_audio_task
        if soniox is None:
            return
        s, t = soniox, soniox_audio_task
        soniox = None
        soniox_audio_task = None
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
        try:
            await s.__aexit__(None, None, None)
        except Exception:
            pass

    async def forward_soniox_audio(session: SonioxSession):
        """Pump Soniox audio to Twilio. On natural end (terminated), self-clean.

        Soniox can emit large pcm_mulaw chunks (hundreds of ms). Twilio Media
        Streams expects ~20 ms frames (160 bytes µ-law @ 8 kHz). Larger frames
        play unevenly or get truncated on some carriers, so we re-frame here.
        """
        import base64
        nonlocal soniox, soniox_audio_task
        FRAME = 160  # bytes = 20 ms of pcm_mulaw at 8 kHz
        frames_sent = 0
        try:
            async for payload in session.audio():
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
            print(f"[soniox] {session.stream_id} forwarded {frames_sent} frames ({frames_sent*20}ms)")
            # If still the active session, mark idle and close.
            if soniox is session:
                soniox = None
                soniox_audio_task = None
                try:
                    await session.__aexit__(None, None, None)
                except Exception:
                    pass

    async with websockets.connect(
        openai_realtime_url(),
        additional_headers=openai_headers(),
        max_size=None,
    ) as oai_ws:
        # Same session shape as the audio path, but output_modalities=["text"] —
        # OpenAI keeps doing STT + server_vad turn-taking; we render audio via Soniox.
        # interrupt_response=False: OpenAI doesn't see the Soniox audio playing to
        # the customer, so any echo bleeding back into the call would otherwise be
        # treated as customer speech and barge-in over the agent's first sentence.
        await oai_ws.send(json.dumps({
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
                            "threshold": 0.6,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 600,
                            "create_response": True,
                            "interrupt_response": False,
                        },
                    },
                },
                "instructions": instructions,
            },
        }))
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
            nonlocal soniox, soniox_audio_task
            delta_count = 0
            async for raw in oai_ws:
                ev = json.loads(raw)
                t = ev.get("type")
                if t == "response.created":
                    # Start a fresh Soniox turn. If one is somehow still open, drop it.
                    if soniox is not None:
                        print("[oai] response.created while prior Soniox turn still active — cancelling")
                        await cancel_soniox_turn()
                    resp_id = ev.get("response", {}).get("id") or "resp"
                    print(f"[oai] response.created id={resp_id}")
                    s = SonioxSession(resp_id)
                    await s.__aenter__()
                    soniox = s
                    soniox_audio_task = asyncio.create_task(forward_soniox_audio(s))
                    delta_count = 0
                elif t in ("response.output_text.delta", "response.text.delta"):
                    if soniox is not None:
                        delta = ev.get("delta", "")
                        if delta:
                            delta_count += 1
                            try:
                                await soniox.send_text(delta)
                            except Exception as e:
                                print("[soniox] send_text error:", e)
                elif t == "response.done":
                    # End-of-response is the only signal we use to flush text_end.
                    # `response.output_text.done` also fires but earlier double-flushing
                    # caused Soniox to truncate the trailing audio.
                    if soniox is not None:
                        print(f"[oai] response.done after {delta_count} text deltas — flushing text_end")
                        try:
                            await soniox.send_text("", end=True)
                        except Exception as e:
                            print("[soniox] text_end error:", e)
                elif t == "input_audio_buffer.speech_started":
                    # interrupt_response=False on the session means OpenAI buffers the
                    # incoming speech but won't cancel the in-flight response. Echo or
                    # background noise will no longer cut off Soniox mid-utterance.
                    print("[oai] speech_started (buffered, no barge-in in soniox mode)")
                elif t == "error":
                    print("[oai] error:", ev)

        try:
            await asyncio.gather(twilio_to_openai(), openai_to_soniox(), return_exceptions=True)
        finally:
            await cancel_soniox_turn()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
