# Deploy guide

This repo is two services. Vercel hosts the static React UI; the Python
WebSocket server lives elsewhere because **Vercel cannot run long-lived
WebSocket servers** (Twilio Media Streams keeps a WSS open for the whole
phone call).

```
[ Browser ]  ──HTTPS──>  [ Vercel: web/dist ]
[ Browser ]  ──HTTPS──>  [ Render: server (FastAPI) ]
[ Twilio  ]  ──WSS────>  [ Render: server (FastAPI) /ws ]
[ Server  ]  ──WSS────>  [ api.openai.com /v1/realtime ]
```

## 1. Server on Render

1. Push the repo to GitHub (already done).
2. Go to https://dashboard.render.com → **New + → Blueprint** → connect
   the `passportcard-demo` repo. Render reads [`render.yaml`](render.yaml)
   and creates the service.
3. In the new service's **Environment** tab fill the secrets that have
   `sync: false` in `render.yaml`:
   - `OPENAI_API_KEY`
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   - `TWILIO_FROM_NUMBER`
   - `PUBLIC_BASE_URL` — leave empty for now; you'll set it after the first deploy.
   - `WEB_ORIGIN` — leave empty for now; you'll set it after the Vercel deploy.
4. After the first deploy you'll have a URL like
   `https://passportcard-demo-server.onrender.com`. Copy it and set
   `PUBLIC_BASE_URL` to that exact URL, then redeploy. (Twilio will fetch
   `/twiml` from this URL and Stream to its `wss://` counterpart.)
5. Render Free tier spins down after 15 min of inactivity and cold-starts
   in ~30s. For a live demo, use the **Starter** plan ($7/mo) so the call
   doesn't time out while the dyno wakes up.

## 2. Web on Vercel

1. https://vercel.com/new → import the same GitHub repo.
2. Vercel will detect two roots — **set Root Directory to `web`**.
   Framework: `Vite`. Build command/output directory pre-filled by
   [`web/vercel.json`](web/vercel.json).
3. In **Environment Variables** add:
   - `VITE_API_BASE_URL` = your Render URL from step 1
     (e.g. `https://passportcard-demo-server.onrender.com`)
4. Deploy. You'll get a URL like
   `https://passportcard-demo.vercel.app`.
5. Back on Render, set `WEB_ORIGIN` to that exact Vercel URL (CORS lockdown)
   and redeploy.

## 3. Twilio sanity-check

Twilio doesn't need any URL configured in its console — the demo uses
the Twilio REST API to start each outbound call and passes the TwiML URL
inline (`url=<PUBLIC_BASE_URL>/twiml?...`).

If you're using the WhatsApp sandbox, make sure each recipient has sent
the `join <two-words>` message to +14155238886 from their own WhatsApp.

## Common pitfalls

- **Vercel deploy "fails"**: it's because the server folder has no
  framework Vercel recognizes. Setting Root Directory to `web` (per step
  2) fixes that — Vercel only sees the React app.
- **Browser can't reach the API**: check `VITE_API_BASE_URL` in the
  Vercel env (must be set at *build time*, not runtime — re-deploy after
  changing).
- **CORS blocked**: `WEB_ORIGIN` on Render must exactly match the Vercel
  origin (no trailing slash, https included). Multiple origins can be
  comma-separated.
- **Twilio call connects but no audio**: usually the OpenAI key is wrong
  or `gpt-realtime-2` isn't enabled on your account — check the Render
  logs for `OpenAI realtime error:` lines.
- **Render Free dyno is asleep**: the first call after idle will hang
  ~30s while the server cold-starts. Upgrade to Starter for demos.

## Local dev still works

Run server + ngrok + `npm run dev` exactly as before. Empty
`VITE_API_BASE_URL` falls back to the Vite proxy at `/api/*` →
`localhost:8000`.
