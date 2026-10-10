from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

QUALITY_MODEL = "qwen2.5:7b-instruct-q4_K_M"
SNAPPY_MODEL = "qwen2.5:3b"
WHISPER_LANGUAGES = ("en", "es", "fr", "de", "pt", "ja")
REWARD_ACTIONS = ("speak", "chat", "ignore")


def _command_list(data: dict[str, Any], path: str, default: list[str]) -> list[str]:
    raw = _get(data, path, default) or []
    commands: list[str] = []
    for item in raw:
        text = str(item).strip().lower()
        if not text:
            continue
        if not text.startswith("!"):
            text = "!" + text
        commands.append(text)
    return commands


class RewardSpec:
    """How one channel-point reward should be delivered."""

    def __init__(self, action: str, prompt: str = "", cooldown: float = 0.0) -> None:
        cleaned = (action or "").strip().lower()
        self.action = cleaned if cleaned in REWARD_ACTIONS else "ignore"
        self.prompt = prompt or ""
        try:
            self.cooldown = max(0.0, float(cooldown or 0))
        except (TypeError, ValueError):
            self.cooldown = 0.0


def _reward_spec(value: Any) -> RewardSpec:
    if isinstance(value, RewardSpec):
        return value
    if isinstance(value, str):
        return RewardSpec(value.strip().lower())
    if isinstance(value, dict):
        try:
            cooldown = float(value.get("cooldown") or 0)
        except (TypeError, ValueError):
            cooldown = 0.0
        return RewardSpec(
            str(value.get("action") or "ignore"),
            str(value.get("prompt") or ""),
            cooldown,
        )
    return RewardSpec("ignore")


def _parse_profiles(raw: Any) -> dict[str, dict[str, Any]]:
    """Named overrides for the runtime keys a profile is allowed to change."""
    if not isinstance(raw, dict):
        return {}
    profiles: dict[str, dict[str, Any]] = {}
    for name, body in raw.items():
        if not isinstance(body, dict):
            continue
        spec: dict[str, Any] = {}
        if "mood" in body:
            spec["mood"] = str(body.get("mood") or "normal").strip().lower()
        if "cooldown_seconds" in body:
            spec["cooldown_seconds"] = float(body.get("cooldown_seconds") or 0)
        if "per_user_cooldown_seconds" in body:
            spec["per_user_cooldown_seconds"] = float(body.get("per_user_cooldown_seconds") or 0)
        events = body.get("events")
        if "events.speak" in body:
            spec["events_speak"] = bool(body.get("events.speak"))
        elif isinstance(events, dict) and "speak" in events:
            spec["events_speak"] = bool(events.get("speak"))
        elif "events_speak" in body:
            spec["events_speak"] = bool(body.get("events_speak"))
        if "post_voice_replies_to_chat" in body:
            spec["post_voice_replies_to_chat"] = bool(body.get("post_voice_replies_to_chat"))
        if "max_replies_per_minute" in body:
            spec["max_replies_per_minute"] = int(body.get("max_replies_per_minute") or 0)
        profiles[str(name)] = spec
    return profiles


