from __future__ import annotations

from src.twitch.chat import ChatLine

SPEAK_COMMANDS = {"!pixel", "!ask"}
CONTROL_VERBS = {"mute", "unmute", "chill", "hype", "stop", "clip"}


def control_command_names(persona_name: str = "", configured: str = "") -> set[str]:
    """Commands that steer the booth. ``!pixel`` stays available as an alias."""
    names = {"!pixel"}
    raw = (configured or "").strip().lower()
    if raw:
        names.add(raw if raw.startswith("!") else f"!{raw}")
        return names
    first = (persona_name or "").strip().lower().split()
    if first and first[0]:
        names.add(f"!{first[0]}")
    return names


def pixel_control(line: ChatLine, commands: set[str] | None = None) -> str:
    """Return a steering verb, or empty when this line is not a control command."""
    allowed = commands or {"!pixel"}
    if (line.command or "").lower() not in allowed:
        return ""
    parts = (line.text or "").split()
    if len(parts) < 2:
        return ""
    verb = parts[1].lower()
    return verb if verb in CONTROL_VERBS else ""


def chat_action(line: ChatLine, *, welcome_first_time: bool = False) -> str:
    """Return speak, chat, command, control, or skip.

    Mentions, questions, and greetings are spoken.
    Ambient lines are typed only.
    A first-time chatter is typed, with the welcome hint added by the caller.
    Allowlisted commands are handled by the caller.
    """
    if pixel_control(line):
        return "control"
    cmd = (line.command or "").lower()
    if cmd == "!so":
        return "command"
    if welcome_first_time and line.first_msg and not cmd:
        return "chat"
    if cmd in SPEAK_COMMANDS or line.mention or line.is_engaging():
        return "speak"
    if cmd:
        return "command"
    return "chat"


def custom_reply(line: ChatLine, commands: dict[str, str] | None) -> str:
    """Filled fixed-text reply for a custom command, or empty when this line is not one."""
    table = commands or {}
    cmd = (line.command or "").lower()
    if not cmd:
        return ""
    template = table.get(cmd)
    if template is None:
        return ""
    text = str(template).strip()
    if not text:
        return ""
    return text.replace("{user}", line.user or "chat")


def mention_waits(mention: bool, mention_boost: bool, cooldown: float, per_user: float) -> tuple[float, float]:
    """Global cooldown and per-user cooldown for this line."""
    if mention and mention_boost:
        return min(4.0, cooldown), min(8.0, per_user)
    return cooldown, per_user
