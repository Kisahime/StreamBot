"""Mic utterance to spoken reply."""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from src.audio.vad import trim_to_speech
from src.llm.persona import (
    mood_temperature,
)
from src.memory import (
    STREAMER_THREAD,
    apply_catchphrase_budget,
    for_speech,
    is_generic_filler,
    repeats_recent,
    sanitize_reply,
)
from src.safety import DEFLECT, clean_chat_output, fence_viewer_text, is_blocked

log = logging.getLogger("cohost.orch")


def _bump_voice(orch) -> int:
    orch._voice_gen += 1
    orch._voice_cancel.set()
    orch._voice_cancel = asyncio.Event()
    orch.tts.interrupt()
    return orch._voice_gen


async def interrupt(orch) -> None:
    orch._bump_voice()
    orch.duck.up()
    await orch.hub.patch_status(
        speaking=False, caption="", phase="listening" if orch.mic.listen_enabled else "idle"
    )


def _clean(orch, raw: str) -> str:
    reply = sanitize_reply(raw, str(orch.persona.get("name") or ""))
    reply = apply_catchphrase_budget(
        reply, orch._phrases(), orch._catchphrase_at, window=orch.settings.catchphrase_seconds
    )
    return reply


def _chat_text(orch, text: str, target_user: str | None = None) -> str:
    return clean_chat_output(
        text, target_user, allow_links=orch.settings.allow_links, max_chars=orch.settings.chat_max_chars
    )


async def _emit_cohost(orch, reply: str, *, thread: str, speak: bool, post: bool) -> None:
    spoken = for_speech(reply)
    chat_text = orch._chat_text(reply)
    orch.memory.remember(
        "cohost_voice" if speak else "cohost_chat",
        orch._cohost_name(),
        spoken if speak else chat_text,
        via="voice" if speak else "chat",
        thread=thread,
    )
    orch._spawn(orch.hub.broadcast({"type": "memory", "data": orch.memory.snapshot()}), "memory")
    orch._remember_caption(spoken)
    await orch.hub.broadcast({"type": "transcript", "data": {"role": "cohost", "text": spoken}})
    if post and orch.twitch and orch.twitch.connected:
        if not chat_text:
            await orch._note_skip("output guard", "")
        else:
            try:
                t0 = time.perf_counter()
                await orch.twitch.send(chat_text)
                orch.timings["chat_send_ms"] = int((time.perf_counter() - t0) * 1000)
                await orch.hub.broadcast(
                    {
                        "type": "chat",
                        "data": {"user": orch.persona.get("name"), "text": chat_text, "bot": True},
                    }
                )
            except Exception:
                log.exception("event chat send failed")
    if speak and (not orch.tts_mute) and (not orch._busy_voice):
        await orch.hub.patch_status(
            phase="speaking", speaking=True, caption=spoken, timings=dict(orch.timings)
        )
        await orch._speak(spoken)
    await orch.hub.patch_status(
        phase="listening" if orch.mic.listen_enabled else "idle",
        speaking=False,
        caption=spoken[-280:],
        timings=dict(orch.timings),
    )


async def _speak(orch, text: str) -> None:
    if not text or orch.tts_mute:
        return
    orch._speak_depth += 1
    if orch._speak_depth == 1:
        orch.duck.on_speaking(True)
        await orch.hub.patch_status(duck=orch.duck.mode, speaking=True)
    try:
        await orch.tts.speak(text)
    finally:
        orch._speak_depth = max(0, orch._speak_depth - 1)
        if orch._speak_depth == 0:
            orch.duck.on_speaking(False)


async def on_utterance(orch, audio: np.ndarray) -> None:
    gen = orch._bump_voice()
    orch._busy_voice = True
    started = time.perf_counter()
    try:
        if orch.stt.model is None:
            await orch.hub.broadcast({"type": "log", "data": "Whisper is still loading or failed to load."})
            return
        sr = orch.settings.sample_rate
        clipped = await asyncio.to_thread(trim_to_speech, audio, sr)
        await orch.hub.patch_status(phase="thinking", speaking=False)
        try:
            text = await asyncio.to_thread(orch.stt.transcribe, clipped)
        except Exception as e:
            log.exception("transcribe failed")
            await orch.hub.broadcast({"type": "log", "data": f"STT error: {e}"})
            await orch.hub.patch_status(phase="listening" if orch.mic.listen_enabled else "idle")
            return
        orch.timings["stt_ms"] = int((time.perf_counter() - started) * 1000)
        if gen != orch._voice_gen:
            return
        if not text:
            await orch.hub.patch_status(
                phase="listening" if orch.mic.listen_enabled else "idle", timings=dict(orch.timings)
            )
            return
        await orch._handle_voice_text(text, gen, started)
    finally:
        orch._busy_voice = False
        if orch._event_pending:
            orch._kick_events()
        elif orch._chat_pending:
            orch._kick_chat()


