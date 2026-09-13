from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
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
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    mention: bool = False

    def render(self) -> str:
        return f"{self.user}: {self.text}"

    def is_engaging(self) -> bool:
        if self.mention:
            return True
        return bool(ENGAGE_RE.search(self.text or ""))


def normalize_token(token: str) -> str:
    token = token.strip()
    if not token:
        return ""
    if not token.lower().startswith("oauth:"):
        token = f"oauth:{token}"
    return token


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
        bot_names: Optional[list[str]] = None,
    ) -> None:
        self.token = normalize_token(token)
        self.nick = nick
        self.channel = channel.lstrip("#")
        self.on_line = on_line
        self.on_status = on_status
        self.buffer: deque[ChatLine] = deque(maxlen=buffer_size)
        self.bot_names = {n.lower() for n in (bot_names or [nick]) if n}
        self._bot: Optional[commands.Bot] = None
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._connect_error: Optional[str] = None
        self.connected = False

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

            async def event_error(inner_self, error: Exception, data: Optional[str] = None) -> None:
                parent._connect_error = str(error)
                log.error("Twitch error: %s %s", error, data or "")
                await parent.on_status("error")
                parent._ready.set()

            async def event_message(inner_self, message) -> None:
                if message.echo:
                    return
                author = (message.author.name if message.author else "") or ""
                content = (message.content or "").strip()
                if author.lower() in parent.bot_names:
                    return
                if is_spammy(content):
                    return
                mention = any(n in content.lower() for n in parent.bot_names)
                line = ChatLine(user=author, text=content, mention=mention)
                parent.buffer.append(line)
                await parent.on_line(line)

        self._bot = Bot()
        self._task = asyncio.create_task(self._bot.start(), name="twitch-irc")
        await self.on_status("connecting")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except asyncio.TimeoutError:
            await self.disconnect()
            raise TimeoutError("Twitch IRC did not become ready in 20s. Check token, nick, and channel.")
        if not self.connected:
            err = self._connect_error or "Twitch login failed"
            await self.disconnect()
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
        await self.on_status("disconnected")

    async def send(self, text: str) -> None:
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
