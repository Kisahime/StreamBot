from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from src.safety import fence_viewer_text
from src.twitch.auth import CORE_TYPES, feed_for, missing_scope

log = logging.getLogger("cohost.eventsub")

WS_URL = "wss://eventsub.wss.twitch.tv/ws"
HELIX = "https://api.twitch.tv/helix"
KEEPALIVE_GRACE = 3.0


def next_backoff(current: float) -> float:
    """2s, 4s, 8s … capped at 60s."""
    return min(60.0, max(2.0, float(current) * 2))

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
    id: str = ""

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
            msg = str(extra.get("message") or "")
            bit = f" for {months} months" if months else ""
            extra_msg = f" They said: {fence_viewer_text(msg, limit=200)}" if msg else ""
            return f"{self.user} just resubbed{bit}.{extra_msg}"
        if self.kind == "gift":
            total = extra.get("total") or 1
            who = "Anonymous" if extra.get("anonymous") or not self.user else self.user
            n = int(total) if str(total).isdigit() else 1
            return f"{who} gifted {n} sub{'s' if n != 1 else ''}."
        if self.kind == "raid":
            viewers = extra.get("viewers") or extra.get("viewer_count") or "?"
            return f"{self.user} just raided in with {viewers} viewers."
        if self.kind == "cheer":
            bits = extra.get("bits") or 0
            if len(names) > 1:
                total = extra.get("total_bits") or bits
                return f"{', '.join(names)} cheered ({total} bits)."
            who = names[0] if names else "Someone"
            return f"{who} cheered {bits} bits."
        if self.kind == "redeem":
            title = extra.get("title") or "a reward"
            note = str(extra.get("input") or "")
            bit = f" They said: {fence_viewer_text(note, limit=200)}" if note else ""
            return f"{self.user} redeemed {title}.{bit}"
        if self.kind == "hype_begin":
            train = str(extra.get("train") or "").strip()
            label = f"{train} hype train" if train else "hype train"
            return f"A {label} just started (level {extra.get('level') or 1})."
        if self.kind == "hype_end":
            train = str(extra.get("train") or "").strip()
            label = f"{train} hype train" if train else "hype train"
            return f"The {label} ended at level {extra.get('level') or 1}."
        if self.kind == "poll_start":
            return f"A poll just started: {extra.get('title') or 'a poll'}."
        if self.kind == "poll_end":
            return _outcome_scene("Poll", extra, "a poll")
        if self.kind == "prediction_start":
            return f"A prediction just opened: {extra.get('title') or 'a prediction'}."
        if self.kind == "prediction_end":
            return _outcome_scene("Prediction", extra, "a prediction")
        return f"{self.kind}: {self.user}"


def _outcome_scene(label: str, extra: dict[str, Any], fallback: str) -> str:
    winner = str(extra.get("winner") or "").strip()
    if not winner:
        return f"The {label.lower()} ended: {extra.get('title') or fallback}."
    percent = extra.get("percent")
    if percent is None or percent == "":
        return f"{label} ended: '{winner}' won."
    return f"{label} ended: '{winner}' won with {int(percent)}%."


