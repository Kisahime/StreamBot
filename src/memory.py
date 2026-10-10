from __future__ import annotations

import json
import logging
import re
import shutil
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("cohost.memory")

AT_RE = re.compile(r"@([A-Za-z0-9_]+)")
URL_RE = re.compile(r"https?://\S+", re.I)
CMD_RE = re.compile(r"(^|\s)!\w+")
BROADCAST_RE = re.compile(r"(?i)^(hey |hi )?(chat|everyone|folks|guys|all)\b")
JSON_RE = re.compile(r"\{.*\}", re.S)
META_LINE = re.compile(
    r"(?is)^\s*(?:action|reply|message|output|response|type|mode|chatreply|chat.?reply)"
    r"\s*:\s*[a-z0-9_\- ]*[:\-]?\s*"
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
INTERVIEW = re.compile(
    r"(?i)("
    r"caught your eye|clips or moments|clip/moment|any clips|"
    r"what'?s been on the screen|what'?s got your interest|"
    r"wanna talk about|want to talk about|what do you want to talk about|"
    r"good memes|got a quick one|quick one for you"
    r")"
)
NOTE_RE = re.compile(
    r"(?i)\b(i main|i play|i(?:'m| am) a|first time|my name is|i love|i main)\b"
)
EMOTES = {
    "lul",
    "lulw",
    "kekw",
    "omegalul",
    "pogchamp",
    "kappa",
    "kappapride",
    "monkas",
    "monkaw",
    "sadge",
    "pepega",
    "pepehands",
    "widepeepohappy",
    "catjam",
    "kkona",
    "pog",
    "pogu",
    "pausechamp",
}


def sanitize_reply(text: str, bot_name: str = "") -> str:
    """Strip JSON, action labels, and other model junk from a chat line."""
    try:
        return _sanitize_reply(text, bot_name)
    except re.error:
        return re.sub(r"\s+", " ", (text or "").strip())


def _sanitize_reply(text: str, bot_name: str = "") -> str:
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


def never_say_phrases(persona: dict[str, Any] | None) -> list[str]:
    if not persona:
        return []
    items = persona.get("never_say") or persona.get("banned_phrases") or []
    if isinstance(items, str):
        items = [items]
    return [str(x).strip() for x in items if str(x).strip()]


def is_generic_filler(text: str, extra: list[str] | None = None) -> bool:
    raw = text or ""
    if GENERIC_BOT.search(raw) or INTERVIEW.search(raw):
        return True
    low = raw.lower()
    for phrase in extra or []:
        p = (phrase or "").strip().lower()
        if p and p in low:
            return True
    return False


def replies_too_similar(a: str, b: str) -> bool:
    """True when two replies are the same template with the names swapped."""

    def toks(raw: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9']+", (raw or "").lower()) if len(w) > 2}

    left, right = toks(a), toks(b)
    if len(left) < 3 or len(right) < 3:
        return False
    return len(left & right) / min(len(left), len(right)) >= 0.6


def streamer_smalltalk_fallback(text: str) -> str:
    """A short in-character line when the model only produced an interview template."""
    low = re.sub(r"[^a-z0-9' ?]", "", (text or "").lower()).strip()
    if re.search(r"\bhow are (you|ya|u)\b", low):
        return "pretty good, parked on the couch. you?"
    if re.search(r"\b(what('?s| is) your name|who are you)\b", low):
        return "Pixel."
    if low in {"yes", "yes?", "yeah", "yeah?", "yep", "yep?"}:
        return "yeah?"
    if low in {"hey", "hi", "hello", "yo", "sup", "test", "testing", "hey pixel", "hi pixel", "hello pixel"}:
        return "hey. loud and clear."
    return ""


def repeats_recent(reply: str, earlier: list[str]) -> bool:
    cur = (reply or "").strip().lower()
    if not cur:
        return False
    for old in earlier:
        prev = (old or "").strip().lower()
        if not prev:
            continue
        if cur == prev or replies_too_similar(reply, old):
            return True
    return False


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
    """TTS should say names, not the @ symbol, and skip links, commands, and emotes."""
    raw = AT_RE.sub(r"\1", text or "")
    raw = URL_RE.sub(" ", raw)
    raw = CMD_RE.sub(" ", raw)
    raw = re.sub(r":[A-Za-z0-9_]{2,}:", " ", raw)
    kept: list[str] = []
    for tok in raw.split():
        bare = tok.strip(".,!?").lower()
        if bare in EMOTES:
            continue
        kept.append(tok)
    return " ".join(kept).strip()


def apply_catchphrase_budget(
    text: str,
    phrases: list[str],
    last_used: dict[str, float],
    *,
    window: float,
    now: float | None = None,
) -> str:
    """Drop a catchphrase that was used inside the window."""
    now = time.time() if now is None else now
    out = text or ""
    for phrase in phrases:
        p = (phrase or "").strip()
        if not p or p.lower() not in out.lower():
            continue
        seen = last_used.get(p.lower(), 0.0)
        if now - seen < window:
            out = re.sub(re.escape(p), "", out, flags=re.IGNORECASE)
        else:
            last_used[p.lower()] = now
    return re.sub(r"\s+", " ", out).strip(" -—")


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
    greeted: bool = False


STREAMER_THREAD = "streamer"
NOTE_CAP = 4


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
        self.dirty = False
        self.load()

    def set_streamer_display(self, name: str) -> None:
        self.streamer_display = (name or "Streamer").strip() or "Streamer"

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("memory.json is unreadable; keeping a backup and starting empty")
            self._preserve_bad()
            return
        self._ingest(data)

    def _preserve_bad(self) -> None:
        bad = self.path.with_suffix(".json.bad")
        try:
            shutil.copy2(self.path, bad)
        except Exception:
            log.exception("could not copy corrupt memory")
        bak = self.path.with_suffix(".json.bak")
        if bak.exists():
            try:
                data = json.loads(bak.read_text(encoding="utf-8"))
                self._ingest(data)
                log.info("restored memory from %s", bak.name)
            except Exception:
                log.exception("memory backup was also unreadable")

    def _ingest(self, data: dict[str, Any]) -> None:
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
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        self.dirty = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "events": [asdict(e) for e in self.events],
            "viewers": {k: asdict(v) for k, v in self.viewers.items()},
            "threads": {k: [asdict(t) for t in turns] for k, turns in self.threads.items()},
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(raw, encoding="utf-8")
        if self.path.exists():
            try:
                shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
            except Exception:
                log.exception("could not refresh memory backup")
        tmp.replace(self.path)

    def clear(self) -> None:
        self.events.clear()
        self.viewers.clear()
        self.threads.clear()
        self.dirty = True
        self.flush()

    def _thread(self, key: str) -> deque[Turn]:
        if key not in self.threads:
            self.threads[key] = deque(maxlen=self.thread_turns)
        return self.threads[key]

    @staticmethod
    def viewer_thread_key(login: str) -> str:
        return f"viewer:{(login or '').lower()}"

    def add_note(self, key: str, note: str, *, is_streamer: bool = False) -> None:
        text = (note or "").strip()
        if not text:
            return
        viewer = self.viewers.get(key) or Viewer(
            name=self.streamer_display if is_streamer else key,
            is_streamer=is_streamer,
        )
        viewer.notes.append(text[:140])
        viewer.notes = viewer.notes[-NOTE_CAP:]
        viewer.last_ts = time.time()
        self.viewers[key] = viewer
        self.save()

    def _maybe_note(self, key: str, text: str) -> None:
        if not NOTE_RE.search(text or ""):
            return
        viewer = self.viewers.get(key)
        if not viewer:
            return
        snippet = text.strip()[:140]
        if snippet in viewer.notes:
            return
        viewer.notes.append(snippet)
        viewer.notes = viewer.notes[-NOTE_CAP:]

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
            self._maybe_note(key, text)
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
            Turn(
                ts=ev.ts,
                who=user or ("Pixel" if role == "cohost" else self.streamer_display),
                text=text,
                via=via,
                role=role,
            )
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
        low = (reply or "").lower()
        if any(g in low for g in ("hey", "hi ", "hello", "welcome", "heyyy")):
            viewer.greeted = True
        self.viewers[key] = viewer
        self.save()

    def mark_greeted(self, user: str) -> None:
        key = (user or "").lower()
        viewer = self.viewers.get(key)
        if not viewer:
            return
        viewer.greeted = True
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
        include_alerts: bool = True,
    ) -> str:
        """Roster, notes, and alerts. Thread turns are passed separately as chat messages."""
        streamer_name = streamer_name or self.streamer_display
        focus = focus or STREAMER_THREAD

        if focus == STREAMER_THREAD:
            where = "on the microphone" if is_voice else "in Twitch chat (this is still the streamer, not a viewer)"
            header = f"You are talking with {streamer_name}, the STREAMER, {where}."
            focus_viewer = self.viewers.get(STREAMER_THREAD)
        else:
            login = focus.split(":", 1)[-1] if focus.startswith("viewer:") else focus
            header = (
                f"You are replying in Twitch chat to viewer @{login}. "
                f"This is NOT the streamer ({streamer_name})."
            )
            focus_viewer = self.viewers.get(login.lower())

        extra = []
        if focus_viewer and focus_viewer.notes:
            extra.append(
                "Private bookmarks, do not mention these unless they ask: "
                + "; ".join(focus_viewer.notes[-NOTE_CAP:])
            )
        if focus != STREAMER_THREAD and focus_viewer:
            if focus_viewer.greeted:
                extra.append("You already greeted them this session. Do not greet again.")
            elif focus_viewer.messages <= 1:
                extra.append("First time you've talked to them.")

        roster_bits = []
        ranked = sorted(self.viewers.values(), key=lambda x: x.last_ts, reverse=True)
        for v in ranked[:8]:
            if v.is_streamer:
                continue
            if focus != STREAMER_THREAD and v.name.lower() == focus.split(":")[-1]:
                continue
            snippet = (v.last_said or "")[:80]
            roster_bits.append(f"{v.name}: “{snippet}”" if snippet else v.name)
        roster = (
            ("Other people in chat (not this conversation): " + "; ".join(roster_bits))
            if roster_bits
            else "Chat is otherwise quiet."
        )

        parts = [header, roster]
        if extra:
            parts.append("\n".join(extra))
        if include_alerts:
            alerts = [e.line() for e in self.events if e.kind == "twitch_event"][-6:]
            if alerts:
                parts.append("Alerts that matter:\n" + "\n".join(alerts))
        return "\n\n".join(parts)

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
            "greeted": v.greeted,
            "notes": list(v.notes),
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
