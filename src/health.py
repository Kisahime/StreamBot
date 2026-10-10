from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from typing import Any

from src.audio.devices import list_devices
from src.settings import Settings
from src.twitch.auth import FEED_ORDER

_gpu_cache: tuple[float, dict[str, Any]] | None = None
_ollama_cache: tuple[float, dict[str, Any]] | None = None
_devices_cache: dict[str, Any] | None = None


def clear_device_cache() -> None:
    global _devices_cache
    _devices_cache = None


async def load_devices(*, refresh: bool = False) -> dict[str, Any]:
    global _devices_cache
    if refresh:
        clear_device_cache()
    if _devices_cache is None:
        try:
            _devices_cache = await asyncio.to_thread(list_devices)
        except Exception as e:
            _devices_cache = {"error": str(e), "inputs": [], "outputs": []}
    return _devices_cache


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
    global _ollama_cache
    now = time.monotonic()
    if _ollama_cache and now - _ollama_cache[0] < 10:
        ollama = _ollama_cache[1]
    else:
        ollama = await orch.llm.health()
        _ollama_cache = (now, ollama)
    gpu = await asyncio.to_thread(nvidia_gpu)
    devices = await load_devices()
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
            "mode": getattr(orch.tts, "mode", "oneshot"),
        },
        "twitch": {
            "state": orch.hub.status.get("twitch"),
            "eventsub": orch.hub.status.get("eventsub"),
            "channel": settings.twitch_channel,
            "nick": settings.twitch_nick,
            "has_token": bool(settings.twitch_token),
            "has_client_id": bool(settings.twitch_client_id),
            "has_event_token": bool(settings.event_token()),
            "events_enabled": settings.events_enabled,
            "feeds": _twitch_feeds(orch),
        },
        "persona": orch.persona.get("name"),
        "streamer": {
            "display_name": orch.identity.display_name,
            "twitch_login": orch.identity.twitch_login or settings.twitch_channel,
            "spoken_name": orch.identity.spoken_name(),
        },
        "memory": orch.memory.snapshot(),
        "devices": devices,
        "setup": _setup_hints(ollama, settings, gpu, orch),
        "vram": {
            "profile": settings.vram_profile,
            "warn_mb": settings.vram_warn_mb,
            "snappy": "Whisper CUDA + qwen2.5:3b",
            "quality": "Whisper CPU + qwen2.5:7b-instruct-q4_K_M",
        },
        "timings": getattr(orch, "timings", {}),
        "duck": orch.duck.status() if getattr(orch, "duck", None) else {"mode": "off", "note": ""},
        "wake": orch.mic.wake.status() if getattr(orch, "mic", None) else {"enabled": False},
        "mood": getattr(orch, "mood", "normal"),
        "voices": list(settings.tts_voices),
        "voice": settings.tts_voice,
        "language": settings.whisper_language,
        "vad": {
            "silence_ms": settings.silence_ms,
            "energy_threshold": settings.energy_threshold,
            "max_utterance_seconds": settings.max_utterance_seconds,
        },
        "audio": {
            "mic_device": settings.mic_device,
            "speaker_device": settings.speaker_device,
            "mic_device_name": settings.mic_device_name,
            "speaker_device_name": settings.speaker_device_name,
        },
        "overlay": {"alert_hold_seconds": settings.overlay_alert_hold},
        "profile": getattr(orch, "profile_name", ""),
        "profiles": list(getattr(settings, "profiles", {}) or {}),
        "runtime": {
            "mood": getattr(orch, "mood", "normal"),
            "cooldown_seconds": settings.cooldown_seconds,
            "per_user_cooldown_seconds": settings.per_user_cooldown_seconds,
            "events_speak": settings.events_speak,
            "post_voice_replies_to_chat": settings.post_voice_replies_to_chat,
            "max_replies_per_minute": settings.max_replies_per_minute,
        },
    }


def _twitch_feeds(orch) -> dict[str, str]:
    client = getattr(orch, "eventsub", None)
    reported = dict(getattr(client, "feed_status", {}) or {})
    return {name: str(reported.get(name) or "off") for name in FEED_ORDER}


def _setup_hints(ollama: dict[str, Any], settings: Settings, gpu: dict[str, Any] | None = None, orch=None) -> list[str]:
    hints: list[str] = []
    expires = getattr(getattr(orch, "eventsub", None), "expires_in", None)
    if isinstance(expires, int) and expires < 24 * 60 * 60:
        hints.append("Twitch token expires in under 24 hours. Generate a new one before the next stream.")
    hints.extend(settings.secret_hints())
    if not ollama.get("ok"):
        hints.append("Ollama is not reachable. Install it from https://ollama.com and keep it running.")
    elif not ollama.get("has_model"):
        hints.append(f"Pull the model:  ollama pull {settings.ollama_model}")
    if gpu and gpu.get("ok"):
        used = int(gpu.get("memory_used_mb") or 0)
        if used >= settings.vram_warn_mb:
            if settings.vram_profile == "quality":
                hints.append(
                    f"VRAM {used} MB is over {settings.vram_warn_mb}. Quality profile is already Whisper-on-CPU + 7B. Close other GPU apps."
                )
            else:
                hints.append(
                    f"VRAM {used} MB is over {settings.vram_warn_mb}. "
                    "Switch vram.profile to quality (Whisper on CPU + 7B) or set whisper.device: cpu."
                )
    return hints