class EventSubClient:
    def __init__(
        self,
        token: str,
        client_id: str,
        channel: str,
        on_event: Callable[[StreamEvent], Awaitable[None]],
        on_status: Callable[[str], Awaitable[None]],
        subscriptions: list[tuple[str, str]] | None = None,
    ) -> None:
        self.token = bearer_token(token)
        self.client_id = (client_id or "").strip()
        self.channel = (channel or "").strip().lstrip("#")
        self.on_event = on_event
        self.on_status = on_status
        self.subscriptions = list(subscriptions or SUBSCRIPTIONS)
        self.connected = False
        self.last_error: str | None = None
        self.fatal = False
        self.broadcaster_id = ""
        self.moderator_id = ""
        self._task: asyncio.Task | None = None
        self._stop = False
        self.connecting = False
        self._ready = asyncio.Event()
        self._seen: deque[str] = deque(maxlen=400)
        self._seen_set: set[str] = set()
        self._ws: Any = None
        self.scopes: set[str] | None = None
        self.expires_in: int | None = None
        self.validated_at: float | None = None
        self.token_login = ""
        self.feed_status: dict[str, str] = {}
        self.keepalive_timeout = 10.0

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
        self.connecting = True
        self._ready = asyncio.Event()
        self.last_error = None
        try:
            # Lookups happen before the socket opens. EventSub drops a session
            # that has no subscriptions within about 10s of welcome.
            await self.refresh_auth()
            await self.resolve_ids()
        except Exception:
            self.connecting = False
            raise
        self._task = asyncio.create_task(self._run(), name="twitch-eventsub")
        await self.on_status("connecting")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except TimeoutError as err:
            await self.disconnect()
            raise TimeoutError("EventSub websocket did not finish subscribing. Try Connect alerts again.") from err
        finally:
            self.connecting = False
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
        if not self._ready.is_set():
            self._ready.set()
        await self.on_status("disconnected")

    def _jitter(self, seconds: float) -> float:
        spread = float(seconds) * 0.2
        return max(0.0, float(seconds) + random.uniform(-spread, spread))

    async def _run(self) -> None:
        url = WS_URL
        backoff = 2.0
        while not self._stop:
            try:
                await self._session(url)
                url = WS_URL
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
                log.exception("EventSub session failed")
                self.connected = False
                status = "token rejected" if _looks_like_auth(str(e)) else "error"
                if status == "token rejected":
                    self.fatal = True
                await self.on_status(status)
                if not self._ready.is_set():
                    self._ready.set()
                if self.fatal:
                    break
                url = WS_URL
            if self._stop or self.fatal:
                break
            await asyncio.sleep(self._jitter(backoff))
            backoff = next_backoff(backoff)

    async def _session(self, url: str) -> None:
        ws = await websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**20)
        self._ws = ws
        try:
            await self._read(ws, subscribe=True)
        finally:
            current = self._ws
            self._ws = None
            self.connected = False
            for sock in (ws, current):
                if sock is None:
                    continue
                try:
                    await sock.close()
                except Exception:
                    pass

    async def _read(self, ws, *, subscribe: bool) -> None:
        keepalive = 10.0
        while not self._stop:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=keepalive + KEEPALIVE_GRACE)
            except TimeoutError as err:
                raise TimeoutError("EventSub keepalive timeout") from err
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
                    keepalive = float(session.get("keepalive_timeout_seconds") or keepalive)
                except (TypeError, ValueError):
                    keepalive = 10.0
                self.keepalive_timeout = keepalive
                if subscribe:
                    try:
                        await self._subscribe_all(sid)
                    except Exception as e:
                        self.last_error = str(e)
                        log.error("EventSub subscribe failed: %s", e)
                        status = "token rejected" if _looks_like_auth(str(e)) else "error"
                        if status == "token rejected":
                            self.fatal = True
                        await self.on_status(status)
                        self._ready.set()
                        break
                self.connected = True
                self.last_error = None
                await self.on_status("connected")
                self._ready.set()
            elif mtype == "session_reconnect":
                new_url = (payload.get("session") or {}).get("reconnect_url")
                log.info("EventSub reconnect requested")
                if not new_url:
                    break
                await self._handover(ws, str(new_url))
                return
            elif mtype == "session_keepalive":
                continue
            elif mtype == "revocation":
                await self._on_revocation(payload.get("subscription") or {})
            elif mtype == "notification":
                mid = meta.get("message_id") or ""
                if mid and not self._remember(mid):
                    continue
                ev = _parse_notification(
                    meta.get("subscription_type") or "",
                    payload.get("event") or {},
                    channel=self.channel,
                )
                if ev:
                    await self.on_event(ev)

    async def _handover(self, old_ws, new_url: str) -> None:
        """Open the reconnect URL and wait for its welcome before closing the old socket."""
        new_ws = await websockets.connect(new_url, ping_interval=20, ping_timeout=20, max_size=2**20)
        try:
            raw = await asyncio.wait_for(new_ws.recv(), timeout=10 + KEEPALIVE_GRACE)
            data = json.loads(raw)
        except Exception:
            try:
                await new_ws.close()
            except Exception:
                pass
            raise
        meta = data.get("metadata") or {}
        if (meta.get("message_type") or "") != "session_welcome":
            try:
                await new_ws.close()
            except Exception:
                pass
            raise RuntimeError("EventSub reconnect did not welcome")
        session = (data.get("payload") or {}).get("session") or {}
        try:
            self.keepalive_timeout = float(session.get("keepalive_timeout_seconds") or 10)
        except (TypeError, ValueError):
            self.keepalive_timeout = 10.0
        try:
            await old_ws.close()
        except Exception:
            pass
        self._ws = new_ws
        self.connected = True
        await self.on_status("connected")
        await self._read(new_ws, subscribe=False)

    async def _on_revocation(self, sub: dict) -> None:
        subtype = str(sub.get("type") or "")
        status = str(sub.get("status") or "revoked")
        feed = feed_for(subtype) if subtype else ""
        if feed:
            self.feed_status[feed] = f"revoked {status}"
        log.warning("EventSub revoked %s: %s", subtype, status)
        await self.on_status("revoked")

    def _remember(self, mid: str) -> bool:
        if mid in self._seen_set:
            return False
        if len(self._seen) == self._seen.maxlen:
            dropped = self._seen[0]
            self._seen_set.discard(dropped)
        self._seen.append(mid)
        self._seen_set.add(mid)
        return True

    def _helix_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Client-Id": self.client_id,
            "Content-Type": "application/json",
        }

    def _apply_auth(self, meta: dict) -> None:
        self.scopes = set(meta.get("scopes") or [])
        expires = meta.get("expires_in")
        self.expires_in = int(expires) if expires is not None else None
        self.token_login = str(meta.get("login") or "")
        self.validated_at = time.time()
        user_id = str(meta.get("user_id") or "")
        if user_id:
            self.moderator_id = user_id

    async def refresh_auth(self) -> None:
        from src.twitch.auth import validate

        try:
            meta = await validate(self.token)
        except Exception as exc:
            log.warning("Token validate failed; subscriptions will not be scope-filtered: %s", exc)
            self.scopes = None
            self.expires_in = None
            self.token_login = ""
            return
        self._apply_auth(meta)

    async def revalidate(self) -> str:
        """Refresh scopes while connected. A 401 or 403 is fatal."""
        from src.twitch.auth import validate

        try:
            meta = await validate(self.token)
        except Exception as exc:
            message = str(exc)
            if "HTTP 401" in message or "HTTP 403" in message:
                self.fatal = True
                return "token rejected"
            log.warning("Token revalidate failed: %s", exc)
            return "error"
        self._apply_auth(meta)
        return "ok"

    async def resolve_ids(self) -> None:
        if self.broadcaster_id and self.moderator_id:
            return
        headers = self._helix_headers()
        async with httpx.AsyncClient(timeout=12.0) as client:
            if not self.moderator_id:
                me = await client.get(f"{HELIX}/users", headers=headers)
                if me.status_code != 200:
                    raise RuntimeError(_helix_err("Get Users (self)", me))
                me_data = (me.json().get("data") or [{}])[0]
                self.moderator_id = str(me_data.get("id") or "")
            if not self.broadcaster_id:
                ch = await client.get(f"{HELIX}/users", headers=headers, params={"login": self.channel})
                if ch.status_code != 200:
                    raise RuntimeError(_helix_err("Get Users (channel)", ch))
                ch_list = ch.json().get("data") or []
                if not ch_list:
                    raise RuntimeError(f"Twitch channel login not found: {self.channel}")
                self.broadcaster_id = str(ch_list[0].get("id") or "")
        if not self.broadcaster_id or not self.moderator_id:
            raise RuntimeError("Could not resolve broadcaster or token user id")

    def _note_feed(self, subscription_type: str, status: str) -> None:
        feed = feed_for(subscription_type)
        current = self.feed_status.get(feed)
        if status == "ok":
            if current is None:
                self.feed_status[feed] = "ok"
            return
        self.feed_status[feed] = status

    async def _subscribe_all(self, session_id: str) -> None:
        if not self.broadcaster_id or not self.moderator_id:
            await self.resolve_ids()
        self.feed_status = {}
        headers = self._helix_headers()
        broadcaster_id = self.broadcaster_id
        moderator_id = self.moderator_id
        core = [item for item in self.subscriptions if item[0] in CORE_TYPES]
        extra = [item for item in self.subscriptions if item[0] not in CORE_TYPES]
        async with httpx.AsyncClient(timeout=12.0) as client:
            errors = await self._post_subscriptions(client, headers, session_id, broadcaster_id, moderator_id, core)
            errors.extend(
                await self._post_subscriptions(client, headers, session_id, broadcaster_id, moderator_id, extra)
            )
        attempted = [
            item
            for item in self.subscriptions
            if not str(self.feed_status.get(feed_for(item[0]), "")).startswith("missing scope")
        ]
        if attempted and len(errors) == len(attempted):
            raise RuntimeError("; ".join(errors))
        if errors:
            log.warning("Some EventSub subscriptions failed: %s", "; ".join(errors))
            self.last_error = "; ".join(errors)

    async def _post_subscriptions(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        session_id: str,
        broadcaster_id: str,
        moderator_id: str,
        items: list[tuple[str, str]],
    ) -> list[str]:
        if not items:
            return []
        sem = asyncio.Semaphore(4)

        async def _one(stype: str, version: str) -> str | None:
            scope = missing_scope(self.scopes, stype)
            if scope:
                self._note_feed(stype, f"missing scope {scope}")
                log.info("Skipping %s: missing scope %s", stype, scope)
                return None
            async with sem:
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
                response = await client.post(f"{HELIX}/eventsub/subscriptions", headers=headers, json=body)
            if response.status_code in (200, 202, 409):
                self._note_feed(stype, "ok")
                return None
            message = f"{stype}: {_helix_err('subscribe', response)}"
            self._note_feed(stype, f"HTTP {response.status_code} {message}"[:180])
            return message

        results = await asyncio.gather(*(_one(stype, version) for stype, version in items))
        return [item for item in results if item]


