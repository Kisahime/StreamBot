from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.memory import STREAMER_THREAD, ConversationMemory


def _section(title: str, lines: list[str]) -> str:
    body = "\n".join(f"- {line}" for line in lines) if lines else "- none this session"
    return f"## {title}\n{body}"


def build_recap(memory: ConversationMemory, *, vibe: str = "", when: datetime | None = None) -> str:
    """Deterministic Markdown recap. ``vibe`` is an optional one-paragraph line."""
    follows: list[str] = []
    subs: list[str] = []
    raids: list[str] = []
    for event in memory.events:
        if event.kind != "twitch_event":
            continue
        text = (event.text or "").strip()
        if not text:
            continue
        low = text.lower()
        if "raid" in low:
            raids.append(text)
        elif "sub" in low or "gift" in low:
            subs.append(text)
        elif "follow" in low:
            follows.append(text)

    chatters: list[tuple[int, str]] = []
    moments: list[str] = []
    for key, viewer in memory.viewers.items():
        if viewer.notes:
            who = viewer.name or key
            for note in viewer.notes:
                moments.append(f"{who}: {note}")
        if key == STREAMER_THREAD or viewer.is_streamer:
            continue
        if viewer.messages:
            chatters.append((viewer.messages, viewer.name or key))
    chatters.sort(key=lambda item: (-item[0], item[1].lower()))
    chatter_lines = [f"{name} — {count} message{'s' if count != 1 else ''}" for count, name in chatters[:8]]

    stamp = (when or datetime.now()).strftime("%Y-%m-%d")
    parts = [
        f"# Stream recap {stamp}",
        _section("Follows", follows),
        _section("Subs", subs),
        _section("Raids", raids),
        _section("Top chatters", chatter_lines),
        _section("Noted moments", moments),
    ]
    paragraph = (vibe or "").strip()
    if paragraph:
        parts.append(f"## Vibe\n{paragraph}")
    return "\n\n".join(parts) + "\n"


def write_recap(
    memory: ConversationMemory,
    directory: Path,
    *,
    vibe: str = "",
    when: datetime | None = None,
) -> tuple[str, Path]:
    moment = when or datetime.now()
    text = build_recap(memory, vibe=vibe, when=moment)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{moment:%Y-%m-%d}.md"
    path.write_text(text, encoding="utf-8")
    return text, path
