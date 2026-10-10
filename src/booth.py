from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class BoothState:
    listen: bool = False
    tts_mute: bool = False
    post_voice_replies_to_chat: bool = True
    mood: str = "normal"
    profile: str = ""

    def clamped(self) -> BoothState:
        if self.mood not in ("chill", "normal", "hype"):
            self.mood = "normal"
        return self


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
        except Exception:
            pass
    return state.clamped()


def save_booth(path: Path, state: BoothState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state.clamped()), ensure_ascii=False, indent=2), encoding="utf-8")
