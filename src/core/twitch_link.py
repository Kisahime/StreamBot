"""IRC and EventSub connect, watchdog, shoutout, and markers."""

from __future__ import annotations

import asyncio
import logging
import time

from src.memory import (
    STREAMER_THREAD,
)
from src.twitch.chat import TwitchChat, irc_scope_problem, looks_like_auth_failure
from src.twitch.eventsub import EventSubClient, _looks_like_auth
from src.twitch.helix import HelixError

log = logging.getLogger("cohost.orch")

TOKEN_REVALIDATE_SECONDS = 3600


def _live(name: str):
    """Look up a name tests patch on src.orchestrator, at call time."""
    import src.orchestrator as mod

    return getattr(mod, name)


async def twitch_connect(orch) -> None:
    await orch.connect_irc()
    await orch.connect_events()


async def connect_irc(orch) -> None:
    orch.settings.reload_secrets()
    orch._want_twitch = True
    orch._irc_fatal = False
    await orch._irc_connect()
    await orch._validate_irc_token()


async def connect_events(orch) -> None:
    orch.settings.reload_secrets()
    orch._want_events = True
    orch._eventsub_fatal = False
    orch._eventsub_error = ""
    await orch._eventsub_connect()
    if orch._eventsub_error:
        raise RuntimeError(orch._eventsub_error)


async def _irc_connect(orch) -> None:
    if orch.twitch:
        await orch.twitch.disconnect()
    bot_name = orch.settings.twitch_nick
    persona_name = str(orch.persona.get("name") or "")
    orch.twitch = TwitchChat(
        token=orch.settings.twitch_token,
        nick=bot_name,
        channel=orch.settings.twitch_channel,
        on_line=orch.on_chat,
        on_status=orch._twitch_status,
        buffer_size=orch.settings.chat_buffer_size,
        bot_names=[bot_name, persona_name],
        allow_from=[orch.identity.login_key()] if orch.identity.login_key() else [],
        commands=orch._irc_commands(),
        min_send_gap=orch.settings.min_send_gap_seconds,
    )
    try:
        await orch.twitch.connect()
    except Exception as e:
        if looks_like_auth_failure(str(e)) or "token rejected" in str(e).lower():
            orch._irc_fatal = True
            await orch.hub.patch_status(twitch="token rejected", twitch_error=str(e))
        raise


async def _eventsub_connect(orch) -> None:
    async with orch._eventsub_lock:
        await orch._eventsub_connect_locked()


async def _eventsub_connect_locked(orch) -> None:
    if not orch.settings.events_enabled:
        await orch.hub.patch_status(eventsub="off")
        return
    if orch.eventsub and orch.eventsub.alive():
        return
    if orch.eventsub:
        await orch.eventsub.disconnect()
        orch.eventsub = None
    try:
        orch.eventsub = EventSubClient(
            token=orch.settings.event_token(),
            client_id=orch.settings.twitch_client_id,
            channel=orch.settings.twitch_channel,
            on_event=orch.on_stream_event,
            on_status=orch._eventsub_status,
            subscriptions=orch.settings.event_subscriptions(),
        )
        await orch.eventsub.connect()
        if orch.eventsub.fatal:
            orch._eventsub_fatal = True
    except Exception as e:
        log.warning("EventSub not connected: %s", e)
        orch.eventsub = None
        fatal = _looks_like_auth(str(e)) or "is required" in str(e).lower()
        orch._eventsub_fatal = fatal
        orch._eventsub_error = str(e)
        state = "token rejected" if fatal else "error"
        await orch.hub.patch_status(eventsub=state, eventsub_error=str(e))
        await orch.hub.broadcast({"type": "log", "data": f"EventSub: {e}"})


async def twitch_disconnect(orch) -> None:
    await orch.disconnect_irc()
    await orch.disconnect_events()


async def disconnect_irc(orch) -> None:
    orch._want_twitch = False
    orch._irc_fatal = False
    if orch.twitch:
        await orch.twitch.disconnect()
        orch.twitch = None


