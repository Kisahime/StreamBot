from __future__ import annotations

import re

_ABBREV = re.compile(
    r"(?i)(?:^|[\s(])(mr|mrs|ms|dr|st|jr|sr|vs|etc|prof|gen|col|lt|sgt|capt|rev)\.$"
)


def _is_abbrev(head: str) -> bool:
    return bool(_ABBREV.search(head.strip()))


def take_sentence(buf: str, *, flush: bool = False) -> tuple[str, str] | None:
    """Split one sentence without cutting abbreviations or ellipses.

    Returns (sentence, rest) or None if more text is needed.
    A trailing em dash is not a boundary.
    """
    if not buf:
        return None
    i = 0
    while i < len(buf):
        ch = buf[i]
        if ch in ".!?":
            if buf[i : i + 3] == "...":
                j = i + 3
                if j < len(buf) and buf[j].isspace():
                    sent = buf[:j].strip()
                    if sent:
                        return sent, buf[j:].lstrip()
                i = j
                continue
            if ch == "." and _is_abbrev(buf[: i + 1]):
                i += 1
                continue
            nxt = i + 1
            if nxt < len(buf) and buf[nxt].isspace():
                sent = buf[:nxt].strip()
                if sent:
                    return sent, buf[nxt:].lstrip()
            if nxt >= len(buf) and flush:
                sent = buf.strip()
                if sent:
                    return sent, ""
        i += 1
    if flush:
        tail = buf.strip()
        if tail:
            return tail, ""
    return None


def cap_sentence(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.strip()
