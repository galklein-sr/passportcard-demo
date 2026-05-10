# PassportCard Pay – Service Agent Demo

Proactive AI voice agent that calls a customer after a card-usage refusal,
explains the issue in Hebrew, and offers next steps.
Switches language automatically if the customer responds in another language.

**Stack:** FastAPI + Twilio + OpenAI Realtime GA (`gpt-realtime-2`, `audio/pcmu` passthrough), React + Vite frontend.

## Setup

1. Fill in `.env` (copy from `.env.example`):
   - `OPENAI_API_KEY` (model defaults to `gpt-realtime-2`)
   - Twilio Account SID, Auth Token, From number
   - `PUBLIC_BASE_URL` = your ngrok HTTPS URL
2. Add real phone numbers to `server/contacts.json` (E.164, e.g. `+972501234567`).

## Run

In three terminals:

```powershell
# 1. Server
cd server
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

```powershell
# 2. Tunnel
ngrok http 8000
# copy the https URL into PUBLIC_BASE_URL in .env, restart the server
```

```powershell
# 3. Web UI
cd web
npm install
npm run dev
# open http://localhost:5173
```

## Demo flow

1. Open the web UI, pick a contact, click **חייג ללקוח**.
2. The server picks a random refusal use case from `instructions/מקרי בוחן עם נוסחים.xlsx`,
   triggers a Twilio outbound call to the contact's phone.
3. Twilio fetches `/twiml`, which returns a `<Connect><Stream>` pointing at the WSS bridge.
4. The bridge opens a WebSocket to `wss://api.openai.com/v1/realtime?model=gpt-realtime-2`,
   sends a GA `session.update` (nested `audio.input`/`audio.output`, MIME-style `audio/pcmu`),
   then triggers the agent's opening line. Audio is `audio/pcmu` (G.711 µ-law) end-to-end — no resampling.
5. Speak — `server_vad` handles turn-taking; barge-in cancels in-flight responses.

## Files

- `server/main.py` — HTTP + WebSocket app, Twilio outbound call, TwiML, bridge.
- `server/use_cases.py` — loads use cases from the xlsx.
- `server/prompt.py` — builds the per-call Hebrew system prompt.
- `server/bridge.py` — OpenAI Realtime URL + auth header helpers (the bridge body lives in `main.py`).
- `server/contacts.json` — Gal / Yaniv / Shay (add phone numbers).
- `web/src/App.tsx` — UI: contact picker + call button + status panel.

## Notes

- Voice defaults to `marin`; try `cedar` for a male voice (set `OPENAI_REALTIME_VOICE` in `.env`).
- Available voices: `alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`, `marin`, `cedar`. OpenAI recommends `marin` / `cedar` for highest quality.
- Model defaults to `gpt-realtime-2` (current GA flagship, May 2026). Set `OPENAI_REALTIME_MODEL=gpt-realtime-mini` for the cheaper variant.
- The agent always opens in Hebrew; if the customer speaks another language it follows them automatically.
