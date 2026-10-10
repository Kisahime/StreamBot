from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from twitchio.ext import commands

log = logging.getLogger("cohost.twitch")

URL_RE = re.compile(r"https?://\S+", re.I)
CMD_RE = re.compile(r"^!\w+")
ENGAGE_RE = re.compile(
    r"(?i)\b("
    r"hi|hey|hello|yo|sup|howdy|hiya|heya|hola|"
    r"good (morning|afternoon|evening|night)|"
    r"how are (you|ya|u)|how(?:'|’)s it going|what(?:'|’)s up|whats up|wyd|"
    r"who are you|what(?: are|'s) you"
    r")\b|\?"
)


@dataclass
class ChatLine:
    user: str
    text: str
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    mention: bool = False
    command: str = ""
    is_mod: bool = False
    is_broadcaster: bool = False
    ts: float = field(default_factory=time.monotonic)
    id: str = ""
    first_msg: bool = False

    def render(self) -> str:
        return f"{self.user}: {self.text}"

    def is_engaging(self) -> bool:
        if self.mention:
            return True
        return bool(ENGAGE_RE.search(self.text or ""))


def text_mentions(text: str, name: str) -> bool:
    """True when ``name`` is a whole word, with or without a leading @."""
    name = (name or "").strip().lstrip("@")
    if not name:
        return False
    return re.search(rf"(?<![A-Za-z0-9_])@?{re.escape(name)}(?![A-Za-z0-9_])", text or "", re.I) is not None


def normalize_token(token: str) -> str:
    token = token.strip()
    if not token:
        return ""
    if not token.lower().startswith("oauth:"):
        token = f"oauth:{token}"
    return token


def irc_scope_problem(meta: dict, nick: str) -> str:
    """Empty when this IRC token can chat as ``nick``. Otherwise a short reason."""
    login = str(meta.get("login") or "")
    expected = (nick or "").strip().lstrip("#").lower()
    if login.lower() != expected:
        return f"IRC token login is {login}, expected {expected}"
    scopes = {str(scope) for scope in (meta.get("scopes") or [])}
    for scope in ("chat:read", "chat:edit"):
        if scope not in scopes:
            return f"IRC token is missing {scope}"
    return ""


def looks_like_auth_failure(text: str) -> bool:
    low = (text or "").lower()
    return any(
        token in low
        for token in ("login unsuccessful", "authentication", "invalid token", "oauth", "401", "403", "improperly formatted auth")
    )


def message_is_first(message: Any) -> bool:
    """True when Twitch marks this as the chatter's first message in the channel."""
    if bool(getattr(message, "first", False)):
        return True
    tags = getattr(message, "tags", None)
    if isinstance(tags, dict):
        raw = tags.get("first-msg", tags.get("first_msg"))
        return str(raw).strip() in {"1", "true", "True"}
    return False


def author_flags(author: Any) -> tuple[bool, bool]:
    """Mod and broadcaster flags from TwitchIO, with a badge fallback."""
    if author is None:
        return False, False
    is_mod = bool(getattr(author, "is_mod", False))
    is_broadcaster = bool(getattr(author, "is_broadcaster", False))
    badges = getattr(author, "badges", None)
    names: set[str] = set()
    if isinstance(badges, dict):
        names = {str(name).lower() for name in badges}
    elif isinstance(badges, (list, tuple, set)):
        names = {str(name).lower() for name in badges}
    is_mod = is_mod or "moderator" in names
    is_broadcaster = is_broadcaster or "broadcaster" in names
    return is_mod, is_broadcaster


def is_spammy(text: str) -> bool:
    t = text.strip()
    if len(t) < 2:
        return True
    if CMD_RE.match(t):
        return True
    no_urls = URL_RE.sub("", t).strip()
    if not no_urls:
        return True
    if len(set(t.lower().split())) == 1 and len(t.split()) > 6:
        return True
    return False


