from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket


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
            "ollama": "unknown",
            "caption": "",
            "last_user": "",
        }

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