def _looks_like_auth(text: str) -> bool:
    low = (text or "").lower()
    return any(
        bit in low
        for bit in ("401", "403", "invalid oauth", "missing proper authorization", "authorization failed")
    )


def _helix_err(what: str, resp: httpx.Response) -> str:
    try:
        msg = resp.json().get("message") or resp.text
    except Exception:
        msg = resp.text
    return f"{what} HTTP {resp.status_code}: {str(msg)[:240]}"


def _hype_extra(event: dict[str, Any]) -> dict[str, Any]:
    train = str(event.get("type") or "").replace("_", " ").strip()
    return {"level": event.get("level") or 1, "train": train}


def _quiet_status(event: dict[str, Any]) -> bool:
    return str(event.get("status") or "").strip().lower() in {"archived", "terminated"}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _poll_extra(event: dict[str, Any]) -> dict[str, Any]:
    title = event.get("title") or "a poll"
    raw_choices = event.get("choices") if isinstance(event.get("choices"), list) else []
    choices = []
    for choice in raw_choices:
        if not isinstance(choice, dict):
            continue
        choices.append({"title": str(choice.get("title") or ""), "votes": _as_int(choice.get("votes"))})
    extra: dict[str, Any] = {"title": title, "choices": choices}
    if not choices:
        return extra
    winner = max(choices, key=lambda item: item["votes"])
    total = sum(item["votes"] for item in choices)
    extra["winner"] = winner["title"]
    if total > 0:
        extra["percent"] = int(round(100 * winner["votes"] / total))
    return extra


