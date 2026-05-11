# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PassportCard Pay service-agent demo: a proactive AI voice agent that calls a customer in Hebrew after a card-usage refusal. Also has a WhatsApp channel that sends a templated message instead of placing a call.

Two services in one repo:
- `server/` — FastAPI app: HTTP API + Twilio outbound calls + WebSocket bridge between Twilio Media Streams and the OpenAI Realtime API.
- `web/` — React + Vite UI: contact picker + "call" button + status panel.

## Commands

```powershell
# Server (port 8000)
cd server
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py

# Tunnel for local Twilio webhooks
ngrok http 8000
# Put the https URL into PUBLIC_BASE_URL in .env, then restart the server.

# Web (port 5173, proxies /api -> :8000)
cd web
npm install
npm run dev
npm run build     # production build (consumed by Vercel)
```

There is no test suite and no linter configured.

## Architecture

Audio path during a voice call:

```
Browser ──HTTPS──> FastAPI /api/call ──REST──> Twilio (places outbound call)
Twilio  ──HTTPS──> FastAPI /twiml    (returns <Connect><Stream wss://.../ws>)
Twilio  ──WSS───>  FastAPI /ws       (Media Streams, G.711 µ-law base64 frames)
FastAPI ──WSS───>  api.openai.com/v1/realtime?model=gpt-realtime-2
```

Key design choices to preserve when editing the bridge in [server/main.py](server/main.py):

- **Audio is `audio/pcmu` (G.711 µ-law) end-to-end** — no resampling. The OpenAI Realtime GA `session.update` uses the nested `audio.input` / `audio.output` shape with MIME-style format strings (`{"type": "audio/pcmu"}`) and `output_modalities: ["audio"]`. Don't revert to the legacy flat shape.
- **Turn-taking is `server_vad`** with `create_response: true`. The opening line is triggered by an explicit `response.create` after `session.update`.
- **Barge-in**: when `input_audio_buffer.speech_started` arrives, cancel the in-flight response (`response.cancel` + `conversation.item.truncate` on `last_assistant_item`) and send Twilio a `clear` event to drop its outbound buffer.
- **GA event names**: audio deltas come as `response.output_audio.delta` (legacy `response.audio.delta` is still accepted as a fallback).
- The `/ws` handler consumes Twilio messages until the `start` event arrives, pulls `customParameters` (`case`, `name`) and `streamSid` from it, then enters `_run` which assumes start has already been consumed.

Per-call state:
- `CALL_STATE: dict[callSid -> {customer_name, case}]` is populated in `/api/call` and read in `/ws` (the `case` and `name` are also passed through TwiML `<Parameter>`s as a backup path).
- `CASES` is loaded once at import time from `instructions/מקרי בוחן עם נוסחים.xlsx` by [server/use_cases.py](server/use_cases.py). Each case has `id`, `rc`, `rc_description`, `failure_reason`, and `script` (Hebrew). `/api/call` picks one at random.

Prompt: [server/prompt.py](server/prompt.py) builds the Hebrew system prompt per call. The prompt has load-bearing rules: always open in Hebrew; address the customer in **plural** (`ניסיתם`, `אתם`) to dodge gender mistakes; switch language only if the customer does. Don't soften these without intent — they were tightened in recent commits (see `git log`).

WhatsApp channel: same `/api/call` endpoint, `channel: "whatsapp"`. Sends `case.script` (with `[שם הלקוח]` substituted) via Twilio Messages API from `TWILIO_WHATSAPP_FROM`. No bridge, no Realtime API.

## Deployment

Split deploy (see [DEPLOY.md](DEPLOY.md)): **web on Vercel**, **server on Render** ([render.yaml](render.yaml)). Vercel cannot host the server because Twilio Media Streams keeps a WSS open for the entire call. Web's Vercel root is `web/` (Vercel will fail if pointed at repo root). `VITE_API_BASE_URL` is a **build-time** env on Vercel; `WEB_ORIGIN` on Render must exactly match the Vercel origin for CORS.

## Environment variables

Server reads (via `python-dotenv` from the repo-root `.env`):
- `OPENAI_API_KEY`, `OPENAI_REALTIME_MODEL` (default `gpt-realtime-2`), `OPENAI_REALTIME_VOICE` (default `marin`)
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_WHATSAPP_FROM` (for WhatsApp channel)
- `PUBLIC_BASE_URL` — the externally-reachable HTTPS base (ngrok URL locally, Render URL in prod). Used to build the TwiML `<Stream>` WSS URL and the `/twiml` callback URL passed to Twilio.
- `WEB_ORIGIN` — comma-separated CORS allowlist; empty means `*`.
- `PORT` — Render injects this.

Web reads at build time: `VITE_API_BASE_URL` (empty in dev falls back to the Vite proxy).

## Contacts

[server/contacts.json](server/contacts.json) holds the demo contacts (Gal / Yaniv / Shay). Phone numbers are E.164 and must be filled in for calls to work.
