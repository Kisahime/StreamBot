from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from typing import Any

from src.audio.devices import list_devices
from src.settings import Settings

_gpu_cache: tuple[float, dict[str, Any]] | None = None
_ollama_cache: tuple[float, dict[str, Any]] | None = None
_devices_cache: dict[str, Any] | None = None


def nvidia_gpu() -> dict[str, Any]:
    global _gpu_cache
    now = time.monotonic()
    if _gpu_cache and now - _gpu_cache[0] < 8:
        return _gpu_cache[1]
    creation = 0
    if sys.platform == "win32":
        creation = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            timeout=4,
            text=True,
            creationflags=creation,
        )
        line = out.strip().splitlines()[0]
        name, total, used, util = [p.strip() for p in line.split(",")]
        data = {
            "ok": True,
            "name": name,
            "memory_total_mb": int(float(total)),
            "memory_used_mb": int(float(used)),
            "utilization": int(float(util)),
        }
    except Exception as e:
        data = {"ok": False, "error": str(e)}
    _gpu_cache = (now, data)
    return data


async def snapshot(settings: Settings, orch) -> dict[str, Any]:
    global _ollama_cache, _devices_cache
    now = time.monotonic()
    if _ollama_cache and now - _ollama_cache[0] < 10:
        ollama = _ollama_cache[1]
    else:
        ollama = await orch.llm.health()
        _ollama_cache = (now, ollama)
    gpu = await asyncio.to_thread(nvidia_gpu)
    if _devices_cache is None:
        try:
            _devices_cache = await asyncio.to_thread(list_devices)
        except Exception as e:
            _devices_cache = {"error": str(e), "inputs": [], "outputs": []}
    return {
        "gpu": gpu,
        "ollama": ollama,
        "whisper": {
            "model": settings.whisper_model,
            "device": orch.stt.device,
            "compute_type": orch.stt.compute_type,
            "loaded": orch.stt.model is not None,
            "error": orch.stt.error,
        },
        "tts": {
            "ready": orch.tts.ready,
            "error": orch.tts.error,
            "mute": orch.tts_mute,
        },
        "twitch": {
            "state": orch.hub.status.get("twitch"),
            "channel": settings.twitch_channel,
            "nick": settings.twitch_nick,
            "has_token": bool(settings.twitch_token),
        },
        "persona": orch.persona.get("name"),
        "memory": orch.memory.snapshot(),
        "devices": _devices_cache,
        "setup": _setup_hints(ollama, settings),
    }


def _setup_hints(ollama: dict[str, Any], settings: Settings) -> list[str]:
    hints: list[str] = []
    if not ollama.get("ok"):
        hints.append("Ollama is not reachable. Install it from https://ollama.com and keep it running.")
    elif not ollama.get("has_model"):
        hints.append(f'Pull the default model:  ollama pull {settings.ollama_model}')
    if not settings.twitch_token:
        hints.append("Copy .env.example to .env and add a Twitch chat token.")
    return hints
