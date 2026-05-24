"""Soniox real-time TTS WebSocket client.

One SonioxSession == one assistant turn. The OpenAI Realtime API streams text
deltas; we forward them as `text` chunks, then send `text_end:true` when the
turn is done. Soniox streams base64-encoded pcm_mulaw @ 8 kHz back, which
slots straight into Twilio's media frames.

Protocol reference: https://soniox.com/docs/api-reference/tts/websocket-api
"""

from __future__ import annotations

import json
import os
from typing import AsyncIterator

import websockets


SONIOX_WS_URL = "wss://tts-rt.soniox.com/tts-websocket"


def _config_message(stream_id: str) -> dict:
    return {
        "api_key": os.environ["SONIOX_API_KEY"],
        "model": os.environ.get("SONIOX_MODEL", "tts-rt-v1"),
        "language": os.environ.get("SONIOX_LANGUAGE", "he"),
        "voice": os.environ.get("SONIOX_VOICE", "Maya"),
        "audio_format": "pcm_mulaw",
        "sample_rate": 8000,
        "stream_id": stream_id,
    }


class SonioxSession:
    """Per-turn Soniox TTS WebSocket session.

    Usage:
        async with SonioxSession(stream_id) as s:
            await s.send_text("hello ")
            await s.send_text("world", end=True)
            async for mulaw_b64 in s.audio():
                ...
    """

    def __init__(self, stream_id: str):
        self.stream_id = stream_id
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def __aenter__(self) -> "SonioxSession":
        self._ws = await websockets.connect(SONIOX_WS_URL, max_size=None)
        await self._ws.send(json.dumps(_config_message(self.stream_id)))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def send_text(self, text: str, end: bool = False) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps({
            "text": text,
            "text_end": end,
            "stream_id": self.stream_id,
        }))

    async def cancel(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({
                "stream_id": self.stream_id,
                "cancel": True,
            }))
        except Exception:
            pass

    async def audio(self) -> AsyncIterator[str]:
        """Yield base64-encoded pcm_mulaw chunks until the stream terminates."""
        assert self._ws is not None
        async for raw in self._ws:
            ev = json.loads(raw)
            if "audio" in ev:
                yield ev["audio"]
                if ev.get("audio_end"):
                    # Soniox will send `terminated` next; keep reading until then.
                    continue
            if ev.get("terminated"):
                return
            if "error_code" in ev:
                print("Soniox error:", ev)
                return
