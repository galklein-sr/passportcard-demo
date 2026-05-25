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

    Conversational design:
      - OpenAI emits text deltas; we buffer them until a sentence boundary.
      - Each sentence is rendered as its own short Soniox stream (cheap, since
        the WebSocket is shared and persistent).
      - We trail each sentence's audio with a Twilio `mark` so we know exactly
        when playback finishes on the customer's line. Between marks we open
        the customer-audio gate briefly so OpenAI's VAD can detect a real
        barge-in — without echo leaking back during agent speech.
      - On barge-in (VAD speech_started while the gate is open), we cancel the
        in-flight OpenAI response and drop any queued sentences for that turn.
    """
    import asyncio, base64, json, os, re
    import time as _time
    import websockets
    from prompt import build_instructions

    model = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2")
    instructions = build_instructions(ctx["customer_name"], ctx["case"])

    # ---- timing constants ----
    AGENT_TAIL_GRACE_SEC = 0.10  # tiny tail after Twilio confirms a sentence finished playing
    BARGE_IN_WINDOW_SEC = 0.60   # gate open between sentences so VAD can fire on real speech
    FRAME = 160  # bytes = 20 ms of pcm_mulaw at 8 kHz

    # Sentence boundary: any of . ! ? ׃ (Hebrew sof pasuq) followed by space/end,
    # or a newline. We keep the punctuation in the chunk so Soniox prosody is right.
    SENTENCE_BOUNDARY = re.compile(r"[.!?׃](?=\s|$)|\n")

    # ---- per-call state ----
    soniox_conn: SonioxConnection  # set inside the async-with
    current_stream: SonioxStream | None = None
    current_audio_task: asyncio.Task | None = None
    # Single timestamp: drop customer media until monotonic() >= this value.
    # Far in the future while the agent is mid-sentence; resets to now+grace
    # when each sentence's audio is confirmed played by Twilio.
    gate_customer_until: float = 0.0
    # Queue of sentence-text fragments awaiting playback for the current response.
    sentence_queue: "asyncio.Queue[str | None]" = asyncio.Queue()
    # Set when the customer barges in (VAD during open gate). Player drops pending sentences.
    barge_in_event = asyncio.Event()
    # Maps Twilio mark name -> asyncio.Event we set when that mark echoes back.
    pending_marks: dict[str, asyncio.Event] = {}
    # Monotonically-increasing sentence id so each Soniox stream / mark name is unique.
    sentence_counter = 0

    def _next_sid(resp_id: str) -> str:
        nonlocal sentence_counter
        sentence_counter += 1
        return f"{resp_id}_s{sentence_counter}"

    async def forward_stream_audio(stream: SonioxStream) -> None:
        """Pump Soniox audio chunks to Twilio in 20 ms frames."""
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
            print(f"[soniox] {stream.stream_id} forwarded {frames_sent} frames")

    async def play_sentence(text: str) -> None:
        """Play one sentence, blocking until Twilio confirms it has finished playing."""
        nonlocal current_stream, current_audio_task, gate_customer_until
        text = text.strip()
        if not text:
            return
        # Hard-close the customer audio gate for the duration of this sentence
        # (60 s ceiling; reset below once Twilio confirms playback finished).
        gate_customer_until = _time.monotonic() + 60.0
        sid = _next_sid(text[:8])  # reuse text prefix as a debug hint; we override below
        # Start a fresh per-sentence Soniox stream.
        stream = await soniox_conn.start_stream(sid)
        current_stream = stream
        audio_task = asyncio.create_task(forward_stream_audio(stream))
        current_audio_task = audio_task
        # Send text + text_end so Soniox starts generating and flushes promptly.
        try:
            await stream.send_text(text, end=True)
        except Exception as e:
            print("[soniox] send_text error:", e)
        # Wait for all audio chunks to be forwarded to Twilio.
        try:
            await audio_task
        except (asyncio.CancelledError, Exception):
            pass
        if current_stream is stream:
            current_stream = None
            current_audio_task = None
        if barge_in_event.is_set():
            # Audio was cancelled mid-sentence by a barge-in. No point sending
            # a mark and waiting for it — the buffer is being cleared anyway.
            return
        # Send a Twilio mark right after the last audio frame and wait for the
        # echo so we know the customer actually finished hearing the sentence.
        mark_name = f"m_{sid}"
        mark_evt = asyncio.Event()
        pending_marks[mark_name] = mark_evt
        try:
            await twilio_ws.send_text(json.dumps({
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": mark_name},
            }))
        except Exception:
            pending_marks.pop(mark_name, None)
            return
        try:
            await asyncio.wait_for(mark_evt.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            print(f"[twilio] mark {mark_name} timed out")
        finally:
            pending_marks.pop(mark_name, None)
        # Tiny echo grace, then open the gate for the barge-in window.
        gate_customer_until = _time.monotonic() + AGENT_TAIL_GRACE_SEC

    async def cancel_current_sentence() -> None:
        """Cancel the in-flight Soniox stream (used on barge-in)."""
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

    async def sentence_player() -> None:
        """Pull sentences off the queue and play them in order, honoring barge-in."""
        nonlocal gate_customer_until
        while True:
            sentence = await sentence_queue.get()
            if sentence is None:
                # End-of-response sentinel: open the gate so customer can talk.
                gate_customer_until = _time.monotonic() + AGENT_TAIL_GRACE_SEC
                continue
            if barge_in_event.is_set():
                # Drop sentences from a cancelled response.
                continue
            await play_sentence(sentence)
            if barge_in_event.is_set():
                continue
            # Brief barge-in window between sentences. Twilio's audio buffer is
            # now empty (mark confirmed) so VAD won't see echo; only real
            # customer speech reaches OpenAI during this window.
            await asyncio.sleep(BARGE_IN_WINDOW_SEC)

    async with SonioxConnection() as _soniox_conn, websockets.connect(
        openai_realtime_url(),
        additional_headers=openai_headers(),
        max_size=None,
    ) as oai_ws:
        soniox_conn = _soniox_conn  # bind for inner closures (Python doesn't let us nonlocal an outer name from async-with)
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
                            "prefix_padding_ms": 200,
                            "silence_duration_ms": 400,
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
                        if _time.monotonic() < gate_customer_until:
                            continue
                        await oai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"],
                        }))
                    elif ev == "mark":
                        name = data.get("mark", {}).get("name")
                        evt = pending_marks.get(name) if name else None
                        if evt is not None:
                            evt.set()
                    elif ev == "stop":
                        break
            except WebSocketDisconnect:
                pass

        async def openai_to_soniox():
            """Read OpenAI events, buffer text deltas into sentences, and push
            sentence text to the player queue. Handle barge-in on VAD start."""
            nonlocal gate_customer_until
            VERBOSE_EVENTS = {
                "session.created", "session.updated", "response.created",
                "response.done", "input_audio_buffer.speech_started",
                "input_audio_buffer.speech_stopped", "error",
            }
            text_buffer = ""
            delta_count = 0
            response_id: str | None = None

            def flush_complete_sentences() -> None:
                nonlocal text_buffer
                while True:
                    m = SENTENCE_BOUNDARY.search(text_buffer)
                    if m is None:
                        return
                    end = m.end()
                    chunk = text_buffer[:end].strip()
                    text_buffer = text_buffer[end:]
                    if chunk:
                        sentence_queue.put_nowait(chunk)

            async for raw in oai_ws:
                ev = json.loads(raw)
                t = ev.get("type")
                if t in VERBOSE_EVENTS:
                    if t == "error":
                        print(f"[oai] ERROR: {ev}")
                    else:
                        print(f"[oai] {t}")

                if t == "response.created":
                    barge_in_event.clear()
                    text_buffer = ""
                    delta_count = 0
                    response_id = ev.get("response", {}).get("id") or "resp"
                    print(f"[oai] response.created id={response_id}")
                    # Hard-close gate for the upcoming sentence(s).
                    gate_customer_until = _time.monotonic() + 60.0
                    try:
                        await oai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    except Exception:
                        pass
                elif t in ("response.output_text.delta", "response.text.delta"):
                    if barge_in_event.is_set():
                        continue
                    delta = ev.get("delta", "")
                    if not delta:
                        continue
                    delta_count += 1
                    text_buffer += delta
                    flush_complete_sentences()
                elif t == "response.done":
                    # Flush any trailing text without a final punctuation.
                    if text_buffer.strip() and not barge_in_event.is_set():
                        sentence_queue.put_nowait(text_buffer.strip())
                    text_buffer = ""
                    sentence_queue.put_nowait(None)  # end-of-response sentinel
                    print(f"[oai] response.done after {delta_count} text deltas")
                elif t == "input_audio_buffer.speech_started":
                    # Only treat as barge-in if our gate is currently OPEN
                    # (i.e., between sentences). If the gate is closed the
                    # event is almost certainly echo and we ignore it.
                    if _time.monotonic() >= gate_customer_until:
                        print("[oai] customer barge-in detected")
                        barge_in_event.set()
                        # Drain the sentence queue so the player drops the rest.
                        while not sentence_queue.empty():
                            try:
                                sentence_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                        try:
                            await oai_ws.send(json.dumps({"type": "response.cancel"}))
                        except Exception:
                            pass
                        await cancel_current_sentence()
                        # Flush whatever Twilio still has buffered.
                        try:
                            await twilio_ws.send_text(json.dumps({
                                "event": "clear",
                                "streamSid": stream_sid,
                            }))
                        except Exception:
                            pass

        try:
            await asyncio.gather(
                twilio_to_openai(),
                openai_to_soniox(),
                sentence_player(),
                return_exceptions=True,
            )
        finally:
            await cancel_current_sentence()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
