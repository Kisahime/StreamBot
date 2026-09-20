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
    r"(?i)\b("
    r"how can i assist you( today)?|how can i help you( today)?|"
    r"what can i (do|help) for you|as an ai|i('m| am) here to help|"
    r"great question|that'?s a great point|i'?d be happy to|"
    r"let me know if( you)?|i don'?t have feelings|"
    r"absolutely|of course"
    r")\b"
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
    via: str = "chat"
    thread: str = ""

    def line(self) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.ts))
        kind = self.kind
        if kind in ("streamer", "streamer_voice"):
            who = f"{self.user} (mic)"
        elif kind == "streamer_chat":
            who = f"{self.user} (chat)"
        elif kind == "viewer":
            who = self.user
        elif kind == "cohost_voice":
            who = "You (voice)"
        elif kind == "twitch_event":
            who = f"alert {self.user}".strip()
        else:
            who = "You (chat)"
        return f"[{stamp}] {who}: {self.text}"


@dataclass
class Turn:
    ts: float
    who: str
    text: str
    via: str
    role: str

    def line(self) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.ts))
        tag = "mic" if self.via == "voice" and self.role == "streamer" else self.via
        return f"[{stamp}] {self.who} ({tag}): {self.text}"


@dataclass
class Viewer:
    name: str
    messages: int = 0
    last_said: str = ""
    last_reply: str = ""
    last_ts: float = 0.0
    notes: list[str] = field(default_factory=list)
    is_streamer: bool = False


STREAMER_THREAD = "streamer"


