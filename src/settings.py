from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def _get(data: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class Settings:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.root = ROOT

        self.host = _get(data, "server.host", "127.0.0.1")
        self.port = int(_get(data, "server.port", 8080))

        self.ollama_host = _get(data, "ollama.host", "http://127.0.0.1:11434").rstrip("/")
        self.ollama_model = _get(data, "ollama.model", "qwen2.5:3b")
        self.ollama_timeout = float(_get(data, "ollama.timeout_seconds", 120))

        self.whisper_model = _get(data, "whisper.model_size", "small")
        self.whisper_device = _get(data, "whisper.device", "auto")
        self.whisper_language = _get(data, "whisper.language", "en")
        self.compute_type_cuda = _get(data, "whisper.compute_type_cuda", "float16")
        self.compute_type_cpu = _get(data, "whisper.compute_type_cpu", "int8")

        self.tts_enabled = bool(_get(data, "tts.enabled", True))
        self.tts_mute = bool(_get(data, "tts.mute", False))
        self.tts_voice = _get(data, "tts.voice", "en_US-amy-medium")
        self.tts_length_scale = float(_get(data, "tts.length_scale", 1.05))
        self.piper_dir = ROOT / str(_get(data, "tts.piper_dir", "models/piper"))
        self.max_sentence_chars = int(_get(data, "tts.max_sentence_chars", 280))

        self.sample_rate = int(_get(data, "audio.sample_rate", 16000))
        self.mic_device = _get(data, "audio.mic_device", None)
        self.speaker_device = _get(data, "audio.speaker_device", None)
        self.silence_ms = int(_get(data, "audio.silence_ms", 800))
        self.min_utterance_ms = int(_get(data, "audio.min_utterance_ms", 350))
        self.energy_threshold = float(_get(data, "audio.energy_threshold", 0.012))
        self.max_utterance_seconds = float(_get(data, "audio.max_utterance_seconds", 20))

        self.chat_buffer_size = int(_get(data, "chat.buffer_size", 40))
        self.cooldown_seconds = float(_get(data, "chat.cooldown_seconds", 30))
        self.max_replies_per_minute = int(_get(data, "chat.max_replies_per_minute", 4))
        self.scan_interval_seconds = float(_get(data, "chat.scan_interval_seconds", 8))
        self.mention_boost = bool(_get(data, "chat.mention_boost", True))
        self.post_voice_replies_to_chat = bool(_get(data, "chat.post_voice_replies_to_chat", True))
        self.chat_max_chars = int(_get(data, "chat.chat_max_chars", 200))

        self.spoken_turns = int(_get(data, "memory.spoken_turns", 8))
        self.chat_context_lines = int(_get(data, "memory.chat_context_lines", 20))
        self.memory_events = int(_get(data, "memory.events", 80))
        self.memory_viewers = int(_get(data, "memory.viewers", 40))
        self.per_user_cooldown_seconds = float(_get(data, "chat.per_user_cooldown_seconds", 20))
        self.auto_reconnect_twitch = bool(_get(data, "chat.auto_reconnect", True))

        self.twitch_token = os.getenv("TWITCH_TOKEN", "").strip()
        self.twitch_nick = os.getenv("TWITCH_NICK", "").strip()
        self.twitch_channel = os.getenv("TWITCH_CHANNEL", "").strip().lstrip("#")

    def reload_secrets(self) -> None:
        load_dotenv(ROOT / ".env", override=True)
        self.twitch_token = os.getenv("TWITCH_TOKEN", "").strip()
        self.twitch_nick = os.getenv("TWITCH_NICK", "").strip()
        self.twitch_channel = os.getenv("TWITCH_CHANNEL", "").strip().lstrip("#")


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    cfg_path = path or (ROOT / "config.yaml")
    with cfg_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Settings(data)
