"""Hermetic pytest setup.

Sounddevice and faster-whisper are stubbed at import time, before test modules
import ``src``. A machine with no audio device and no model weights can still
collect and run the suite. Twitch credentials are never read from ``.env``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PERSONA_FIXTURE = ROOT / "tests" / "fixtures" / "persona.yaml"

FAKE_TWITCH_ENV = {
    "TWITCH_TOKEN": "oauth:fake-token-not-real",
    "TWITCH_NICK": "pixelbot",
    "TWITCH_CHANNEL": "testchannel",
    "TWITCH_CLIENT_ID": "fake-client-id",
    "TWITCH_EVENT_TOKEN": "oauth:fake-event-token",
}


class _Stream:
    started = 0

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs

    def start(self) -> None:
        type(self).started += 1

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None

    def read(self, frames: int):  # noqa: ARG002
        return b""

    def write(self, data) -> None:  # noqa: ARG002
        return None


def install_sounddevice_stub() -> types.ModuleType:
    """Replace ``sounddevice`` so imports never open PortAudio."""
    existing = sys.modules.get("sounddevice")
    if existing is not None and getattr(existing, "_cohost_stub", False):
        return existing

    def query_devices():
        return []

    def query_hostapis(index=None):
        apis = [{"name": "stub", "devices": []}]
        if index is None:
            return apis
        return apis[index]

    def play(*args, **kwargs) -> None:  # noqa: ARG001
        return None

    def stop(*args, **kwargs) -> None:  # noqa: ARG001
        return None

    def wait(*args, **kwargs) -> None:  # noqa: ARG001
        return None

    mod = types.ModuleType("sounddevice")
    mod.InputStream = _Stream
    mod.OutputStream = _Stream
    mod.RawInputStream = _Stream
    mod.RawOutputStream = _Stream
    mod.query_devices = query_devices
    mod.query_hostapis = query_hostapis
    mod.play = play
    mod.stop = stop
    mod.wait = wait
    mod.default = types.SimpleNamespace(device=(None, None), samplerate=(16000, 22050))
    mod._cohost_stub = True
    sys.modules["sounddevice"] = mod
    return mod


def install_faster_whisper_stub() -> types.ModuleType:
    """Replace ``faster_whisper`` so a test cannot download or load a model."""
    existing = sys.modules.get("faster_whisper")
    if existing is not None and getattr(existing, "_cohost_stub", False):
        return existing

    class WhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

        def transcribe(self, *args, **kwargs):  # noqa: ARG002
            return iter(()), None

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = WhisperModel
    mod._cohost_stub = True
    sys.modules["faster_whisper"] = mod
    return mod


# Pytest imports this file before it imports test modules. The stubs have to be
# in sys.modules now; a fixture would run only after those imports.
SOUNDDEVICE_STUB = install_sounddevice_stub()
FASTER_WHISPER_STUB = install_faster_whisper_stub()


@pytest.fixture
def sounddevice_stub() -> types.ModuleType:
    return install_sounddevice_stub()


@pytest.fixture
def faster_whisper_stub() -> types.ModuleType:
    return install_faster_whisper_stub()


@pytest.fixture(autouse=True)
def fake_twitch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests off a real ``.env`` and off any tokens in the process environment."""
    for key, value in FAKE_TWITCH_ENV.items():
        monkeypatch.setenv(key, value)
    import src.settings as settings_mod

    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *args, **kwargs: False)


@pytest.fixture
def settings(tmp_path: Path, fake_twitch_env: None) -> object:
    """In-memory settings. Tokens come from the fake env, not from ``.env``."""
    from src.settings import Settings

    data = {
        "server": {"host": "127.0.0.1", "port": 8080, "allow_remote": False},
        "ollama": {
            "host": "http://127.0.0.1:11434",
            "model": "qwen2.5:3b",
            "timeout_seconds": 5,
        },
        "whisper": {"model_size": "tiny", "device": "cpu", "language": "en"},
        "vram": {"profile": "snappy"},
        "tts": {
            "enabled": True,
            "mute": False,
            "voice": "en_US-amy-medium",
            "piper_dir": str(tmp_path / "piper"),
        },
        "audio": {
            "mic_device": None,
            "speaker_device": None,
            "duck_enabled": False,
            "wake_word": False,
        },
        "chat": {
            "cooldown_seconds": 12,
            "per_user_cooldown_seconds": 20,
            "mention_boost": True,
            "max_replies_per_minute": 8,
            "post_voice_replies_to_chat": True,
            "chat_max_chars": 200,
            "commands": ["!so", "!pixel", "!ask"],
        },
        "events": {"enabled": False, "speak": True, "post_to_chat": True},
    }
    built = Settings(data)
    assert built.twitch_token == FAKE_TWITCH_ENV["TWITCH_TOKEN"]
    assert built.twitch_nick == FAKE_TWITCH_ENV["TWITCH_NICK"]
    assert built.twitch_channel == FAKE_TWITCH_ENV["TWITCH_CHANNEL"]
    assert built.piper_dir == tmp_path / "piper"
    return built