async def pretend(orch, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    gen = orch._bump_voice()
    orch._busy_voice = True
    try:
        await orch._handle_voice_text(text, gen, time.perf_counter())
    finally:
        orch._busy_voice = False
        if orch._event_pending:
            orch._kick_events()
        elif orch._chat_pending:
            orch._kick_chat()


async def _handle_voice_text(orch, text: str, gen: int, started: float) -> None:
    await orch.hub.patch_status(
        last_user=text, streamer_name=orch.identity.spoken_name(), timings=dict(orch.timings)
    )
    await orch.hub.broadcast(
        {
            "type": "transcript",
            "data": {"role": "streamer", "text": text, "name": orch.identity.spoken_name()},
        }
    )
    stored = "[skipped topic]" if is_blocked(text) else text
    orch.memory.remember(
        "streamer_voice", orch.identity.spoken_name(), stored, via="voice", thread=STREAMER_THREAD
    )
    await orch._voice_reply(text, gen, started)


def _remember_caption(orch, text: str) -> None:
    cleaned = (text or "").strip()
    if cleaned:
        orch._last_caption = cleaned


async def replay_last(orch) -> bool:
    text = (orch._last_caption or "").strip()
    if not text:
        return False
    await orch.say_line(text, post_chat=False)
    return True


async def say_line(orch, text: str, post_chat: bool = True, speak: bool = True) -> None:
    reply = sanitize_reply(text, str(orch.persona.get("name") or ""))
    chat_text = orch._chat_text(reply)
    if not chat_text:
        await orch._note_skip("output guard", "")
        return
    spoken = for_speech(chat_text)
    orch._remember_caption(spoken)
    if post_chat and orch.twitch and orch.twitch.connected:
        await orch.twitch.send(chat_text)
        await orch.hub.broadcast(
            {"type": "chat", "data": {"user": orch.persona.get("name"), "text": chat_text, "bot": True}}
        )
    await orch.hub.broadcast({"type": "transcript", "data": {"role": "cohost", "text": spoken}})
    if speak and (not orch.tts_mute):
        await orch.hub.patch_status(phase="speaking", speaking=True, caption=spoken)
        await orch._speak(spoken)
    await orch.hub.patch_status(
        phase="listening" if orch.mic.listen_enabled else "idle", speaking=False, caption=spoken[-280:]
    )


def _chat_context(orch, limit: int | None = None) -> str:
    n = orch.settings.chat_context_lines if limit is None else limit
    if not orch.twitch:
        return "(no chat connected)"
    lines = orch.twitch.recent(n)
    rendered = []
    for line in lines:
        if is_blocked(line.text):
            continue
        rendered.append(f"{line.user}: {fence_viewer_text(line.text)}")
    return "\n".join(rendered) if rendered else "(chat is quiet)"


def _history_messages(orch, thread_key: str, limit: int | None = None) -> list[dict[str, str]]:
    banned = orch._banned_extra()
    kept = []
    for msg in orch.memory.llm_messages(thread_key, limit=limit):
        if msg["role"] == "assistant" and is_generic_filler(msg["content"], banned):
            continue
        kept.append(msg)
    return kept


def _reply_is_bad(orch, reply: str, previous: str, recent: list[str] | None = None) -> bool:
    if not reply or is_generic_filler(reply, orch._banned_extra()):
        return True
    earlier = [previous, *(recent or [])]
    return repeats_recent(reply, earlier)


async def _voice_reply(orch, user_text: str, gen: int, started: float) -> None:
    cancel = orch._voice_cancel
    name = orch.identity.spoken_name()
    if is_blocked(user_text):
        reply = DEFLECT
        if gen != orch._voice_gen:
            return
        if not orch.tts_mute:
            await orch.hub.patch_status(phase="speaking", speaking=True, caption=reply)
            await orch._speak(reply)
        await orch._emit_voice_result(name, user_text, reply, gen, started, post=False)
        return
    messages = [
        {"role": "system", "content": orch.voice_prompt},
        *orch._history_messages(STREAMER_THREAD),
        {
            "role": "user",
            "content": f"You're on the couch next to {name}. They just said into the mic:\n{fence_viewer_text(user_text)}\n\n{orch.memory.prompt_block(STREAMER_THREAD, streamer_name=name, is_voice=True)}\n\nPeople talking in chat (not {name}):\n{orch._chat_context()}\n\nReact to {name} like you're sitting next to them. Don't recap. Don't @ anyone.",
        },
    ]
    full: list[str] = []
    first_token = False
    first_audio = False
    async for sentence in orch.llm.stream_sentences(
        messages,
        temperature=mood_temperature(orch.mood),
        cancel=cancel,
        max_chars=orch.settings.max_sentence_chars,
    ):
        if gen != orch._voice_gen or cancel.is_set():
            return
        if not first_token:
            first_token = True
            orch.timings["first_token_ms"] = int((time.perf_counter() - started) * 1000)
        sentence = orch._clean(sentence.strip())
        if not sentence or is_generic_filler(sentence, orch._banned_extra()):
            continue
        if is_blocked(sentence):
            sentence = DEFLECT
        full.append(sentence)
        await orch.hub.patch_status(
            phase="speaking", speaking=True, caption=" ".join(full)[-280:], timings=dict(orch.timings)
        )
        if not orch.tts_mute:
            await orch._speak(sentence)
            if not first_audio:
                first_audio = True
                orch.timings["first_audio_ms"] = int((time.perf_counter() - started) * 1000)
        if gen != orch._voice_gen:
            return
    reply = orch._clean(" ".join(full).strip())
    if not reply:
        err = orch.llm.last_error or "empty model reply"
        await orch.hub.broadcast({"type": "log", "data": f"LLM: {err}"})
        await orch.hub.patch_status(phase="listening" if orch.mic.listen_enabled else "idle", speaking=False)
        return
    if gen != orch._voice_gen:
        return
    await orch._emit_voice_result(name, user_text, reply, gen, started, post=True)


async def _emit_voice_result(
    orch, name: str, user_text: str, reply: str, gen: int, started: float, post: bool
) -> None:
    if gen != orch._voice_gen:
        return
    orch.memory.remember("cohost_voice", orch._cohost_name(), reply, via="voice", thread=STREAMER_THREAD)
    orch._spawn(orch.hub.broadcast({"type": "memory", "data": orch.memory.snapshot()}), "memory")
    orch._remember_caption(reply)
    await orch.hub.broadcast({"type": "transcript", "data": {"role": "cohost", "text": reply}})
    if post and orch.settings.post_voice_replies_to_chat and orch.twitch and orch.twitch.connected:
        if gen != orch._voice_gen:
            return
        short = orch._chat_text(reply)
        if not short:
            await orch._note_skip("output guard", "")
        else:
            try:
                t0 = time.perf_counter()
                await orch.twitch.send(short)
                orch.timings["chat_send_ms"] = int((time.perf_counter() - t0) * 1000)
                orch.memory.remember(
                    "cohost_chat", orch._cohost_name(), short, via="chat", thread=STREAMER_THREAD
                )
                await orch.hub.broadcast(
                    {"type": "chat", "data": {"user": orch.persona.get("name"), "text": short, "bot": True}}
                )
            except Exception:
                log.exception("failed to echo voice reply to chat")
    await orch.hub.patch_status(
        phase="listening" if orch.mic.listen_enabled else "idle",
        speaking=False,
        caption=reply[-280:],
        timings=dict(orch.timings),
    )


async def _level_loop(orch) -> None:
    while not orch._stopping:
        try:
            await asyncio.sleep(0.2)
            wake = getattr(orch.mic, "wake", None)
            wake_loaded = getattr(wake, "model", None) is not None
            stream_open = getattr(orch.mic, "_stream", None) is not None
            if stream_open and (orch.mic.listen_enabled or orch.mic.ptt_down or wake_loaded):
                score = float(getattr(orch.mic, "wake_score", getattr(wake, "score", 0.0)) or 0.0)
                await orch.hub.broadcast(
                    {
                        "type": "level",
                        "data": {"level": round(float(orch.mic.level), 4), "wake": round(score, 4)},
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("level loop failed")
