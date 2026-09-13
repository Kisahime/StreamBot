from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

AT_RE = re.compile(r"@([A-Za-z0-9_]+)")
BROADCAST_RE = re.compile(r"(?i)^(hey |hi )?(chat|everyone|folks|guys|all)\b")
JSON_RE = re.compile(r"\{.*\}", re.S)
META_LINE = re.compile(
    r"(?is)^\s*(?:action|reply|message|output|response|type|mode|chatreply|chat.?reply)"
    r"\s*:\s*[a-z0-9_\-\s]*[:\-]?\s*"
)
GENERIC_BOT = re.compile(
    r"(?i)\b(how can i assist you( today)?|how can i help you( today)?|"
    r"what can i (do|help) for you|as an ai|i'm here to help)\b"
)


def sanitize_reply(text: str, bot_name: str = "") -> str:
    """Strip JSON, action labels, and other model junk from a chat line."""
    try:
        return _sanitize_reply(text, bot_name)
    except re.error:
        return re.sub(r"\s+", " ", (text or "").strip())


def _sanitize_reply(text: str, bot_name: str = "") -> str:
    """Strip JSON, action labels, and other model junk from a chat line."""
    text = (text or "").strip().strip('"').strip("'")
    text = re.sub(r"^```(?:json|txt|markdown)?\s*|\s*```$", "", text, flags=re.I | re.M).strip()
    match = JSON_RE.search(text)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, dict):
                text = str(data.get("reply") or data.get("message") or data.get("text") or "")
        except json.JSONDecodeError:
            pass
    for _ in range(6):
        nxt = META_LINE.sub("", text).strip()
        if nxt == text:
            break
        text = nxt
    cleaned_lines = []
    for ln in text.splitlines():
        if META_LINE.match(ln.strip()):
            continue
        cleaned_lines.append(ln.strip())
    text = " ".join(cleaned_lines).strip()
    if bot_name:
        text = re.sub(
            rf"^@?{re.escape(bot_name)}\s*[:\-]\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def is_generic_filler(text: str) -> bool:
    return bool(GENERIC_BOT.search(text or ""))


def ensure_chat_mention(text: str, user: str) -> str:
    """Prefix @user when the line is for one viewer and isn't already tagged."""
    user = (user or "").lstrip("@").strip()
    text = (text or "").strip()
    if not user or not text:
        return text
    if BROADCAST_RE.match(text):
        return text
    if re.search(rf"@{re.escape(user)}\b", text, flags=re.IGNORECASE):
        return text
    return f"@{user} {text}"


def for_speech(text: str) -> str:
    """TTS should say names, not the @ symbol."""
    return AT_RE.sub(r"\1", text or "").strip()


@dataclass
class Event:
    ts: float
    kind: str
    user: str
    text: str

    def line(self) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.ts))
        if self.kind == "streamer":
            who = "Streamer"
        elif self.kind == "viewer":
            who = self.user
        elif self.kind == "cohost_voice":
            who = "You (voice)"
        else:
            who = "You (chat)"
        return f"[{stamp}] {who}: {self.text}"


@dataclass
class Viewer:
    name: str
    messages: int = 0
    last_said: str = ""
    last_reply: str = ""
    last_ts: float = 0.0
    notes: list[str] = field(default_factory=list)


class ConversationMemory:
    def __init__(self, path: Path, event_limit: int = 80, viewer_limit: int = 40) -> None:
        self.path = path
        self.event_limit = event_limit
        self.viewer_limit = viewer_limit
        self.events: deque[Event] = deque(maxlen=event_limit)
        self.viewers: dict[str, Viewer] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for item in data.get("events") or []:
            try:
                self.events.append(Event(**item))
            except Exception:
                continue
        for key, item in (data.get("viewers") or {}).items():
            try:
                self.viewers[key] = Viewer(**item)
            except Exception:
                continue

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "events": [asdict(e) for e in self.events],
            "viewers": {k: asdict(v) for k, v in self.viewers.items()},
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self) -> None:
        self.events.clear()
        self.viewers.clear()
        self.save()

    def remember(self, kind: str, user: str, text: str) -> Event:
        ev = Event(ts=time.time(), kind=kind, user=user or "", text=(text or "").strip())
        self.events.append(ev)
        if kind == "viewer" and ev.user:
            key = ev.user.lower()
            viewer = self.viewers.get(key) or Viewer(name=ev.user)
            viewer.name = ev.user
            viewer.messages += 1
            viewer.last_said = ev.text
            viewer.last_ts = ev.ts
            self.viewers[key] = viewer
            self._trim_viewers()
        elif kind.startswith("cohost") and ev.text:
            mentioned = AT_RE.findall(ev.text)
            targets = [m.lower() for m in mentioned] or []
            for key in targets:
                if key in self.viewers:
                    self.viewers[key].last_reply = ev.text
                    self.viewers[key].last_ts = ev.ts
        self.save()
        return ev

    def note_reply_to(self, user: str, reply: str) -> None:
        key = (user or "").lower()
        if not key:
            return
        viewer = self.viewers.get(key) or Viewer(name=user)
        viewer.last_reply = reply
        viewer.last_ts = time.time()
        self.viewers[key] = viewer
        self.save()

    def viewer(self, name: str) -> Viewer | None:
        return self.viewers.get((name or "").lower())

    def prompt_block(self, focus_user: str | None = None, event_n: int = 24) -> str:
        recent = list(self.events)[-event_n:]
        lines = [e.line() for e in recent] or ["(no conversation yet)"]
        block = "Conversation so far (remember who said what; stay consistent):\n" + "\n".join(lines)
        if focus_user:
            v = self.viewer(focus_user)
            if v:
                block += (
                    f"\n\nAbout {v.name}: {v.messages} chat messages this session. "
                    f"They last said: {v.last_said or '(nothing)'}. "
                    f"You last replied: {v.last_reply or '(nothing yet)'}."
                )
        else:
            active = sorted(self.viewers.values(), key=lambda x: x.last_ts, reverse=True)[:6]
            if active:
                bits = []
                for v in active:
                    bits.append(f"{v.name} last said “{v.last_said[:80]}”")
                block += "\n\nActive chatters: " + "; ".join(bits)
        return block

    def snapshot(self) -> dict[str, Any]:
        recent = [e.line() for e in list(self.events)[-30:]]
        people = [
            {
                "name": v.name,
                "messages": v.messages,
                "last_said": v.last_said,
                "last_reply": v.last_reply,
            }
            for v in sorted(self.viewers.values(), key=lambda x: x.last_ts, reverse=True)[:12]
        ]
        return {"events": recent, "viewers": people, "event_count": len(self.events)}

    def _trim_viewers(self) -> None:
        if len(self.viewers) <= self.viewer_limit:
            return
        ranked = sorted(self.viewers.items(), key=lambda kv: kv[1].last_ts)
        for key, _ in ranked[: max(0, len(self.viewers) - self.viewer_limit)]:
            self.viewers.pop(key, None)
