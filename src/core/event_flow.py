"""Stream-event batching, priority, and replies."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from src.llm.persona import (
    mood_temperature,
)
from src.memory import (
    STREAMER_THREAD,
    is_generic_filler,
)
from src.recap import build_recap, write_recap
from src.safety import DEFLECT, fence_viewer_text, is_blocked
from src.twitch.eventsub import StreamEvent

log = logging.getLogger("cohost.orch")

EVENT_PRIORITY = {
    "raid": 0,
    "gift": 1,
    "sub": 2,
    "resub": 3,
    "cheer": 4,
    "hype_begin": 5,
    "hype_end": 5,
    "redeem": 6,
    "follow": 7,
    "poll_start": 8,
    "poll_end": 8,
    "prediction_start": 8,
    "prediction_end": 8,
}


async def on_stream_event(orch, event: StreamEvent) -> None:
    await orch.hub.broadcast(
        {
            "type": "alert",
            "data": {"kind": event.kind, "user": event.user, "people": event.people(), "text": event.scene()},
        }
    )
    await orch.hub.broadcast({"type": "log", "data": f"Alert {event.kind}: {event.scene()}"})
    if event.kind in ("hype_progress", "hype_begin", "hype_end"):
        await _broadcast_hype(orch, event)
        if event.kind == "hype_progress":
            return
    if event.kind == "stream_online":
        session = orch.memory.start_session()
        await orch.hub.broadcast({"type": "log", "data": f"Session {session}"})
        return
    if event.kind == "stream_offline":
        moment = datetime.now()
        _text, path = write_recap(
            orch.memory,
            orch.data_dir / "recaps",
            when=moment,
            filename=f"{moment:%Y-%m-%d-%H%M}.md",
        )
        await orch.hub.broadcast({"type": "log", "data": f"Recap {path.name}"})
        if orch.settings.idle_on_offline:
            await orch.set_listen(False)
        return
    if event.kind == "ad_break":
        await orch.say_line(event.scene(), post_chat=True, speak=False)
        return
    if event.kind in ("shoutout_in", "shoutout_out", "raid_out"):
        await orch.say_line(event.scene(), post_chat=True, speak=bool(orch.settings.events_speak))
        return
    if event.kind == "raid":
        orch._last_raider = event.user
        orch._last_raider_id = str(event.extra.get("from_id") or "")
    if event.kind == "redeem":
        title = str(event.extra.get("title") or "")
        spec = orch.settings.reward_spec(title)
        if spec.action == "ignore":
            await orch._note_skip("reward ignored", event.user)
            return
        key = title.strip().lower()
        if spec.cooldown > 0 and time.monotonic() - orch._reward_at.get(key, 0.0) < spec.cooldown:
            await orch._note_skip("reward cooldown", event.user)
            return
        if spec.cooldown > 0:
            orch._reward_at[key] = time.monotonic()
        event.extra["delivery"] = spec.action
        if spec.prompt:
            event.extra["reward_prompt"] = spec.prompt
    if event.kind == "follow":
        if not orch.settings.announce_follows:
            return
        orch._follow_names.extend(event.people())
        if not orch._follow_task or orch._follow_task.done():
            orch._follow_task = orch._spawn(orch._flush_follows(), "follow-batch")
        return
    if event.kind == "cheer":
        orch._cheers.append(event)
        if not orch._cheer_task or orch._cheer_task.done():
            orch._cheer_task = orch._spawn(orch._flush_cheers(), "cheer-batch")
        return
    orch._event_pending.append(event)
    orch._kick_events()


def _hype_message(orch, event: StreamEvent) -> dict:
    extra = event.extra or {}
    return {
        "level": extra.get("level") or 1,
        "progress": extra.get("progress") or 0,
        "goal": extra.get("goal") or 0,
        "total": extra.get("total") or 0,
        "expires_at": extra.get("expires_at") or "",
        "active": event.kind != "hype_end",
        "hold_seconds": orch.settings.overlay_alert_hold,
    }


async def _broadcast_hype(orch, event: StreamEvent) -> None:
    payload = _hype_message(orch, event)
    await orch.hub.broadcast({"type": "hype", "data": payload})
    await orch.hub.patch_status(hype=payload, hype_level=payload["level"])


async def _flush_follows(orch) -> None:
    try:
        await asyncio.sleep(orch.settings.follow_batch_seconds)
    except asyncio.CancelledError:
        return
    await orch._emit_follows()


async def _emit_follows(orch) -> None:
    names = [n for n in orch._follow_names if n]
    orch._follow_names = []
    if not names:
        return
    batched = StreamEvent(kind="follow", user=names[0], users=names[1:])
    orch._event_pending.append(batched)
    orch._kick_events()


async def _flush_cheers(orch) -> None:
    try:
        await asyncio.sleep(orch.settings.follow_batch_seconds)
    except asyncio.CancelledError:
        return
    await orch._emit_cheers()


async def _emit_cheers(orch) -> None:
    cheers = list(orch._cheers)
    orch._cheers = []
    if not cheers:
        return
    names: list[str] = []
    total = 0
    for ev in cheers:
        names.extend(ev.people())
        try:
            total += int(ev.extra.get("bits") or 0)
        except (TypeError, ValueError):
            pass
    batched = StreamEvent(
        kind="cheer",
        user=names[0] if names else "Someone",
        users=names[1:],
        extra={"bits": total, "total_bits": total},
    )
    orch._event_pending.append(batched)
    orch._kick_events()


def _kick_events(orch) -> None:
    if orch._event_scheduled or orch._stopping:
        return
    orch._event_scheduled = True
    try:
        orch._event_q.put_nowait(1)
    except Exception:
        orch._event_scheduled = False


async def _event_worker(orch) -> None:
    while not orch._stopping:
        await orch._event_q.get()
        orch._event_scheduled = False
        try:
            await orch._drain_events()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event worker failed")


async def _drain_events(orch) -> None:
    while orch._event_pending and (not orch._stopping):
        if orch._busy_voice:
            await orch._note_skip("busy on mic", "")
            await asyncio.sleep(0.25)
            orch._kick_events()
            return
        event = min(orch._event_pending, key=lambda e: EVENT_PRIORITY.get(e.kind, 9))
        try:
            orch._event_pending.remove(event)
        except ValueError:
            break
        await orch._publish_queue()
        await orch._react_event(event)
    if orch._chat_pending and (not orch._busy_voice):
        orch._kick_chat()


async def _react_event(orch, event: StreamEvent) -> None:
    if orch._busy_voice:
        orch._event_pending.appendleft(event)
        orch._kick_events()
        return
    people = event.people()
    primary = people[0] if people else event.user or "chat"
    thread = (
        orch.memory.viewer_thread_key(primary)
        if primary and primary.lower() not in ("anonymous", "chat")
        else STREAMER_THREAD
    )
    reward_input = str(event.extra.get("input") or "")
    if event.kind == "redeem" and is_blocked(reward_input):
        orch.memory.remember("twitch_event", primary, "[skipped topic]", via="event", thread=thread)
        orch._spawn(orch.hub.broadcast({"type": "memory", "data": orch.memory.snapshot()}), "memory")
        await orch._emit_cohost(DEFLECT, thread=thread, speak=False, post=orch.settings.events_post_to_chat)
        return
    scene = event.scene()
    orch.memory.remember("twitch_event", primary, scene, via="event", thread=thread)
    orch._spawn(orch.hub.broadcast({"type": "memory", "data": orch.memory.snapshot()}), "memory")
    delivery = str(event.extra.get("delivery") or ("speak" if orch.settings.events_speak else "chat"))
    if delivery == "ignore":
        return
    if is_blocked(scene):
        reply = DEFLECT
    else:
        temp = mood_temperature("hype" if event.kind == "raid" and orch.mood == "normal" else orch.mood)
        reward_prompt = str(event.extra.get("reward_prompt") or "").strip()
        if event.kind == "redeem" and reward_prompt:
            user_content = (
                reward_prompt.replace("{user}", primary).replace("{input}", fence_viewer_text(reward_input))
                + "\n\nWords to speak aloud only."
            )
        else:
            user_content = f"You're on the couch next to {orch.identity.spoken_name()}. This just happened on stream:\n{scene}\n\nHype it like a cohost for a second, then drop it. No corporate thank-you. Words to speak aloud only."
        try:
            reply = await orch._finish_reply(
                [{"role": "system", "content": orch.voice_prompt}, {"role": "user", "content": user_content}],
                temperature=temp,
            )
        except TimeoutError:
            await orch._note_skip("llm timeout", primary)
            await orch.hub.patch_status(phase="listening" if orch.mic.listen_enabled else "idle")
            return
    if not reply:
        await orch.hub.patch_status(phase="listening" if orch.mic.listen_enabled else "idle")
        return
    await orch._emit_cohost(
        reply,
        thread=thread,
        speak=delivery == "speak" and orch.settings.events_speak,
        post=orch.settings.events_post_to_chat,
    )


async def _bounded_complete(orch, messages: list[dict[str, str]], temperature: float) -> str:
    return await asyncio.wait_for(
        orch.llm.complete(messages, temperature=temperature), timeout=orch.settings.chat_timeout_seconds
    )


async def _finish_reply(
    orch, messages: list[dict[str, str]], *, temperature: float, retry: bool = True
) -> str:
    raw = await orch._bounded_complete(messages, temperature)
    reply = orch._clean(raw)
    if retry and (not reply or is_generic_filler(reply, orch._banned_extra())):
        raw = await orch._bounded_complete(
            messages + [{"role": "user", "content": "shorter, messier, no greeting, no thank-you template."}],
            min(1.0, temperature + 0.05),
        )
        reply = orch._clean(raw)
    if not reply or is_generic_filler(reply, orch._banned_extra()):
        return ""
    if is_blocked(reply):
        return DEFLECT
    return reply


async def make_recap(orch, *, vibe: bool = False) -> dict[str, str]:
    paragraph = ""
    if vibe:
        draft = build_recap(orch.memory)
        try:
            raw = await asyncio.wait_for(
                orch.llm.complete(
                    [
                        {
                            "role": "system",
                            "content": "Write one short paragraph about this stream. No headings.",
                        },
                        {"role": "user", "content": draft or "Quiet stream."},
                    ],
                    temperature=0.7,
                ),
                timeout=orch.settings.chat_timeout_seconds,
            )
        except TimeoutError:
            paragraph = ""
        else:
            paragraph = (raw or "").strip()
    text, path = write_recap(orch.memory, orch.data_dir / "recaps", vibe=paragraph)
    return {"markdown": text, "path": path.name}
