"""Orchestrator facade. Behaviour lives in src.core; this class keeps the public methods."""

from __future__ import annotations

import asyncio
import logging

from src.core import chat_flow, event_flow, lifecycle, queue, twitch_link, voice_flow
from src.core.event_flow import EVENT_PRIORITY
from src.core.twitch_link import TOKEN_REVALIDATE_SECONDS
from src.settings import ROOT, update_config
from src.twitch.helix import create_clip, create_marker, shoutout, user_id

log = logging.getLogger("cohost.orch")

__all__ = [
    "EVENT_PRIORITY",
    "Orchestrator",
    "ROOT",
    "TOKEN_REVALIDATE_SECONDS",
    "create_clip",
    "create_marker",
    "shoutout",
    "update_config",
    "user_id",
]


class Orchestrator:
    def __init__(
        self, settings, hub, data_dir=None, *, llm=None, tts=None, stt=None, mic=None, persona_path=None
    ):
        return lifecycle.bind(
            self, settings, hub, data_dir, llm=llm, tts=tts, stt=stt, mic=mic, persona_path=persona_path
        )

    def _refresh_prompts(self):
        return lifecycle._refresh_prompts(self)

    def _cohost_name(self):
        return lifecycle._cohost_name(self)

    def _tts_speaking(self):
        return lifecycle._tts_speaking(self)

    def _barge_in(self):
        return lifecycle._barge_in(self)

    def _spawn(self, coro, name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._bg.add(task)
        task.add_done_callback(self._on_bg_done)
        return task

    def _on_bg_done(self, task: asyncio.Task) -> None:
        self._bg.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        log.error("background task %s failed", task.get_name(), exc_info=exc)

    def _phrases(self):
        return lifecycle._phrases(self)

    def _banned_extra(self):
        return lifecycle._banned_extra(self)

    def _persist_booth(self):
        return lifecycle._persist_booth(self)

    def _install_profile(self, name, *, persist):
        return lifecycle._install_profile(self, name, persist=persist)

    async def apply_profile(self, name):
        return await lifecycle.apply_profile(self, name)

    async def start(self, loop):
        return await lifecycle.start(self, loop)

    async def _load_models(self):
        return await lifecycle._load_models(self)

    async def _models_ready(self):
        return await lifecycle._models_ready(self)

    async def shutdown(self):
        return await lifecycle.shutdown(self)

    def set_identity(self, display_name, twitch_login):
        return lifecycle.set_identity(self, display_name, twitch_login)

    def forget_viewer(self, login):
        return lifecycle.forget_viewer(self, login)

    def clear_memory(self):
        return lifecycle.clear_memory(self)

    def reload_persona(self):
        return lifecycle.reload_persona(self)

    def save_persona_text(self, text):
        return lifecycle.save_persona_text(self, text)

    async def _after_persona_reload(self):
        return await lifecycle._after_persona_reload(self)

    async def set_mood(self, mood):
        return await lifecycle.set_mood(self, mood)

    async def set_listen(self, enabled):
        return await lifecycle.set_listen(self, enabled)

    async def set_ptt(self, down):
        return await lifecycle.set_ptt(self, down)

    async def set_mute(self, mute):
        return await lifecycle.set_mute(self, mute)

    def set_post_voice(self, enabled):
        return lifecycle.set_post_voice(self, enabled)

    def _wake_status(self):
        return lifecycle._wake_status(self)

    async def set_vad(self, silence_ms, energy, max_seconds):
        return await lifecycle.set_vad(self, silence_ms, energy, max_seconds)

    async def set_devices(self, mic_device, speaker_device, mic_name=None, speaker_name=None):
        return await lifecycle.set_devices(self, mic_device, speaker_device, mic_name, speaker_name)

    async def set_language(self, language):
        return await lifecycle.set_language(self, language)

    async def set_voice(self, voice):
        return await lifecycle.set_voice(self, voice)

    def set_duck(self, enabled):
        return lifecycle.set_duck(self, enabled)

    def _bump_voice(self):
        return voice_flow._bump_voice(self)

    async def interrupt(self):
        return await voice_flow.interrupt(self)

    async def twitch_connect(self):
        return await twitch_link.twitch_connect(self)

    async def connect_irc(self):
        return await twitch_link.connect_irc(self)

    async def connect_events(self):
        return await twitch_link.connect_events(self)

    async def _irc_connect(self):
        return await twitch_link._irc_connect(self)

    async def _eventsub_connect(self):
        return await twitch_link._eventsub_connect(self)

    async def _eventsub_connect_locked(self):
        return await twitch_link._eventsub_connect_locked(self)

    async def twitch_disconnect(self):
        return await twitch_link.twitch_disconnect(self)

    async def disconnect_irc(self):
        return await twitch_link.disconnect_irc(self)

    async def disconnect_events(self):
        return await twitch_link.disconnect_events(self)

    async def _twitch_watchdog(self):
        return await twitch_link._twitch_watchdog(self)

    async def _validate_irc_token(self):
        return await twitch_link._validate_irc_token(self)

    async def _revalidate_event_token(self):
        return await twitch_link._revalidate_event_token(self)

    async def _twitch_status(self, state):
        return await twitch_link._twitch_status(self, state)

    async def _eventsub_status(self, state):
        return await twitch_link._eventsub_status(self, state)

    async def on_stream_event(self, event):
        return await event_flow.on_stream_event(self, event)

    async def _flush_follows(self):
        return await event_flow._flush_follows(self)

    async def _emit_follows(self):
        return await event_flow._emit_follows(self)

    async def _flush_cheers(self):
        return await event_flow._flush_cheers(self)

    async def _emit_cheers(self):
        return await event_flow._emit_cheers(self)

    def _kick_events(self):
        return event_flow._kick_events(self)

    def _kick_chat(self):
        return chat_flow._kick_chat(self)

    async def _event_worker(self):
        return await event_flow._event_worker(self)

    async def _drain_events(self):
        return await event_flow._drain_events(self)

    async def _react_event(self, event):
        return await event_flow._react_event(self, event)

    async def _bounded_complete(self, messages, temperature):
        return await event_flow._bounded_complete(self, messages, temperature)

    async def _finish_reply(self, messages, *, temperature, retry=True):
        return await event_flow._finish_reply(self, messages, temperature=temperature, retry=retry)

    def _clean(self, raw):
        return voice_flow._clean(self, raw)

    def _chat_text(self, text, target_user=None):
        return voice_flow._chat_text(self, text, target_user)

    async def _emit_cohost(self, reply, *, thread, speak, post):
        return await voice_flow._emit_cohost(self, reply, thread=thread, speak=speak, post=post)

    async def _speak(self, text):
        return await voice_flow._speak(self, text)

    async def on_chat(self, line):
        return await chat_flow.on_chat(self, line)

    async def on_utterance(self, audio):
        return await voice_flow.on_utterance(self, audio)

    async def pretend(self, text):
        return await voice_flow.pretend(self, text)

    async def _handle_voice_text(self, text, gen, started):
        return await voice_flow._handle_voice_text(self, text, gen, started)

    def _remember_caption(self, text):
        return voice_flow._remember_caption(self, text)

    async def replay_last(self):
        return await voice_flow.replay_last(self)

    async def say_line(self, text, post_chat=True, speak=True):
        return await voice_flow.say_line(self, text, post_chat, speak)

    def _chat_context(self, limit=None):
        return voice_flow._chat_context(self, limit)

    def _history_messages(self, thread_key, limit=None):
        return voice_flow._history_messages(self, thread_key, limit)

    def _reply_is_bad(self, reply, previous, recent=None):
        return voice_flow._reply_is_bad(self, reply, previous, recent)

    async def _voice_reply(self, user_text, gen, started):
        return await voice_flow._voice_reply(self, user_text, gen, started)

    async def _emit_voice_result(self, name, user_text, reply, gen, started, post):
        return await voice_flow._emit_voice_result(self, name, user_text, reply, gen, started, post)

    def _rate_ok(self, *, mention, user=""):
        return chat_flow._rate_ok(self, mention=mention, user=user)

    def _mark_reply(self, user=""):
        return chat_flow._mark_reply(self, user)

    async def _chat_scan_loop(self):
        return await chat_flow._chat_scan_loop(self)

    async def _chat_worker(self):
        return await chat_flow._chat_worker(self)

    async def _expire_stale_chat(self):
        return await chat_flow._expire_stale_chat(self)

    async def _drain_chat(self):
        return await chat_flow._drain_chat(self)

    def _pick_chat_target(self):
        return chat_flow._pick_chat_target(self)

    def _drop_chat_target(self, target):
        return chat_flow._drop_chat_target(self, target)

    def _control_ready(self):
        return chat_flow._control_ready(self)

    def _clip_ready(self):
        return chat_flow._clip_ready(self)

    async def _type_ack(self, text):
        return await chat_flow._type_ack(self, text)

    async def _run_pixel_control(self, verb):
        return await chat_flow._run_pixel_control(self, verb)

    async def _consider_chat_locked(self):
        return await chat_flow._consider_chat_locked(self)

    async def _perform_chat(self, target, *, ignore_rate):
        return await chat_flow._perform_chat(self, target, ignore_rate=ignore_rate)

    async def _decide_chat(self, line, action):
        return await chat_flow._decide_chat(self, line, action)

    async def _persona_chat_reply(self, line, ambient=False, retry=False):
        return await chat_flow._persona_chat_reply(self, line, ambient, retry)

    def _control_names(self):
        return chat_flow._control_names(self)

    def _irc_commands(self):
        return chat_flow._irc_commands(self)

    def _can_run_mod_command(self, line):
        return chat_flow._can_run_mod_command(self, line)

    def _shoutout_ready(self):
        return twitch_link._shoutout_ready(self)

    async def shoutout(self, login):
        return await twitch_link.shoutout(self, login)

    async def mark_moment(self, note):
        return await twitch_link.mark_moment(self, note)

    async def create_clip(self):
        return await twitch_link.create_clip(self)

    async def edit_note(self, login, text, op="add", pinned=None):
        return await lifecycle.edit_note(self, login, text, op, pinned)

    async def _note_skip(self, reason, user):
        return await lifecycle._note_skip(self, reason, user)

    def _next_qid(self):
        return queue._next_qid(self)

    def _assign_queue_ids(self):
        return queue._assign_queue_ids(self)

    def _retain_chat(self, kept):
        return queue._retain_chat(self, kept)

    def _retain_events(self, kept):
        return queue._retain_events(self, kept)

    def _retain_questions(self, kept):
        return queue._retain_questions(self, kept)

    def queue_snapshot(self):
        return queue.queue_snapshot(self)

    async def drop_queued(self, item_id):
        return await queue.drop_queued(self, item_id)

    async def answer_queued(self, item_id):
        return await queue.answer_queued(self, item_id)

    async def make_recap(self, *, vibe=False):
        return await event_flow.make_recap(self, vibe=vibe)

    async def _publish_queue(self):
        return await queue._publish_queue(self)

    async def _memory_prune_loop(self):
        return await lifecycle._memory_prune_loop(self)

    async def _memory_flush_loop(self):
        return await lifecycle._memory_flush_loop(self)

    async def _resolve_audio_devices(self):
        return await lifecycle._resolve_audio_devices(self)

    async def _ollama_watch(self):
        return await lifecycle._ollama_watch(self)

    def restore_log_level(self):
        return lifecycle.restore_log_level(self)

    async def enable_debug_log(self, seconds=600):
        return await lifecycle.enable_debug_log(self, seconds)

    async def _debug_log_window(self, seconds):
        return await lifecycle._debug_log_window(self, seconds)

    async def _level_loop(self):
        return await voice_flow._level_loop(self)
