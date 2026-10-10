from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.memory import ConversationMemory


def _section(title: str, lines: list[str]) -> str:
    body = "\n".join(f"- {line}" for line in lines) if lines else "- none this session"
    return f"## {title}\n{body}"


def _session_event(memory: ConversationMemory, event) -> bool:
    return event.session_id == memory.session_id


def build_recap(memory: ConversationMemory, *, vibe: str = "", when: datetime | None = None) -> str:
    """Deterministic Markdown recap for the current session. ``vibe`` is optional."""
    follows: list[str] = []
    subs: list[str] = []
    raids: list[str] = []
    cheers: list[str] = []
    redeems: list[str] = []
    polls: list[str] = []
    for event in memory.events:
        if event.kind != "twitch_event" or not _session_event(memory, event):
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
        elif "cheer" in low or "bits" in low:
            cheers.append(text)
        elif "redeem" in low:
            redeems.append(text)
        elif "poll" in low or "prediction" in low:
            polls.append(text)

    counts: dict[str, tuple[int, str]] = {}
    for event in memory.events:
        if event.kind != "viewer" or not _session_event(memory, event):
            continue
        name = (event.user or "").strip()
        if not name:
            continue
        count, shown = counts.get(name.lower(), (0, name))
        counts[name.lower()] = (count + 1, shown)
    chatters = sorted(counts.values(), key=lambda item: (-item[0], item[1].lower()))
    chatter_lines = [f"{name} — {count} message{'s' if count != 1 else ''}" for count, name in chatters[:8]]

    moments: list[str] = []
    for key, viewer in memory.viewers.items():
        if not viewer.notes:
            continue
        who = viewer.name or key
        for note in viewer.notes:
            moments.append(f"{who}: {note}")

    stamp = (when or datetime.now()).strftime("%Y-%m-%d")
    parts = [
        f"# Stream recap {stamp}",
        _section("Follows", follows),
        _section("Subs", subs),
        _section("Raids", raids),
        _section("Top chatters", chatter_lines),
        _section("Noted moments", moments),
    ]
    if cheers:
        parts.append(_section("Cheers", cheers))
    if redeems:
        parts.append(_section("Redeems", redeems))
    if polls:
        parts.append(_section("Polls", polls))
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
    filename: str | None = None,
) -> tuple[str, Path]:
    moment = when or datetime.now()
    text = build_recap(memory, vibe=vibe, when=moment)
    directory.mkdir(parents=True, exist_ok=True)
    name = Path(filename).name if filename else f"{moment:%Y-%m-%d}.md"
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return text, path
