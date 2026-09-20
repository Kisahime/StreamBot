from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx
import websockets

log = logging.getLogger("cohost.eventsub")

WS_URL = "wss://eventsub.wss.twitch.tv/ws"
HELIX = "https://api.twitch.tv/helix"

SUBSCRIPTIONS = (
    ("channel.follow", "2"),
    ("channel.subscribe", "1"),
    ("channel.subscription.gift", "1"),
    ("channel.subscription.message", "1"),
    ("channel.raid", "1"),
)


def bearer_token(token: str) -> str:
    t = (token or "").strip()
    if t.lower().startswith("oauth:"):
        t = t[6:]
    return t


@dataclass
class StreamEvent:
    kind: str
    user: str
    users: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def people(self) -> list[str]:
        names = [n for n in ([self.user] + list(self.users)) if n]
        seen: set[str] = set()
        out: list[str] = []
        for n in names:
            key = n.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(n)
        return out

    def scene(self) -> str:
        names = self.people()
        extra = self.extra
        if self.kind == "follow":
            if len(names) <= 1:
                who = names[0] if names else "Someone"
                return f"{who} just followed the channel."
            if len(names) == 2:
                return f"{names[0]} and {names[1]} just followed."
            return f"{', '.join(names[:-1])}, and {names[-1]} just followed."
        if self.kind == "sub":
            tier = extra.get("tier") or ""
            bit = f" (tier {tier})" if tier and str(tier) not in ("", "1000") else ""
            return f"{self.user} just subscribed{bit}."
        if self.kind == "resub":
            months = extra.get("months") or extra.get("cumulative_months") or ""
            msg = extra.get("message") or ""
            bit = f" for {months} months" if months else ""
            extra_msg = f' They said: "{msg}"' if msg else ""
            return f"{self.user} just resubbed{bit}.{extra_msg}"
        if self.kind == "gift":
            total = extra.get("total") or 1
            who = "Anonymous" if extra.get("anonymous") or not self.user else self.user
            n = int(total) if str(total).isdigit() else 1
            return f"{who} gifted {n} sub{'s' if n != 1 else ''}."
        if self.kind == "raid":
            viewers = extra.get("viewers") or extra.get("viewer_count") or "?"
            return f"{self.user} just raided in with {viewers} viewers."
        return f"{self.kind}: {self.user}"


