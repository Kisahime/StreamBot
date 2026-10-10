from __future__ import annotations

import re

from src.sentences import cap_sentence

DEFLECT = "nah I'm leaving that one. anyway."

# Requests the persona already refuses. Matched locally so the model is not asked to invent a refusal.
_ASK = re.compile(
    r"(?i)\b("
    r"diagnos(e|is|ing)|what meds|should i take|prescri(be|ption)|dosage|"
    r"legal advice|should i sue|lawsuit|"
    r"who should i vote|political (take|opinion)|vote for|"
    r"real-world politics"
    r")\b"
)

# A short backstop. The model prompt is not the only gate.
_SLUR = re.compile(
    r"(?i)\b("
    r"nigger|nigga|faggot|retard|tranny|kike"
    r")\b"
)


def is_blocked(text: str) -> bool:
    raw = text or ""
    if _SLUR.search(raw):
        return True
    return bool(_ASK.search(raw))


_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_MENTION = re.compile(r"(?<!\w)@([A-Za-z0-9_]+)")
_LEADING_COMMAND = "!/.\\"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def fence_viewer_text(text: str, limit: int = 300) -> str:
    """Wrap text a person typed so the model can see it is not an instruction."""
    raw = _CONTROL.sub("", text or "")
    raw = raw.replace("<<", "‹‹").replace(">>", "››")
    lines = [line[:limit] for line in raw.splitlines()] or [""]
    body = "\n".join(lines).strip()
    return f"<<viewer>>\n{body}\n<</viewer>>"


def clean_chat_output(
    text: str,
    target_user: str | None = None,
    *,
    allow_links: bool = False,
    max_chars: int = 200,
) -> str:
    """Make a model line safe to post in Twitch chat.

    Strips a leading command character, drops links unless allowed, drops
    @-mentions other than ``target_user``, and caps on a word boundary.
    Returns ``""`` when nothing meaningful is left.
    """
    raw = (text or "").strip()
    raw = raw.lstrip(_LEADING_COMMAND).strip()
    if not allow_links:
        raw = _URL.sub(" ", raw)
    target = (target_user or "").lstrip("@").lower()

    def _keep_mention(match: re.Match[str]) -> str:
        name = match.group(1)
        if target and name.lower() == target:
            return match.group(0)
        return " "

    raw = _MENTION.sub(_keep_mention, raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = cap_sentence(raw, max_chars)
    if not re.search(r"[A-Za-z0-9]", raw):
        return ""
    return raw