def _get(data: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in {"127.0.0.1", "localhost", "::1"}


class Settings:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.root = ROOT

        self.host = str(_get(data, "server.host", "127.0.0.1"))
        self.port = int(_get(data, "server.port", 8080))
        self.allow_remote = bool(_get(data, "server.allow_remote", False))

        self.ollama_host = _get(data, "ollama.host", "http://127.0.0.1:11434").rstrip("/")
        self.ollama_model = _get(data, "ollama.model", SNAPPY_MODEL)
        self.ollama_timeout = float(_get(data, "ollama.timeout_seconds", 120))
        self.ollama_num_ctx = int(_get(data, "ollama.num_ctx", 4096))
        self.ollama_num_predict_chat = int(_get(data, "ollama.num_predict_chat", 80))
        self.ollama_num_predict_voice = int(_get(data, "ollama.num_predict_voice", 160))
        self.ollama_keep_alive = str(_get(data, "ollama.keep_alive", "30m"))
        self.chat_timeout_seconds = float(_get(data, "ollama.chat_timeout_seconds", 20))

        self.whisper_model = _get(data, "whisper.model_size", "small")
        self.whisper_device = _get(data, "whisper.device", "auto")
        self.whisper_language = _get(data, "whisper.language", "en")
        self.compute_type_cuda = _get(data, "whisper.compute_type_cuda", "float16")
        self.compute_type_cpu = _get(data, "whisper.compute_type_cpu", "int8")

        self.vram_profile = str(_get(data, "vram.profile", "snappy") or "snappy").lower()
        self.vram_warn_mb = int(_get(data, "vram.warn_mb", 7000))
        if self.vram_profile == "quality":
            self.whisper_device = "cpu"
            if self.ollama_model in (SNAPPY_MODEL, "", None):
                self.ollama_model = _get(data, "vram.quality_model", QUALITY_MODEL)

        self.tts_enabled = bool(_get(data, "tts.enabled", True))
        self.tts_mute = bool(_get(data, "tts.mute", False))
        self.tts_voice = _get(data, "tts.voice", "en_US-amy-medium")
        self.tts_length_scale = float(_get(data, "tts.length_scale", 1.05))
        self.piper_dir = ROOT / str(_get(data, "tts.piper_dir", "models/piper"))
        self.max_sentence_chars = int(_get(data, "tts.max_sentence_chars", 280))
        self.tts_voices = list(
            _get(
                data,
                "tts.voices",
                [
                    "en_US-amy-medium",
                    "en_US-kristin-medium",
                    "en_US-hfc_female-medium",
                ],
            )
            or []
        )

        self.sample_rate = int(_get(data, "audio.sample_rate", 16000))
        self.mic_device = _get(data, "audio.mic_device", None)
        self.speaker_device = _get(data, "audio.speaker_device", None)
        self.mic_device_name = _get(data, "audio.mic_device_name", None) or None
        self.speaker_device_name = _get(data, "audio.speaker_device_name", None) or None
        self.silence_ms = int(_get(data, "audio.silence_ms", 800))
        self.min_utterance_ms = int(_get(data, "audio.min_utterance_ms", 350))
        self.energy_threshold = float(_get(data, "audio.energy_threshold", 0.012))
        self.max_utterance_seconds = float(_get(data, "audio.max_utterance_seconds", 20))
        self.duck_enabled = bool(_get(data, "audio.duck_enabled", False))
        self.duck_gain = float(_get(data, "audio.duck_gain", 0.25))
        self.duck_apps = [str(item) for item in (_get(data, "audio.duck_apps", []) or []) if str(item).strip()]
        self.duck_skip = [
            str(item)
            for item in (_get(data, "audio.duck_skip", ["obs64.exe", "Discord.exe"]) or [])
            if str(item).strip()
        ]
        self.barge_in = bool(_get(data, "audio.barge_in", True))
        self.echo_gate = bool(_get(data, "audio.echo_gate", True))
        self.echo_gate_ratio = float(_get(data, "audio.echo_gate_ratio", 2.5))
        self.wake_word = bool(_get(data, "audio.wake_word", False))
        self.wake_models = list(_get(data, "audio.wake_models", ["hey_jarvis"]) or ["hey_jarvis"])
        self.wake_threshold = float(_get(data, "audio.wake_threshold", 0.5))

        self.chat_buffer_size = int(_get(data, "chat.buffer_size", 40))
        self.cooldown_seconds = float(_get(data, "chat.cooldown_seconds", 12))
        self.max_replies_per_minute = int(_get(data, "chat.max_replies_per_minute", 8))
        self.scan_interval_seconds = float(_get(data, "chat.scan_interval_seconds", 3))
        self.mention_boost = bool(_get(data, "chat.mention_boost", True))
        self.post_voice_replies_to_chat = bool(_get(data, "chat.post_voice_replies_to_chat", True))
        self.chat_max_chars = int(_get(data, "chat.chat_max_chars", 200))
        self.per_user_cooldown_seconds = float(_get(data, "chat.per_user_cooldown_seconds", 20))
        self.auto_reconnect_twitch = bool(_get(data, "chat.auto_reconnect", True))
        self.min_send_gap_seconds = float(_get(data, "chat.min_send_gap_seconds", 1.2))
        self.chat_commands = _command_list(data, "chat.commands", ["!so", "!pixel", "!ask"])
        self.mod_commands = _command_list(data, "chat.mod_commands", ["!so"])
        self.shoutout_cooldown_seconds = float(_get(data, "chat.shoutout_cooldown_seconds", 120))
        self.shoutout_line = str(_get(data, "chat.shoutout_line", "go check out {user}") or "go check out {user}")
        self.shoutout_speak = bool(_get(data, "chat.shoutout_speak", True))
        self.control_command = str(_get(data, "chat.control_command", "") or "").strip()
        self.chat_max_age_seconds = float(_get(data, "chat.max_age_seconds", 45))
        self.welcome_first_time = bool(_get(data, "chat.welcome_first_time", False))
        self.control_cooldown_seconds = float(_get(data, "chat.control_cooldown_seconds", 4))
        self.ask_queue = bool(_get(data, "chat.ask_queue", False))
        self.clip_cooldown_seconds = float(_get(data, "chat.clip_cooldown_seconds", 60))
        self.ambient_chat_context = int(_get(data, "chat.ambient_context_lines", 6))
        self.allow_links = bool(_get(data, "safety.allow_links", False))

        self.chat_context_lines = int(_get(data, "memory.chat_context_lines", 20))
        self.memory_events = int(_get(data, "memory.events", 80))
        self.memory_viewers = int(_get(data, "memory.viewers", 48))
        self.memory_thread_turns = int(_get(data, "memory.thread_turns", 12))
        self.catchphrase_seconds = float(_get(data, "memory.catchphrase_seconds", 600))
        self.retention_days = int(_get(data, "memory.retention_days", 14))

        self.events_enabled = bool(_get(data, "events.enabled", True))
        self.announce_follows = bool(_get(data, "events.announce_follows", True))
        self.follow_batch_seconds = float(_get(data, "events.follow_batch_seconds", 8))
        self.events_speak = bool(_get(data, "events.speak", True))
        self.events_post_to_chat = bool(_get(data, "events.post_to_chat", True))
        self.bits_enabled = bool(_get(data, "events.bits", False))
        self.channel_points_enabled = bool(_get(data, "events.channel_points", False))
        self.hype_train_enabled = bool(_get(data, "events.hype_train", False))
        self.polls_enabled = bool(_get(data, "events.polls", False))
        self.predictions_enabled = bool(_get(data, "events.predictions", False))
        self.ad_breaks_enabled = bool(_get(data, "events.ad_breaks", False))
        self.shoutouts_enabled = bool(_get(data, "events.shoutouts", False))
        self.idle_on_offline = bool(_get(data, "events.idle_on_offline", True))
        rewards = _get(data, "events.channel_point_rewards", {}) or {}
        self.channel_point_rewards = {
            str(key).strip().lower(): _reward_spec(value)
            for key, value in rewards.items()
            if str(key).strip()
        }
        self.profiles = _parse_profiles(_get(data, "profiles", {}) or {})

        self.overlay_alert_hold = float(_get(data, "overlay.alert_hold_seconds", 6))
        self.log_level = str(_get(data, "logging.level", "INFO") or "INFO").upper()
        self.log_debug_modules = [
            str(name) for name in (_get(data, "logging.debug_modules", []) or []) if str(name).strip()
        ]

        self.twitch_token = os.getenv("TWITCH_TOKEN", "").strip()
        self.twitch_nick = os.getenv("TWITCH_NICK", "").strip()
        self.twitch_channel = os.getenv("TWITCH_CHANNEL", "").strip().lstrip("#")
        self.twitch_client_id = os.getenv("TWITCH_CLIENT_ID", "").strip()
        self.twitch_event_token = os.getenv("TWITCH_EVENT_TOKEN", "").strip()

    def event_token(self) -> str:
        return (self.twitch_event_token or self.twitch_token).strip()

    def missing_secrets(self) -> list[str]:
        missing = []
        if not self.twitch_token:
            missing.append("TWITCH_TOKEN")
        if not self.twitch_nick:
            missing.append("TWITCH_NICK")
        if not self.twitch_channel:
            missing.append("TWITCH_CHANNEL")
        return missing

    def secret_hints(self) -> list[str]:
        hints = []
        missing = self.missing_secrets()
        if missing:
            hints.append(
                "Missing " + ", ".join(missing) + ". Copy .env.example to .env and add a Twitch chat token."
            )
        if self.events_enabled and not self.twitch_client_id:
            hints.append("Add TWITCH_CLIENT_ID (and optional TWITCH_EVENT_TOKEN) for follows/subs/raids.")
        return hints

    def reload_secrets(self) -> None:
        load_dotenv(ROOT / ".env", override=True)
        self.twitch_token = os.getenv("TWITCH_TOKEN", "").strip()
        self.twitch_nick = os.getenv("TWITCH_NICK", "").strip()
        self.twitch_channel = os.getenv("TWITCH_CHANNEL", "").strip().lstrip("#")
        self.twitch_client_id = os.getenv("TWITCH_CLIENT_ID", "").strip()
        self.twitch_event_token = os.getenv("TWITCH_EVENT_TOKEN", "").strip()

    def reward_spec(self, title: str) -> RewardSpec:
        return self.channel_point_rewards.get((title or "").strip().lower(), RewardSpec("ignore"))

    def reward_action(self, title: str) -> str:
        return self.reward_spec(title).action

    def event_subscriptions(self) -> list[tuple[str, str]]:
        subs = [
            ("channel.follow", "2"),
            ("channel.subscribe", "1"),
            ("channel.subscription.gift", "1"),
            ("channel.subscription.message", "1"),
            ("channel.raid", "1"),
        ]
        if self.bits_enabled:
            subs.append(("channel.cheer", "1"))
        if self.channel_points_enabled:
            subs.append(("channel.channel_points_custom_reward_redemption.add", "1"))
        if self.hype_train_enabled:
            # Version 2 is what Twitch's EventSub reference lists. Version 1 was
            # withdrawn on 15 Jan 2026, so "try 1" would be rejected.
            subs.extend(
                [
                    ("channel.hype_train.begin", "2"),
                    ("channel.hype_train.progress", "2"),
                    ("channel.hype_train.end", "2"),
                ]
            )
        if self.polls_enabled:
            subs.extend([("channel.poll.begin", "1"), ("channel.poll.end", "1")])
        if self.predictions_enabled:
            subs.extend([("channel.prediction.begin", "1"), ("channel.prediction.end", "1")])
        if self.ad_breaks_enabled:
            subs.append(("channel.ad_break.begin", "1"))
        if self.shoutouts_enabled:
            subs.extend(
                [
                    ("channel.shoutout.receive", "1"),
                    ("channel.shoutout.create", "1"),
                    ("channel.raid.out", "1"),
                ]
            )
        subs.extend([("stream.online", "1"), ("stream.offline", "1")])
        return subs


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    cfg_path = path or (ROOT / "config.yaml")
    with cfg_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Settings(data)


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return str(value)


def atomic_write_text(path: Path, text: str, *, backup: bool = False) -> None:
    """Write ``text`` via a temp file. Optionally copy the current file to ``name.bak``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def update_config(updates: dict[str, Any], path: Path | None = None) -> None:
    """Update existing scalar keys in config.yaml without dropping comments."""
    cfg_path = path or (ROOT / "config.yaml")
    raw = cfg_path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    lines = raw.decode("utf-8").splitlines()
    for dotted, value in updates.items():
        if "." not in dotted:
            continue
        section, key = dotted.split(".", 1)
        lines = _set_in_section(lines, section, key, _yaml_scalar(value))
    atomic_write_text(cfg_path, newline.join(lines) + newline, backup=True)


def _set_in_section(lines: list[str], section: str, key: str, rendered: str) -> list[str]:
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{section}:"):
            start = i
            break
    if start is None:
        lines.extend(["", f"{section}:", f"  {key}: {rendered}"])
        return lines
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j] and not lines[j].startswith((" ", "#", "\t")):
            end = j
            break
    pattern = re_key(key)
    for j in range(start + 1, end):
        if pattern.match(lines[j]):
            indent = lines[j][: len(lines[j]) - len(lines[j].lstrip(" "))]
            lines[j] = f"{indent}{key}: {rendered}"
            return lines
    lines.insert(end, f"  {key}: {rendered}")
    return lines


def re_key(key: str):
    import re

    return re.compile(rf"^\s*{re.escape(key)}\s*:")


def assert_bind_allowed(host: str, allow_remote: bool) -> None:
    if is_loopback(host) or allow_remote:
        return
    raise RuntimeError(
        f"Refusing to bind {host}. The booth has no auth. "
        "Stay on 127.0.0.1, or set server.allow_remote: true if you mean to expose it."
    )