class EventSubClient:
    def __init__(
        self,
        token: str,
        client_id: str,
        channel: str,
        on_event: Callable[[StreamEvent], Awaitable[None]],
        on_status: Callable[[str], Awaitable[None]],
    ) -> None:
        self.token = bearer_token(token)
        self.client_id = (client_id or "").strip()
        self.channel = (channel or "").strip().lstrip("#")
        self.on_event = on_event
        self.on_status = on_status
        self.connected = False
        self.last_error: str | None = None
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._ready = asyncio.Event()
        self._seen: deque[str] = deque(maxlen=400)
        self._seen_set: set[str] = set()
        self._ws: Any = None

    def alive(self) -> bool:
        return bool(self.connected and self._task and not self._task.done())

    async def connect(self) -> None:
        if self._task and not self._task.done() and self.connected:
            return
        if not self.token:
            raise ValueError("TWITCH_EVENT_TOKEN or TWITCH_TOKEN is required for EventSub")
        if not self.client_id:
            raise ValueError("TWITCH_CLIENT_ID is required for EventSub (must match the event token's app)")
        if not self.channel:
            raise ValueError("TWITCH_CHANNEL is required for EventSub")
        await self.disconnect()
        self._stop = False
        self._ready = asyncio.Event()
        self.last_error = None
        self._task = asyncio.create_task(self._run(), name="twitch-eventsub")
        await self.on_status("connecting")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=25)
        except asyncio.TimeoutError:
            await self.disconnect()
            raise TimeoutError("EventSub did not become ready in 25s. Check Client-ID and event token scopes.")
        if not self.connected:
            err = self.last_error or "EventSub login failed"
            await self.disconnect()
            raise RuntimeError(err)

    async def disconnect(self) -> None:
        self._stop = True
        self.connected = False
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.on_status("disconnected")

    async def _run(self) -> None:
        url = WS_URL
        while not self._stop:
            try:
                next_url = await self._session(url)
                url = next_url or WS_URL
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
                log.exception("EventSub session failed")
                self.connected = False
                await self.on_status("error")
                if not self._ready.is_set():
                    self._ready.set()
                url = WS_URL
            if self._stop:
                break
            await asyncio.sleep(2)

    async def _session(self, url: str) -> Optional[str]:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**20) as ws:
            self._ws = ws
            reconnect: Optional[str] = None
            async for raw in ws:
                if self._stop:
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                meta = data.get("metadata") or {}
                payload = data.get("payload") or {}
                mtype = meta.get("message_type") or ""
                if mtype == "session_welcome":
                    session = payload.get("session") or {}
                    sid = session.get("id")
                    if not sid:
                        self.last_error = "EventSub welcome had no session id"
                        await self.on_status("error")
                        self._ready.set()
                        break
                    try:
                        await self._subscribe_all(sid)
                    except Exception as e:
                        self.last_error = str(e)
                        log.error("EventSub subscribe failed: %s", e)
                        await self.on_status("error")
                        self._ready.set()
                        break
                    self.connected = True
                    self.last_error = None
                    await self.on_status("connected")
                    self._ready.set()
                elif mtype == "session_reconnect":
                    reconnect = (payload.get("session") or {}).get("reconnect_url")
                    log.info("EventSub reconnect requested")
                    break
                elif mtype == "session_keepalive":
                    continue
                elif mtype == "revocation":
                    sub = payload.get("subscription") or {}
                    log.warning("EventSub revoked %s: %s", sub.get("type"), sub.get("status"))
                elif mtype == "notification":
                    mid = meta.get("message_id") or ""
                    if mid and not self._remember(mid):
                        continue
                    ev = _parse_notification(meta.get("subscription_type") or "", payload.get("event") or {})
                    if ev:
                        await self.on_event(ev)
            self._ws = None
            self.connected = False
            return reconnect

    def _remember(self, mid: str) -> bool:
        if mid in self._seen_set:
            return False
        if len(self._seen) == self._seen.maxlen:
            dropped = self._seen[0]
            self._seen_set.discard(dropped)
        self._seen.append(mid)
        self._seen_set.add(mid)
        return True

    async def _subscribe_all(self, session_id: str) -> None:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Client-Id": self.client_id,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=12.0) as client:
            me = await client.get(f"{HELIX}/users", headers=headers)
            if me.status_code != 200:
                raise RuntimeError(_helix_err("Get Users (self)", me))
            me_data = (me.json().get("data") or [{}])[0]
            moderator_id = str(me_data.get("id") or "")
            ch = await client.get(f"{HELIX}/users", headers=headers, params={"login": self.channel})
            if ch.status_code != 200:
                raise RuntimeError(_helix_err("Get Users (channel)", ch))
            ch_list = ch.json().get("data") or []
            if not ch_list:
                raise RuntimeError(f"Twitch channel login not found: {self.channel}")
            broadcaster_id = str(ch_list[0].get("id") or "")
            if not broadcaster_id or not moderator_id:
                raise RuntimeError("Could not resolve broadcaster or token user id")

            errors: list[str] = []
            for stype, version in SUBSCRIPTIONS:
                if stype == "channel.follow":
                    condition = {"broadcaster_user_id": broadcaster_id, "moderator_user_id": moderator_id}
                elif stype == "channel.raid":
                    condition = {"to_broadcaster_user_id": broadcaster_id}
                else:
                    condition = {"broadcaster_user_id": broadcaster_id}
                body = {
                    "type": stype,
                    "version": version,
                    "condition": condition,
                    "transport": {"method": "websocket", "session_id": session_id},
                }
                r = await client.post(f"{HELIX}/eventsub/subscriptions", headers=headers, json=body)
                if r.status_code not in (200, 202):
                    errors.append(f"{stype}: {_helix_err('subscribe', r)}")
            if len(errors) == len(SUBSCRIPTIONS):
                raise RuntimeError("; ".join(errors))
            if errors:
                log.warning("Some EventSub subscriptions failed: %s", "; ".join(errors))
                self.last_error = "; ".join(errors)


def _helix_err(what: str, resp: httpx.Response) -> str:
    try:
        msg = resp.json().get("message") or resp.text
    except Exception:
        msg = resp.text
    return f"{what} HTTP {resp.status_code}: {str(msg)[:240]}"


def _parse_notification(stype: str, event: dict[str, Any]) -> Optional[StreamEvent]:
    user = (
        event.get("user_name")
        or event.get("user_login")
        or event.get("from_broadcaster_user_name")
        or event.get("from_broadcaster_user_login")
        or ""
    )
    if stype == "channel.follow":
        return StreamEvent(kind="follow", user=user)
    if stype == "channel.subscribe":
        if event.get("is_gift"):
            return None
        return StreamEvent(
            kind="sub",
            user=user,
            extra={"tier": event.get("tier"), "is_gift": False},
        )
    if stype == "channel.subscription.message":
        msg = event.get("message") or {}
        text = msg.get("text") if isinstance(msg, dict) else ""
        return StreamEvent(
            kind="resub",
            user=user,
            extra={
                "months": event.get("cumulative_months") or event.get("duration_months"),
                "cumulative_months": event.get("cumulative_months"),
                "streak": event.get("streak_months"),
                "tier": event.get("tier"),
                "message": text,
            },
        )
    if stype == "channel.subscription.gift":
        anon = bool(event.get("is_anonymous"))
        return StreamEvent(
            kind="gift",
            user="" if anon else user,
            extra={
                "total": event.get("total") or 1,
                "cumulative_total": event.get("cumulative_total"),
                "tier": event.get("tier"),
                "anonymous": anon,
            },
        )
    if stype == "channel.raid":
        return StreamEvent(
            kind="raid",
            user=user,
            extra={"viewers": event.get("viewers")},
        )
    return None