async def disconnect_events(orch) -> None:
    orch._want_events = False
    orch._eventsub_fatal = False
    orch._eventsub_error = ""
    if orch.eventsub:
        await orch.eventsub.disconnect()
        orch.eventsub = None


async def _twitch_watchdog(orch) -> None:
    delay = 5.0
    while not orch._stopping:
        try:
            await asyncio.sleep(delay)
            if not orch.settings.auto_reconnect_twitch or not (orch._want_twitch or orch._want_events):
                delay = 5.0
                continue
            if orch._want_twitch and (not (orch.twitch and orch.twitch.alive())):
                if orch._irc_fatal:
                    await orch.hub.patch_status(twitch="token rejected")
                else:
                    await orch.hub.patch_status(twitch="reconnecting")
                    await orch.hub.broadcast({"type": "log", "data": "Twitch chat dropped — reconnecting"})
                    try:
                        await orch._irc_connect()
                        delay = 5.0
                    except Exception as e:
                        if orch._irc_fatal:
                            await orch.hub.patch_status(twitch="token rejected", twitch_error=str(e))
                        else:
                            delay = min(delay * 2, 120.0)
                            await orch.hub.patch_status(twitch="error", twitch_error=str(e))
                            await orch.hub.broadcast({"type": "log", "data": f"Twitch reconnect failed: {e}"})
            if (
                orch._want_events
                and orch.settings.events_enabled
                and (not orch._eventsub_lock.locked())
                and (not (orch.eventsub and orch.eventsub.alive()))
            ):
                if orch._eventsub_fatal or (orch.eventsub and orch.eventsub.fatal):
                    orch._eventsub_fatal = True
                else:
                    await orch.hub.patch_status(eventsub="reconnecting")
                    await orch.hub.broadcast({"type": "log", "data": "EventSub dropped — reconnecting"})
                    await orch._eventsub_connect()
            if orch.eventsub and orch.eventsub.connected and (time.monotonic() >= orch._token_check_at):
                orch._token_check_at = time.monotonic() + TOKEN_REVALIDATE_SECONDS
                await orch._revalidate_event_token()
            irc_up = not orch._want_twitch or bool(orch.twitch and orch.twitch.alive()) or orch._irc_fatal
            events_up = (
                not orch._want_events
                or not orch.settings.events_enabled
                or orch._eventsub_fatal
                or bool(orch.eventsub and orch.eventsub.alive())
            )
            if irc_up and events_up:
                delay = 5.0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("twitch watchdog failed")


async def _validate_irc_token(orch) -> None:
    from src.twitch.auth import validate

    orch.irc_auth_note = ""
    try:
        meta = await validate(orch.settings.twitch_token)
    except Exception as exc:
        message = str(exc)
        if "HTTP 401" in message or "HTTP 403" in message:
            orch._irc_fatal = True
            await orch.hub.patch_status(twitch="token rejected", twitch_error=message)
            await orch.hub.broadcast({"type": "log", "data": message})
            return
        await orch.hub.broadcast({"type": "log", "data": f"IRC token check failed: {message}"})
        return
    problem = irc_scope_problem(meta, orch.settings.twitch_nick)
    if not problem:
        return
    orch.irc_auth_note = problem
    await orch.hub.patch_status(twitch_error=problem)
    await orch.hub.broadcast({"type": "log", "data": problem})


async def _revalidate_event_token(orch) -> None:
    if orch.eventsub is None:
        return
    state = await orch.eventsub.revalidate()
    if state != "token rejected":
        return
    orch._eventsub_fatal = True
    await orch.hub.patch_status(eventsub="token rejected")
    await orch.hub.broadcast({"type": "log", "data": "EventSub token rejected"})


async def _twitch_status(orch, state: str) -> None:
    if state == "token rejected":
        orch._irc_fatal = True
    await orch.hub.patch_status(twitch=state)


