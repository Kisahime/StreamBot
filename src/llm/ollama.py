from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from src.sentences import cap_sentence, take_sentence
from src.settings import Settings

log = logging.getLogger("cohost.llm")


def model_names_match(wanted: str, name: str) -> bool:
    """Exact model name, or the same name with a ``:latest`` tag."""
    left = (wanted or "").strip()
    right = (name or "").strip()
    if not left or not right:
        return False
    if right == left or right == f"{left}:latest":
        return True
    if left.endswith(":latest") and right == left[: -len(":latest")]:
        return True
    return False


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_error: str | None = None
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            timeout = httpx.Timeout(self.settings.ollama_timeout, connect=5.0)
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def health(self) -> dict[str, Any]:
        url = f"{self.settings.ollama_host}/api/tags"
        try:
            r = await self._http().get(url)
            r.raise_for_status()
            data = r.json()
            names = [m.get("name", "") for m in data.get("models", [])]
            wanted = self.settings.ollama_model
            has_model = any(model_names_match(wanted, n) for n in names)
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

    async def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        cancel: asyncio.Event | None = None,
        num_predict: int | None = None,
    ) -> str:
        chunks: list[str] = []
        predict = self.settings.ollama_num_predict_chat if num_predict is None else num_predict
        async for piece in self.stream(messages, temperature=temperature, cancel=cancel, num_predict=predict):
            chunks.append(piece)
        return "".join(chunks).strip()

    async def stream(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        cancel: asyncio.Event | None = None,
        num_predict: int | None = None,
    ) -> AsyncIterator[str]:
        predict = self.settings.ollama_num_predict_voice if num_predict is None else num_predict
        payload = {
            "model": self.settings.ollama_model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.settings.ollama_keep_alive,
            "options": {
                "temperature": temperature,
                "num_ctx": self.settings.ollama_num_ctx,
                "num_predict": predict,
                "repeat_penalty": 1.15,
            },
        }
        try:
            client = self._http()
            async with client.stream("POST", f"{self.settings.ollama_host}/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    self.last_error = f"Ollama HTTP {resp.status_code}: {body[:300]}"
                    log.error(self.last_error)
                    return
                async for line in resp.aiter_lines():
                    if cancel is not None and cancel.is_set():
                        await resp.aclose()
                        return
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
            if cancel is not None and cancel.is_set():
                return
            self.last_error = str(e)
            log.error("Ollama request failed: %s", e)

    async def stream_sentences(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        cancel: asyncio.Event | None = None,
        max_chars: int | None = None,
    ) -> AsyncIterator[str]:
        limit = max_chars if max_chars is not None else self.settings.max_sentence_chars
        buf = ""
        spoken = 0
        async for piece in self.stream(
            messages,
            temperature=temperature,
            cancel=cancel,
            num_predict=self.settings.ollama_num_predict_voice,
        ):
            if cancel is not None and cancel.is_set():
                return
            buf += piece
            while True:
                nxt = take_sentence(buf, flush=False)
                if not nxt:
                    break
                sentence, buf = nxt
                sentence = cap_sentence(sentence, limit)
                if not sentence:
                    continue
                spoken += len(sentence)
                yield sentence
                if spoken >= limit:
                    return
        tail = take_sentence(buf, flush=True)
        if tail and tail[0] and spoken < limit:
            yield cap_sentence(tail[0], limit)
