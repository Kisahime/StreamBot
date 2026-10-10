from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("cohost.hub")
MEMORY_INTERVAL = 0.5


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.status: dict[str, Any] = {
            "phase": "idle",
            "listen": False,
            "ptt": False,
            "tts_mute": False,
            "speaking": False,
            "twitch": "disconnected",
            "eventsub": "disconnected",
            "ollama": "unknown",
            "caption": "",
            "last_user": "",
            "streamer_name": "",
            "streamer_login": "",
            "persona": "",
            "mood": "normal",
            "stt_ready": False,
            "tts_ready": False,
            "skip": {"reason": "", "user": ""},
            "timings": {},
            "queue": {"chat": [], "alerts": []},
            "twitch_error": "",
            "eventsub_error": "",
            "duck": "off",
            "post_voice": True,
            "duck_enabled": False,
            "wake": {"enabled": False},
        }
        self._memory_pending: dict[str, Any] | None = None
        self._memory_task: asyncio.Task | None = None
        self._memory_sent_at = 0.0

    async def register(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def patch_status(self, **kwargs: Any) -> None:
        self.status.update(kwargs)
        await self.broadcast({"type": "status", "data": dict(self.status)})

    async def broadcast(self, event: dict[str, Any]) -> None:
        if event.get("type") == "memory":
            self._queue_memory(event)
            return
        await self._send(event)

    def _queue_memory(self, event: dict[str, Any]) -> None:
        """Keep at most two memory snapshots per second. Newer ones replace a pending send."""
        self._memory_pending = event
        if self._memory_task is not None and not self._memory_task.done():
            return
        self._memory_task = asyncio.create_task(self._drain_memory(), name="memory-broadcast")

    async def _drain_memory(self) -> None:
        try:
            while self._memory_pending is not None:
                event = self._memory_pending
                self._memory_pending = None
                wait = MEMORY_INTERVAL - (time.monotonic() - self._memory_sent_at)
                if self._memory_sent_at and wait > 0:
                    await asyncio.sleep(wait)
                    if self._memory_pending is not None:
                        event = self._memory_pending
                        self._memory_pending = None
                await self._send(event)
                self._memory_sent_at = time.monotonic()
        except Exception:
            log.exception("memory broadcast failed")

    async def _send(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        async with self._lock:
            clients = list(self._clients)
        stale: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            await self.unregister(ws)
