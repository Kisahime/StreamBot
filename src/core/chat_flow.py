"""Chat pick, rate, decide, and perform."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from src.llm.persona import (
    mood_temperature,
)
from src.memory import (
    STREAMER_THREAD,
    ensure_chat_mention,
    for_speech,
    is_generic_filler,
    sanitize_reply,
    streamer_smalltalk_fallback,
)
from src.policy import chat_action, control_command_names, mention_waits, pixel_control
from src.safety import DEFLECT, fence_viewer_text, is_blocked
from src.twitch.chat import ChatLine

log = logging.getLogger("cohost.orch")


def _kick_chat(orch) -> None:
    if orch._chat_scheduled or orch._stopping:
        return
    orch._chat_scheduled = True
    try:
        orch._chat_q.put_nowait(1)
    except Exception:
        orch._chat_scheduled = False


async def on_chat(orch, line: ChatLine) -> None:
    if orch.identity.is_login(line.user):
        name = orch.identity.spoken_name()
        stored = "[skipped topic]" if is_blocked(line.text) else line.text
        orch.memory.remember("streamer_chat", name, stored, via="chat", thread=STREAMER_THREAD)
        await orch.hub.broadcast(
            {
                "type": "chat",
                "data": {"user": name, "text": line.text, "mention": line.mention, "streamer": True},
            }
        )
        orch._spawn(orch.hub.broadcast({"type": "memory", "data": orch.memory.snapshot()}), "memory")
        orch._chat_pending.append(line)
        orch._kick_chat()
        return
    orch._chat_pending.append(line)
    stored = "[skipped topic]" if is_blocked(line.text) else line.text
    orch.memory.remember(
        "viewer", line.user, stored, via="chat", thread=orch.memory.viewer_thread_key(line.user)
    )
    await orch.hub.broadcast(
        {"type": "chat", "data": {"user": line.user, "text": line.text, "mention": line.mention}}
    )
    orch._spawn(orch.hub.broadcast({"type": "memory", "data": orch.memory.snapshot()}), "memory")
    await orch._publish_queue()
    orch._kick_chat()


def _rate_ok(orch, *, mention: bool, user: str = "") -> tuple[bool, str]:
    now = time.monotonic()
    while orch._reply_times and now - orch._reply_times[0] > 60:
        orch._reply_times.popleft()
    if len(orch._reply_times) >= orch.settings.max_replies_per_minute:
        return (False, "cooldown")
    if orch.identity.is_login(user):
        if now - orch._last_chat_reply < 1.5:
            return (False, "cooldown")
        return (True, "")
    global_wait, user_wait = mention_waits(
        mention,
        orch.settings.mention_boost,
        orch.settings.cooldown_seconds,
        orch.settings.per_user_cooldown_seconds,
    )
    key = (user or "").lower()
    if key:
        last = orch._user_reply_at.get(key, 0.0)
        if now - last < user_wait:
            return (False, "per-user cooldown")
    if now - orch._last_chat_reply < global_wait:
        return (False, "cooldown")
    return (True, "")


def _mark_reply(orch, user: str = "") -> None:
    now = time.monotonic()
    orch._last_chat_reply = now
    orch._reply_times.append(now)
    if user:
        orch._user_reply_at[user.lower()] = now


async def _chat_scan_loop(orch) -> None:
    while not orch._stopping:
        try:
            await asyncio.sleep(orch.settings.scan_interval_seconds)
            orch._kick_chat()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("chat scan failed")


async def _chat_worker(orch) -> None:
    while not orch._stopping:
        await orch._chat_q.get()
        orch._chat_scheduled = False
        try:
            await orch._drain_chat()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("chat worker failed")


async def _expire_stale_chat(orch) -> None:
    limit = orch.settings.chat_max_age_seconds
    if limit <= 0 or not orch._chat_pending:
        return
    now = time.monotonic()
    kept: deque[ChatLine] = deque(maxlen=orch._chat_pending.maxlen)
    for line in list(orch._chat_pending):
        age = now - float(getattr(line, "ts", now))
        exempt = orch.identity.is_login(line.user) or bool(line.command)
        if age > limit and (not exempt):
            await orch._note_skip("stale", line.user)
            continue
        kept.append(line)
    orch._chat_pending.clear()
    orch._chat_pending.extend(kept)


async def _drain_chat(orch) -> None:
    await orch._expire_stale_chat()
    while orch._chat_pending and (not orch._stopping):
        if orch._busy_voice or orch._event_pending:
            reason = "busy on mic" if orch._busy_voice else "alert queued"
            await orch._note_skip(reason, "")
            await asyncio.sleep(0.25)
            orch._kick_chat()
            return
        handled = await orch._consider_chat_locked()
        if not handled:
            return


def _pick_chat_target(orch) -> ChatLine | None:
    if not orch._chat_pending:
        return None
    streamer_lines = [line for line in orch._chat_pending if orch.identity.is_login(line.user)]
    if streamer_lines:
        return streamer_lines[0]
    commands = [line for line in orch._chat_pending if line.command]
    if commands:
        return commands[-1]
    engaging = [line for line in orch._chat_pending if line.is_engaging()]
    if engaging:
        return engaging[-1]
    if orch.settings.welcome_first_time:
        firsts = [line for line in orch._chat_pending if line.first_msg and (not line.command)]
        if firsts:
            return firsts[-1]
    return orch._chat_pending[-1]


def _drop_chat_target(orch, target: ChatLine) -> None:
    if orch.identity.is_login(target.user):
        kept = [line for line in orch._chat_pending if line is not target]
    else:
        kept = [
            line
            for line in orch._chat_pending
            if line is not target and line.user.lower() != target.user.lower()
        ]
    orch._chat_pending.clear()
    orch._chat_pending.extend(kept)


def _control_ready(orch) -> bool:
    wait = orch.settings.control_cooldown_seconds
    if wait <= 0:
        return True
    return time.monotonic() - orch._last_control_at >= wait


async def _type_ack(orch, text: str) -> None:
    if orch.twitch and orch.twitch.connected:
        await orch.twitch.send(text)
    await orch.hub.broadcast(
        {"type": "chat", "data": {"user": orch.persona.get("name"), "text": text, "bot": True}}
    )


async def _run_pixel_control(orch, verb: str) -> None:
    if verb == "mute":
        await orch.set_mute(True)
        await orch._type_ack("Muted.")
    elif verb == "unmute":
        await orch.set_mute(False)
        await orch._type_ack("Voice is back.")
    elif verb == "chill":
        await orch.set_mood("chill")
        await orch._type_ack("Mood is chill.")
    elif verb == "hype":
        await orch.set_mood("hype")
        await orch._type_ack("Mood is hype.")
    elif verb == "stop":
        await orch.interrupt()
        await orch._type_ack("Stopped.")


async def _consider_chat_locked(orch) -> bool:
    if orch._busy_voice:
        return False
    if orch._event_pending:
        orch._kick_events()
        return False
    target = orch._pick_chat_target()
    if target is None:
        return False
    if pixel_control(target, orch._control_names()):
        orch._drop_chat_target(target)
        await orch._publish_queue()
        await orch._perform_chat(target, ignore_rate=False)
        return True
    action = chat_action(target, welcome_first_time=orch.settings.welcome_first_time)
    ok, why = orch._rate_ok(mention=action == "speak", user=target.user)
    if not ok:
        await orch._note_skip(why, target.user)
        return False
    orch._drop_chat_target(target)
    await orch._publish_queue()
    await orch._perform_chat(target, ignore_rate=False)
    return True


async def _perform_chat(orch, target: ChatLine, *, ignore_rate: bool) -> None:
    verb = pixel_control(target, orch._control_names())
    if verb:
        if not orch._can_run_mod_command(target):
            await orch._note_skip("not a mod", target.user)
            return
        if not ignore_rate and (not orch._control_ready()):
            await orch._note_skip("control cooldown", target.user)
            return
        await orch._run_pixel_control(verb)
        orch._last_control_at = time.monotonic()
        orch._mark_reply(target.user)
        return
    action = chat_action(target, welcome_first_time=orch.settings.welcome_first_time)
    command = (target.command or "").lower()
    if command in orch.settings.mod_commands and (not orch._can_run_mod_command(target)):
        await orch._note_skip("not a mod", target.user)
        return
    if command == "!so":
        who = ""
        parts = target.text.split()
        if len(parts) > 1:
            who = parts[1].lstrip("@")
        if not ignore_rate and (not orch._shoutout_ready()):
            await orch._note_skip("shoutout cooldown", target.user)
            return
        try:
            await orch.shoutout(who or None)
        except Exception as e:
            await orch.hub.broadcast({"type": "log", "data": f"Shoutout failed: {e}"})
        else:
            orch._last_shoutout_at = time.monotonic()
        orch._mark_reply(target.user)
        return
    if action == "command":
        await orch._note_skip("unhandled command", target.user)
        return
    await orch._decide_chat(target, action)


async def _decide_chat(orch, line: ChatLine, action: str) -> None:
    if orch._busy_voice:
        orch._chat_pending.append(line)
        return
    await orch.hub.patch_status(phase="thinking")
    streamer = orch.identity.is_login(line.user)
    previous = ""
    prev = orch.memory.viewer(STREAMER_THREAD if streamer else line.user)
    if prev and prev.last_reply:
        previous = prev.last_reply
    recent = [
        msg["content"]
        for msg in orch.memory.llm_messages(
            STREAMER_THREAD if streamer else orch.memory.viewer_thread_key(line.user)
        )
        if msg["role"] == "assistant"
    ]
    if is_blocked(line.text):
        reply = DEFLECT
        action = "chat"
    else:
        try:
            reply = orch._clean(await orch._persona_chat_reply(line, ambient=action != "speak"))
            if orch._reply_is_bad(reply, previous, recent):
                reply = orch._clean(
                    await orch._persona_chat_reply(line, ambient=action != "speak", retry=True)
                )
        except TimeoutError:
            await orch._note_skip("llm timeout", line.user)
            await orch.hub.patch_status(phase="listening" if orch.mic.listen_enabled else "idle")
            return
        if orch._reply_is_bad(reply, previous, recent):
            fallback = streamer_smalltalk_fallback(line.text, orch._cohost_name()) if streamer else ""
            if not fallback:
                await orch._note_skip("filler rejected", line.user)
                await orch.hub.broadcast(
                    {"type": "log", "data": f"Chat skip ({line.user}): {line.text[:80]}"}
                )
                await orch.hub.patch_status(phase="listening" if orch.mic.listen_enabled else "idle")
                return
            reply = fallback
        if is_blocked(reply):
            reply = DEFLECT
            action = "chat"
    if streamer:
        chat_text = orch._chat_text(sanitize_reply(reply, str(orch.persona.get("name") or "")))
    else:
        chat_text = orch._chat_text(ensure_chat_mention(reply, line.user), target_user=line.user)
    if not chat_text:
        await orch._note_skip("output guard", line.user)
        await orch.hub.patch_status(phase="listening" if orch.mic.listen_enabled else "idle")
        return
    spoken = for_speech(chat_text)
    thread_key = STREAMER_THREAD if streamer else orch.memory.viewer_thread_key(line.user)
    orch._mark_reply(line.user)
    orch.memory.remember("cohost_chat", orch._cohost_name(), chat_text, via="chat", thread=thread_key)
    orch._remember_caption(spoken)
    orch.memory.note_reply_to(line.user, chat_text, thread=thread_key)
    if not streamer:
        orch.memory.mark_greeted(line.user)
    orch._spawn(orch.hub.broadcast({"type": "memory", "data": orch.memory.snapshot()}), "memory")
    await orch.hub.broadcast(
        {"type": "transcript", "data": {"role": "cohost", "text": spoken, "chat": chat_text}}
    )
    await orch.hub.broadcast(
        {"type": "chat", "data": {"user": orch.persona.get("name"), "text": chat_text, "bot": True}}
    )
    if orch.twitch and orch.twitch.connected:
        try:
            await orch.twitch.send(chat_text)
        except Exception:
            log.exception("chat send failed")
    want_speak = (
        action == "speak" and (not orch.tts_mute) and (not orch._busy_voice) and (not orch.tts.speaking)
    )
    if want_speak:
        await orch.hub.patch_status(phase="speaking", speaking=True, caption=spoken)
        await orch._speak(spoken)
        await orch.hub.patch_status(phase="listening" if orch.mic.listen_enabled else "idle", speaking=False)
    else:
        await orch.hub.patch_status(phase="listening" if orch.mic.listen_enabled else "idle", caption=spoken)


async def _persona_chat_reply(orch, line: ChatLine, ambient: bool = False, retry: bool = False) -> str:
    extra = (
        "\nAnswer only the message above. No greeting by name. Do not ask what they want to talk about. Do not mention clips or moments.\n"
        if retry
        else ""
    )
    prev = orch.memory.viewer(STREAMER_THREAD if orch.identity.is_login(line.user) else line.user)
    prev_bit = ""
    if prev and prev.last_reply and (not is_generic_filler(prev.last_reply, orch._banned_extra())):
        prev_bit = f"\nYou already said to them: {prev.last_reply}\nDon't repeat that.\n"
    name = orch.identity.spoken_name()
    fenced = fence_viewer_text(line.text)
    if orch.identity.is_login(line.user):
        focus = STREAMER_THREAD
        who_line = f"{name} (the streamer, sitting next to you) just typed:\n{fenced}\n"
        ask = f"Reply only to the latest line inside the viewer markers. One short line. Do not repeat a joke or sentence you already used. Do not start with Hey {name}. Do not ask them for memes, clips, or a topic."
    else:
        focus = orch.memory.viewer_thread_key(line.user)
        who_line = f"{line.user} in chat:\n{fenced}\nThis is a viewer, not {name}.\n"
        ask = f"One short Twitch chat line starting with @{line.user}."
        if orch.settings.welcome_first_time and line.first_msg:
            ask += " This is their first message ever in this channel. Give a short typed welcome."
    history_limit = 4 if ambient else None
    messages = [
        {"role": "system", "content": orch.chat_prompt},
        *orch._history_messages(focus, limit=history_limit),
        {
            "role": "user",
            "content": f"{who_line}{prev_bit}{orch.memory.prompt_block(focus, streamer_name=name, is_voice=False, include_alerts=not ambient)}\n\nLive chat:\n{orch._chat_context(orch.settings.ambient_chat_context if ambient else None)}\n{extra}{ask}",
        },
    ]
    try:
        text = orch._clean(
            await asyncio.wait_for(
                orch.llm.complete(messages, temperature=mood_temperature(orch.mood) if not retry else 0.9),
                timeout=orch.settings.chat_timeout_seconds,
            )
        )
    except TimeoutError:
        orch.llm.last_error = "llm timeout"
        raise
    if not text:
        return ""
    target = None if orch.identity.is_login(line.user) else line.user
    return orch._chat_text(text, target_user=target)


def _control_names(orch) -> set[str]:
    return control_command_names(str(orch.persona.get("name") or ""), orch.settings.control_command)


def _irc_commands(orch) -> list[str]:
    commands = list(orch.settings.chat_commands)
    for name in sorted(orch._control_names()):
        if name not in commands:
            commands.append(name)
    return commands


def _can_run_mod_command(orch, line: ChatLine) -> bool:
    if line.is_mod or line.is_broadcaster:
        return True
    return orch.identity.is_login(line.user)
