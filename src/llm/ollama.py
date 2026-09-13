from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from src.settings import Settings

log = logging.getLogger("cohost.llm")


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error: str | None = None

    async def health(self) -> dict[str, Any]:
        url = f"{self.settings.ollama_host}/api/tags"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
            names = [m.get("name", "") for m in data.get("models", [])]
            wanted = self.settings.ollama_model
            has_model = any(wanted == n or n.startswith(wanted) or wanted in n for n in names)
            self.last_error = None
            return {
                "ok": True,
                "models": names,
                "has_model": has_model,
                "host": self.settings.ollama_host,
                "model": self.settings.ollama_model,
            }
        except Exception as e:
            self.last_error = str(e)
            return {
                "ok": False,
                "error": str(e),
                "host": self.settings.ollama_host,
                "model": self.settings.ollama_model,
                "models": [],
                "has_model": False,
            }

    async def complete(self, messages: list[dict[str, str]], temperature: float = 0.7) -> str:
        chunks: list[str] = []
        async for piece in self.stream(messages, temperature=temperature):
            chunks.append(piece)
        return "".join(chunks).strip()

    async def stream(self, messages: list[dict[str, str]], temperature: float = 0.7) -> AsyncIterator[str]:
        payload = {
            "model": self.settings.ollama_model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature, "num_ctx": 4096},
        }
        timeout = httpx.Timeout(self.settings.ollama_timeout, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", f"{self.settings.ollama_host}/api/chat", json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        self.last_error = f"Ollama HTTP {resp.status_code}: {body[:300]}"
                        log.error(self.last_error)
                        return
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if data.get("error"):
                            self.last_error = str(data["error"])
                            log.error("Ollama error: %s", self.last_error)
                            return
                        msg = (data.get("message") or {}).get("content") or ""
                        if msg:
                            yield msg
                        if data.get("done"):
                            break
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)
            log.error("Ollama request failed: %s", e)

    async def stream_sentences(self, messages: list[dict[str, str]], temperature: float = 0.7) -> AsyncIterator[str]:
        buf = ""
        async for piece in self.stream(messages, temperature=temperature):
            buf += piece
            while True:
                match = re.search(r"(.+?[.!?])(\s+|$)", buf, flags=re.S)
                if not match:
                    break
                sentence = match.group(1).strip()
                buf = buf[match.end() :]
                if sentence:
                    yield sentence
        tail = buf.strip()
        if tail:
            yield tail