class TwitchChat:
    def __init__(
        self,
        token: str,
        nick: str,
        channel: str,
        on_line: Callable[[ChatLine], Awaitable[None]],
        on_status: Callable[[str], Awaitable[None]],
        buffer_size: int = 40,
        bot_names: list[str] | None = None,
        allow_from: list[str] | None = None,
        commands: list[str] | None = None,
        min_send_gap: float = 1.2,
    ) -> None:
        self.token = normalize_token(token)
        self.nick = nick
        self.channel = channel.lstrip("#")
        self.on_line = on_line
        self.on_status = on_status
        self.buffer: deque[ChatLine] = deque(maxlen=buffer_size)
        self.bot_names = {n.lower() for n in (bot_names or [nick]) if n}
        self.allow_from = {n.lower().lstrip("@") for n in (allow_from or []) if n}
        self.commands = {c.lower() if c.startswith("!") else f"!{c.lower()}" for c in (commands or [])}
        self.min_send_gap = min_send_gap
        self._bot: commands.Bot | None = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._connect_error: str | None = None
        self.connected = False
        self.auth_failed = False
        self._out: asyncio.Queue[str] = asyncio.Queue()
        self._sender: asyncio.Task | None = None

    def set_allow_from(self, names: list[str]) -> None:
        self.allow_from = {n.lower().lstrip("@") for n in names if n}

    def alive(self) -> bool:
        return bool(self.connected and self._task and not self._task.done())

    async def connect(self) -> None:
        if self._task and not self._task.done() and self.connected:
            return
        if not self.token or not self.nick or not self.channel:
            raise ValueError("TWITCH_TOKEN, TWITCH_NICK, and TWITCH_CHANNEL are required")

        parent = self
        self._ready = asyncio.Event()
        self._connect_error = None

        class Bot(commands.Bot):
            def __init__(inner_self) -> None:
                super().__init__(
                    token=parent.token,
                    prefix="!",
                    initial_channels=[parent.channel],
                )

            async def event_ready(inner_self) -> None:
                parent.connected = True
                log.info("Twitch connected as %s -> #%s", parent.nick, parent.channel)
                await parent.on_status("connected")
                parent._ready.set()

            async def event_error(inner_self, error: Exception, data: str | None = None) -> None:
                parent._connect_error = str(error)
                log.error("Twitch error: %s %s", error, data or "")
                status = "token rejected" if looks_like_auth_failure(str(error)) else "error"
                if status == "token rejected":
                    parent.auth_failed = True
                await parent.on_status(status)
                parent._ready.set()

            async def event_message(inner_self, message) -> None:
                if message.echo:
                    return
                author = (message.author.name if message.author else "") or ""
                content = (message.content or "").strip()
                if author.lower() in parent.bot_names and author.lower() not in parent.allow_from:
                    return
                command = ""
                if content.startswith("!"):
                    command = content.split()[0].lower()
                    if command not in parent.commands:
                        return
                elif is_spammy(content):
                    return
                mention = any(text_mentions(content, n) for n in parent.bot_names)
                is_mod, is_broadcaster = author_flags(message.author)
                line = ChatLine(
                    user=author,
                    text=content,
                    mention=mention,
                    command=command,
                    is_mod=is_mod,
                    is_broadcaster=is_broadcaster,
                    first_msg=message_is_first(message),
                )
                parent.buffer.append(line)
                await parent.on_line(line)

        self.auth_failed = False
        self._bot = Bot()
        if self._sender is None or self._sender.done():
            self._sender = asyncio.create_task(self._send_loop(), name="twitch-send")
        self._task = asyncio.create_task(self._bot.start(), name="twitch-irc")
        await self.on_status("connecting")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except TimeoutError as err:
            await self.disconnect()
            raise TimeoutError("Twitch IRC did not become ready in 20s. Check token, nick, and channel.") from err
        if not self.connected:
            err = self._connect_error or "Twitch login failed"
            auth = looks_like_auth_failure(err)
            await self.disconnect()
            if auth:
                raise RuntimeError(f"token rejected: {err}")
            raise RuntimeError(err)

    async def disconnect(self) -> None:
        self.connected = False
        if self._bot:
            try:
                await self._bot.close()
            except Exception:
                log.exception("Twitch close failed")
            self._bot = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._sender:
            self._sender.cancel()
            try:
                await self._sender
            except asyncio.CancelledError:
                pass
            self._sender = None
        await self.on_status("disconnected")

    async def send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        await self._out.put(text[:500])

    async def _send_loop(self) -> None:
        while True:
            text = await self._out.get()
            try:
                await self._send_now(text)
            except Exception:
                log.exception("chat send failed")
            await asyncio.sleep(self.min_send_gap)

    async def _send_now(self, text: str) -> None:
        if not self._bot or not text.strip():
            return
        channel = self._bot.get_channel(self.channel)
        if channel is None:
            for ch in self._bot.connected_channels or []:
                if (ch.name or "").lower() == self.channel.lower():
                    channel = ch
                    break
        if channel is None:
            log.warning("Twitch channel object missing; cannot send")
            return
        await channel.send(text.strip()[:500])

    def recent(self, n: int) -> list[ChatLine]:
        items = list(self.buffer)
        return items[-n:]