class ConversationMemory:
    def __init__(
        self,
        path: Path,
        event_limit: int = 80,
        viewer_limit: int = 40,
        thread_turns: int = 12,
    ) -> None:
        self.path = path
        self.event_limit = event_limit
        self.viewer_limit = viewer_limit
        self.thread_turns = thread_turns
        self.events: deque[Event] = deque(maxlen=event_limit)
        self.viewers: dict[str, Viewer] = {}
        self.threads: dict[str, deque[Turn]] = {}
        self.streamer_display = "Streamer"
        self.load()

    def set_streamer_display(self, name: str) -> None:
        self.streamer_display = (name or "Streamer").strip() or "Streamer"

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for item in data.get("events") or []:
            try:
                self.events.append(Event(**{k: item[k] for k in item if k in Event.__dataclass_fields__}))
            except Exception:
                continue
        for key, item in (data.get("viewers") or {}).items():
            try:
                allowed = {k: item[k] for k in item if k in Viewer.__dataclass_fields__}
                self.viewers[key] = Viewer(**allowed)
            except Exception:
                continue
        for key, turns in (data.get("threads") or {}).items():
            dq: deque[Turn] = deque(maxlen=self.thread_turns)
            for item in turns or []:
                try:
                    dq.append(Turn(**{k: item[k] for k in item if k in Turn.__dataclass_fields__}))
                except Exception:
                    continue
            self.threads[key] = dq

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "events": [asdict(e) for e in self.events],
            "viewers": {k: asdict(v) for k, v in self.viewers.items()},
            "threads": {k: [asdict(t) for t in v] for k, v in self.threads.items()},
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self) -> None:
        self.events.clear()
        self.viewers.clear()
        self.threads.clear()
        self.save()

    def _thread(self, key: str) -> deque[Turn]:
        if key not in self.threads:
            self.threads[key] = deque(maxlen=self.thread_turns)
        return self.threads[key]

    @staticmethod
    def viewer_thread_key(login: str) -> str:
        return f"viewer:{(login or '').lower()}"

    def remember(
        self,
        kind: str,
        user: str,
        text: str,
        via: str = "chat",
        thread: str | None = None,
    ) -> Event:
        text = (text or "").strip()
        user = user or ""
        if thread is None:
            if kind.startswith("streamer"):
                thread = STREAMER_THREAD
            elif kind == "viewer":
                thread = self.viewer_thread_key(user)
            elif kind == "cohost_voice":
                thread = STREAMER_THREAD
            elif kind == "twitch_event" and user:
                thread = self.viewer_thread_key(user)
            else:
                thread = STREAMER_THREAD
        ev = Event(ts=time.time(), kind=kind, user=user, text=text, via=via, thread=thread)
        self.events.append(ev)

        if kind.startswith("streamer"):
            v = self.viewers.get(STREAMER_THREAD) or Viewer(name=user or self.streamer_display, is_streamer=True)
            v.name = user or self.streamer_display
            v.is_streamer = True
            v.messages += 1
            v.last_said = text
            v.last_ts = ev.ts
            self.viewers[STREAMER_THREAD] = v
            role = "streamer"
        elif kind in ("viewer", "twitch_event") and user:
            key = user.lower()
            v = self.viewers.get(key) or Viewer(name=user)
            v.name = user
            v.messages += 1
            v.last_said = text
            v.last_ts = ev.ts
            self.viewers[key] = v
            self._trim_viewers()
            role = "viewer"
            key = user.lower()
            v = self.viewers.get(key) or Viewer(name=user)
            v.name = user
            v.messages += 1
            v.last_said = text
            v.last_ts = ev.ts
            self.viewers[key] = v
            self._trim_viewers()
            role = "viewer"
        else:
            role = "cohost"
            if kind.startswith("cohost"):
                mentioned = [m.lower() for m in AT_RE.findall(text)]
                for key in mentioned:
                    if key in self.viewers:
                        self.viewers[key].last_reply = text
                        self.viewers[key].last_ts = ev.ts
                if thread == STREAMER_THREAD and STREAMER_THREAD in self.viewers:
                    self.viewers[STREAMER_THREAD].last_reply = text
                    self.viewers[STREAMER_THREAD].last_ts = ev.ts

        self._thread(thread).append(
            Turn(ts=ev.ts, who=user or ("Pixel" if role == "cohost" else self.streamer_display), text=text, via=via, role=role)
        )
        self.save()
        return ev

    def note_reply_to(self, user: str, reply: str, thread: str | None = None) -> None:
        key = STREAMER_THREAD if thread == STREAMER_THREAD else (user or "").lower()
        if not key:
            return
        viewer = self.viewers.get(key) or Viewer(name=user or self.streamer_display, is_streamer=key == STREAMER_THREAD)
        viewer.last_reply = reply
        viewer.last_ts = time.time()
        self.viewers[key] = viewer
        self.save()

    def viewer(self, name: str) -> Viewer | None:
        if (name or "").lower() in ("streamer", STREAMER_THREAD):
            return self.viewers.get(STREAMER_THREAD)
        return self.viewers.get((name or "").lower())

    def llm_messages(self, thread_key: str, limit: int | None = None) -> list[dict[str, str]]:
        n = limit or self.thread_turns
        msgs: list[dict[str, str]] = []
        for turn in list(self._thread(thread_key))[-n:]:
            if turn.role == "cohost":
                msgs.append({"role": "assistant", "content": turn.text})
            else:
                msgs.append({"role": "user", "content": f"{turn.who} ({turn.via}): {turn.text}"})
        return msgs

    def prompt_block(
        self,
        focus: str | None = None,
        *,
        streamer_name: str = "Streamer",
        is_voice: bool = False,
        event_n: int = 20,
    ) -> str:
        streamer_name = streamer_name or self.streamer_display
        focus = focus or STREAMER_THREAD
        thread_lines = [t.line() for t in list(self._thread(focus))[-self.thread_turns:]]
        thread_txt = "\n".join(thread_lines) if thread_lines else "(no turns in this conversation yet)"

        if focus == STREAMER_THREAD:
            where = "on the microphone" if is_voice else "in Twitch chat (this is still the streamer, not a viewer)"
            header = f"You are talking with {streamer_name}, the STREAMER, {where}."
        else:
            login = focus.split(":", 1)[-1] if focus.startswith("viewer:") else focus
            header = (
                f"You are replying in Twitch chat to viewer @{login}. "
                f"This is NOT the streamer ({streamer_name})."
            )

        roster_bits = []
        ranked = sorted(self.viewers.values(), key=lambda x: x.last_ts, reverse=True)
        for v in ranked[:8]:
            if v.is_streamer:
                continue
            if focus != STREAMER_THREAD and v.name.lower() == focus.split(":")[-1]:
                continue
            snippet = (v.last_said or "")[:80]
            roster_bits.append(f"{v.name}: “{snippet}”" if snippet else v.name)
        roster = ("Other people in chat (not this conversation): " + "; ".join(roster_bits)) if roster_bits else "Chat is otherwise quiet."

        recent = [e.line() for e in list(self.events)[-event_n:]]
        global_txt = "\n".join(recent) if recent else "(empty)"
        return (
            f"{header}\n\nThis conversation:\n{thread_txt}\n\n{roster}\n\n"
            f"Room timeline (do not mix these people up):\n{global_txt}"
        )

    def snapshot(self) -> dict[str, Any]:
        recent = [e.line() for e in list(self.events)[-30:]]
        people = []
        streamer = self.viewers.get(STREAMER_THREAD)
        if streamer:
            people.append(self._person_payload(STREAMER_THREAD, streamer))
        for v in sorted(
            (kv for kv in self.viewers.items() if kv[0] != STREAMER_THREAD),
            key=lambda kv: kv[1].last_ts,
            reverse=True,
        )[:16]:
            people.append(self._person_payload(self.viewer_thread_key(v[0]), v[1]))
        return {
            "events": recent,
            "people": people,
            "viewers": [
                {
                    "name": p["name"],
                    "messages": p["messages"],
                    "last_said": p["last_said"],
                    "last_reply": p["last_reply"],
                }
                for p in people
            ],
            "event_count": len(self.events),
        }

    def _person_payload(self, thread_key: str, v: Viewer) -> dict[str, Any]:
        turns = [t.line() for t in self._thread(thread_key)]
        return {
            "id": thread_key,
            "name": v.name,
            "is_streamer": v.is_streamer,
            "messages": v.messages,
            "last_said": v.last_said,
            "last_reply": v.last_reply,
            "thread": turns,
        }

    def _trim_viewers(self) -> None:
        extras = {k: v for k, v in self.viewers.items() if k != STREAMER_THREAD}
        if len(extras) <= self.viewer_limit:
            return
        ranked = sorted(extras.items(), key=lambda kv: kv[1].last_ts)
        drop = len(extras) - self.viewer_limit
        for key, _ in ranked[:drop]:
            self.viewers.pop(key, None)
            self.threads.pop(self.viewer_thread_key(key), None)
