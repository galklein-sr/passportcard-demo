"""Soniox real-time TTS WebSocket client.

The Soniox real-time API multiplexes up to 5 concurrent streams per
WebSocket. We open ONE connection for the duration of a call and run
each assistant turn as a separate stream, identified by `stream_id`.
A background reader demuxes server messages into per-stream queues, and
a heartbeat task sends `keep_alive` every 20s so the connection isn't
dropped between turns.

Per-turn lifecycle (3-step handshake):
  client config(stream_id)             ─▶
  client text(chunk, end=False) ...    ─▶
  client text(end=True)                ─▶
                                       ◀─ audio chunks (base64 pcm_mulaw)
                                       ◀─ audio_end: true
                                       ◀─ terminated: true

Cancellation:
  client {"stream_id": ..., "cancel": true}  ─▶
                                              ◀─ terminated: true

References:
- https://soniox.com/docs/tts/rt/streams
- https://soniox.com/docs/tts/rt/termination
- https://soniox.com/docs/tts/rt/connection-keepalive
- https://soniox.com/docs/api-reference/tts/websocket-api
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import AsyncIterator

import websockets


SONIOX_WS_URL = "wss://tts-rt.soniox.com/tts-websocket"
KEEPALIVE_INTERVAL_SEC = 20  # docs say every 20-30s during idle


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


class SonioxStream:
    """One assistant turn. Created via SonioxConnection.start_stream()."""

    # Sentinel pushed onto the audio queue to signal stream termination.
    _END = object()

    def __init__(self, conn: "SonioxConnection", stream_id: str):
        self._conn = conn
        self.stream_id = stream_id
        self._queue: asyncio.Queue = asyncio.Queue()
        self._text_end_sent = False
        self._closed = False

    async def send_text(self, text: str, end: bool = False) -> None:
        # Soniox accepts a single `text_end:true`; further sends after end
        # are undefined and have been seen to truncate trailing audio.
        if self._text_end_sent or self._closed:
            return
        if end:
            self._text_end_sent = True
        await self._conn._send_json({
            "text": text,
            "text_end": end,
            "stream_id": self.stream_id,
        })

    async def cancel(self) -> None:
        if self._closed:
            return
        # Mark text_end as sent so subsequent send_text calls become no-ops.
        self._text_end_sent = True
        try:
            await self._conn._send_json({
                "stream_id": self.stream_id,
                "cancel": True,
            })
        except Exception:
            pass

    async def audio(self) -> AsyncIterator[str]:
        """Yield base64-encoded pcm_mulaw chunks until the stream terminates."""
        bytes_yielded = 0
        while True:
            item = await self._queue.get()
            if item is self._END:
                break
            assert isinstance(item, str)
            bytes_yielded += (len(item) * 3) // 4  # rough decoded length
            yield item
        print(f"[soniox] {self.stream_id} ended (~{bytes_yielded}ms audio)")

    # ---- internal: called by SonioxConnection reader ----

    def _push_audio(self, b64: str) -> None:
        self._queue.put_nowait(b64)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(self._END)


class SonioxConnection:
    """One persistent Soniox WebSocket, multiplexing many SonioxStream turns.

    Usage:
        async with SonioxConnection() as conn:
            stream = await conn.start_stream("resp_1")
            await stream.send_text("Hello, ")
            await stream.send_text("world.", end=True)
            async for chunk in stream.audio():
                ...
            # connection stays open; start more streams as needed
    """

    def __init__(self):
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._streams: dict[str, SonioxStream] = {}
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> "SonioxConnection":
        self._ws = await websockets.connect(SONIOX_WS_URL, max_size=None)
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        print("[soniox] connection open")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Drain any open streams so their audio iterators exit.
        for s in list(self._streams.values()):
            s._close()
        self._streams.clear()
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._reader_task:
            self._reader_task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        print("[soniox] connection closed")

    async def start_stream(self, stream_id: str) -> SonioxStream:
        """Send config for a new stream and return its handle."""
        if self._closed or self._ws is None:
            raise RuntimeError("SonioxConnection is closed")
        stream = SonioxStream(self, stream_id)
        self._streams[stream_id] = stream
        await self._send_json(_config_message(stream_id))
        print(f"[soniox] stream {stream_id} started")
        return stream

    # ---- internals ----

    async def _send_json(self, payload: dict) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            await self._ws.send(json.dumps(payload))

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                sid = ev.get("stream_id")
                stream = self._streams.get(sid) if sid else None

                if "error_code" in ev:
                    print(f"[soniox] stream {sid} error: {ev}")
                    if stream is not None:
                        stream._close()
                        self._streams.pop(sid, None)
                    continue

                if stream is None:
                    # Message for an unknown/closed stream — ignore.
                    continue

                if "audio" in ev:
                    stream._push_audio(ev["audio"])

                if ev.get("terminated"):
                    print(f"[soniox] stream {sid} terminated")
                    stream._close()
                    self._streams.pop(sid, None)
        except (asyncio.CancelledError, Exception) as e:
            if not isinstance(e, asyncio.CancelledError):
                print(f"[soniox] reader exit: {e}")
            # Tear down all streams so their consumers stop waiting.
            for s in list(self._streams.values()):
                s._close()
            self._streams.clear()

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL_SEC)
                try:
                    await self._send_json({"keep_alive": True})
                except Exception as e:
                    print(f"[soniox] keepalive send failed: {e}")
                    return
        except asyncio.CancelledError:
            pass
