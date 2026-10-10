from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from src.settings import atomic_write_text

log = logging.getLogger("cohost.booth")

MOODS = ("chill", "normal", "hype")


@dataclass
class BoothState:
    listen: bool = False
    tts_mute: bool = False
    post_voice_replies_to_chat: bool = True
    mood: str = "normal"
    profile: str = ""
    mood_override: str = ""

    def clamped(self) -> BoothState:
        if self.mood not in MOODS:
            self.mood = "normal"
        if self.mood_override not in ("", *MOODS):
            self.mood_override = ""
        return self

    def effective_mood(self) -> str:
        """Manual override wins until the next profile switch."""
        return self.mood_override or self.mood


def _preserve_bad(path: Path) -> None:
    bad = path.with_name(path.name + ".bad")
    try:
        shutil.copy2(path, bad)
    except Exception:
        log.exception("could not copy corrupt %s", path.name)
    log.error("%s is unreadable; kept a copy at %s", path.name, bad.name)


def load_booth(path: Path, fallback_mute: bool = False, fallback_post: bool = True) -> BoothState:
    state = BoothState(tts_mute=fallback_mute, post_voice_replies_to_chat=fallback_post)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            state.listen = bool(data.get("listen", state.listen))
            state.tts_mute = bool(data.get("tts_mute", state.tts_mute))
            state.post_voice_replies_to_chat = bool(
                data.get("post_voice_replies_to_chat", state.post_voice_replies_to_chat)
            )
            state.mood = str(data.get("mood") or state.mood)
            state.profile = str(data.get("profile") or "")
            state.mood_override = str(data.get("mood_override") or "")
        except Exception:
            log.exception("booth.json is unreadable")
            _preserve_bad(path)
    return state.clamped()


def save_booth(path: Path, state: BoothState) -> None:
    body = json.dumps(asdict(state.clamped()), ensure_ascii=False, indent=2)
    atomic_write_text(path, body + "\n")
