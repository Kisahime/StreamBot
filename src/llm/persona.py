from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.settings import ROOT


def load_persona(path: Path | None = None) -> dict[str, Any]:
    p = path or (ROOT / "persona.yaml")
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def _bullets(items: Any) -> str:
    if isinstance(items, str) and items.strip():
        return items.strip()
    return "\n".join(f"- {q}" for q in (items or []) if q)


def _examples(persona: dict[str, Any]) -> str:
    examples = persona.get("examples") or []
    lines = []
    for e in examples:
        if not isinstance(e, dict):
            continue
        lines.append(f'Them: {e.get("user")}\nYou: {e.get("assistant")}')
    return "\n\n".join(lines)


def _shared_body(persona: dict[str, Any]) -> str:
    name = persona.get("name", "Pixel")
    rel = (persona.get("relationship") or "").strip()
    speech = _bullets(persona.get("speech"))
    quirks = _bullets(persona.get("quirks"))
    never = _bullets(persona.get("never_say") or persona.get("banned_phrases"))
    banned = _bullets(persona.get("banned_topics"))
    phrases = _bullets(persona.get("catchphrases"))
    examples = _examples(persona)
    style = _bullets(persona.get("style_rules"))
    return f"""You're {name}. Pronouns {persona.get("pronouns", "they/them")}.
{persona.get("tone") or "warm, short, a little sarcastic"}.

{rel}

How you talk:
{speech}

Quirks:
{quirks}

Style:
{style or "- short, spoken, kind"}

Do not discuss: {banned or "politics, medical/legal advice, private viewer data"}

Never sound like a chatbot. Never say things like:
{never or "- how can I help"}

Catchphrases only if they fit, almost never:
{phrases or "(none)"}

Voice to copy:
{examples}
"""


def build_voice_prompt(persona: dict[str, Any], streamer_name: str = "the streamer") -> str:
    return f"""{_shared_body(persona)}
You're live on stream, on the couch next to {streamer_name}. Mic audio from them is them — not chat.
Chatters are other people in the room. Don't mix them up. You can't see the game unless they describe it.
When they talk into the mic, react out loud to {streamer_name}. Words only: no stage directions, no emojis, no @handles, no JSON, no labels.
Don't recap their sentence. Don't offer help. Don't introduce yourself unless they asked you to say hi.
"""


def build_chat_prompt(persona: dict[str, Any], streamer_name: str = "the streamer") -> str:
    name = persona.get("name", "Pixel")
    return f"""{_shared_body(persona)}
You're typing in Twitch chat as {name}. {streamer_name} is the streamer; everyone else is a viewer.
Write like chat, not like email. One short line. No markdown, no JSON, no action/reply labels.
Answer the message you were just given. Do not interview them, and do not ask what they want to talk about.
Only viewers get an @name. Never open a line to {streamer_name} with "Hey {streamer_name}".
Don't greet people you already greeted. Don't introduce yourself every time.
"""


def build_system_prompt(persona: dict[str, Any], streamer_name: str = "the streamer") -> str:
    return build_voice_prompt(persona, streamer_name)


MOOD_LINES = {
    "chill": "Energy: low. Quieter, fewer jokes, still warm.",
    "normal": "",
    "hype": "Energy: up. A little louder, still one or two sentences.",
}

MOOD_TEMP = {"chill": 0.55, "normal": 0.85, "hype": 0.95}


def mood_addon(mood: str) -> str:
    return MOOD_LINES.get(mood, "")


def mood_temperature(mood: str) -> float:
    return MOOD_TEMP.get(mood, 0.85)