async def _eventsub_status(orch, state: str) -> None:
    if state == "token rejected":
        orch._eventsub_fatal = True
    await orch.hub.patch_status(eventsub=state)


def _shoutout_ready(orch) -> bool:
    wait = orch.settings.shoutout_cooldown_seconds
    if wait <= 0:
        return True
    return time.monotonic() - orch._last_shoutout_at >= wait


async def shoutout(orch, login: str | None) -> None:
    target = (login or orch._last_raider or "").lstrip("@").strip()
    if not target:
        raise ValueError("No raider to shout out")
    if not orch.settings.twitch_client_id:
        raise RuntimeError("TWITCH_CLIENT_ID is required for shoutouts")
    if not orch.eventsub or not orch.eventsub.broadcaster_id or (not orch.eventsub.moderator_id):
        raise RuntimeError("EventSub is not connected, so the broadcaster id is unknown")
    token = orch.settings.event_token()
    if target.lower() == orch._last_raider.lower() and orch._last_raider_id:
        to_id = orch._last_raider_id
    else:
        to_id = await _live("user_id")(token, orch.settings.twitch_client_id, target)
    try:
        await _live("shoutout")(
            token,
            orch.settings.twitch_client_id,
            from_broadcaster_id=orch.eventsub.broadcaster_id,
            to_broadcaster_id=to_id,
            moderator_id=orch.eventsub.moderator_id,
        )
    except HelixError as e:
        if e.status == 429:
            log.warning("Helix shoutout rate limited (HTTP 429): %s", e)
        if e.status in (401, 403):
            await orch.hub.patch_status(eventsub="token rejected", eventsub_error=str(e))
        raise
    orch._last_shoutout_at = time.monotonic()
    if orch._last_raider and target.lower() == orch._last_raider.lower():
        line = f"{target}, welcome in."
    else:
        template = orch.settings.shoutout_line or "go check out {user}"
        line = template.replace("{user}", target)
    await orch.say_line(line, post_chat=True, speak=orch.settings.shoutout_speak)


async def mark_moment(orch, note: str) -> dict[str, str]:
    text = (note or "bookmark").strip()
    orch.memory.add_note(STREAMER_THREAD, text, is_streamer=True)
    await asyncio.to_thread(orch.memory.flush)
    await orch.hub.broadcast({"type": "memory", "data": orch.memory.snapshot()})
    marker_error = ""
    if orch.eventsub and orch.eventsub.broadcaster_id and orch.settings.twitch_client_id:
        try:
            await _live("create_marker")(
                orch.settings.event_token(),
                orch.settings.twitch_client_id,
                orch.eventsub.broadcaster_id,
                text,
            )
        except Exception as e:
            marker_error = str(e)
            log.info("stream marker skipped: %s", e)
    return {"note": text, "marker_error": marker_error}


async def create_clip(orch) -> dict[str, str]:
    client = orch.eventsub
    scopes = getattr(client, "scopes", None) if client is not None else None
    if scopes is not None and "clips:edit" not in scopes:
        raise RuntimeError("missing scope clips:edit")
    if not orch.settings.twitch_client_id:
        raise RuntimeError("TWITCH_CLIENT_ID is required for clips")
    if client is None or not getattr(client, "broadcaster_id", ""):
        raise RuntimeError("EventSub is not connected, so the broadcaster id is unknown")
    result = await _live("create_clip")(
        orch.settings.event_token(),
        orch.settings.twitch_client_id,
        client.broadcaster_id,
    )
    clip_id = str(result.get("id") or "").strip()
    url = str(result.get("edit_url") or "").strip()
    note = url or clip_id
    if note:
        orch.memory.add_note(STREAMER_THREAD, note, is_streamer=True)
        await orch.hub.broadcast({"type": "memory", "data": orch.memory.snapshot()})
    await orch.hub.broadcast({"type": "log", "data": f"Clip {clip_id} {url}".strip()})
    return {"id": clip_id, "edit_url": url}
