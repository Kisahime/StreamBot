from __future__ import annotations

from src.twitch.chat import ChatLine

SPEAK_COMMANDS = {"!pixel", "!ask"}


def chat_action(line: ChatLine) -> str:
    """Return speak, chat, command, or skip.

    Mentions, questions, and greetings are spoken.
    Ambient lines are typed only.
    Allowlisted commands are handled by the caller.
    """
    cmd = (line.command or "").lower()
    if cmd == "!so":
        return "command"
    if cmd in SPEAK_COMMANDS or line.mention or line.is_engaging():
        return "speak"
    if cmd:
        return "command"
    return "chat"


def mention_waits(mention: bool, mention_boost: bool, cooldown: float, per_user: float) -> tuple[float, float]:
    """Global cooldown and per-user cooldown for this line."""
    if mention and mention_boost:
        return min(4.0, cooldown), min(8.0, per_user)
    return cooldown, per_user