def _prediction_extra(event: dict[str, Any]) -> dict[str, Any]:
    title = event.get("title") or "a prediction"
    raw = event.get("outcomes") if isinstance(event.get("outcomes"), list) else []
    outcomes = []
    for outcome in raw:
        if not isinstance(outcome, dict):
            continue
        outcomes.append(
            {
                "id": str(outcome.get("id") or ""),
                "title": str(outcome.get("title") or ""),
                "users": _as_int(outcome.get("users")),
                "channel_points": _as_int(outcome.get("channel_points")),
            }
        )
    extra: dict[str, Any] = {"title": title, "outcomes": outcomes}
    if not outcomes:
        return extra
    winner_id = str(event.get("winning_outcome_id") or "")
    winner = next((item for item in outcomes if item["id"] and item["id"] == winner_id), None)
    if winner is None:
        winner = max(outcomes, key=lambda item: (item["channel_points"], item["users"]))
    extra["winner"] = winner["title"]
    points = sum(item["channel_points"] for item in outcomes)
    users = sum(item["users"] for item in outcomes)
    if points > 0:
        extra["percent"] = int(round(100 * winner["channel_points"] / points))
    elif users > 0:
        extra["percent"] = int(round(100 * winner["users"] / users))
    return extra


def _parse_notification(stype: str, event: dict[str, Any], channel: str = "") -> StreamEvent | None:
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
        from_login = str(event.get("from_broadcaster_user_login") or "").lower()
        if channel and from_login == channel.lower():
            return None
        return StreamEvent(
            kind="raid",
            user=user,
            extra={
                "viewers": event.get("viewers"),
                "from_id": event.get("from_broadcaster_user_id") or "",
                "from_login": event.get("from_broadcaster_user_login") or "",
            },
        )
    if stype == "channel.cheer":
        return StreamEvent(
            kind="cheer",
            user=user or "Anonymous",
            extra={"bits": event.get("bits") or 0, "anonymous": bool(event.get("is_anonymous"))},
        )
    if stype == "channel.channel_points_custom_reward_redemption.add":
        reward = event.get("reward") or {}
        title = reward.get("title") if isinstance(reward, dict) else ""
        return StreamEvent(
            kind="redeem",
            user=user,
            extra={"title": title or "", "input": event.get("user_input") or ""},
        )
    if stype == "channel.hype_train.begin":
        return StreamEvent(kind="hype_begin", user="chat", extra=_hype_extra(event))
    if stype == "channel.hype_train.progress":
        return StreamEvent(kind="hype_progress", user="chat", extra=_hype_extra(event))
    if stype == "channel.hype_train.end":
        return StreamEvent(kind="hype_end", user="chat", extra=_hype_extra(event))
    if stype == "channel.poll.begin":
        return StreamEvent(kind="poll_start", user="chat", extra={"title": event.get("title") or "a poll"})
    if stype == "channel.poll.end":
        if _quiet_status(event):
            return None
        return StreamEvent(kind="poll_end", user="chat", extra=_poll_extra(event))
    if stype == "channel.prediction.begin":
        return StreamEvent(kind="prediction_start", user="chat", extra={"title": event.get("title") or "a prediction"})
    if stype == "channel.prediction.end":
        if _quiet_status(event):
            return None
        return StreamEvent(kind="prediction_end", user="chat", extra=_prediction_extra(event))
    return None
