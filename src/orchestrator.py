from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from pathlib import Path

import numpy as np

from src.audio.duck import Duck
from src.audio.mic import MicListener
from src.audio.stt import SpeechToText
from src.audio.vad import trim_to_speech
from src.booth import BoothState, load_booth, save_booth
from src.hub import Hub
from src.identity import StreamerIdentity, load_identity, save_identity
from src.llm.ollama import OllamaClient
from src.llm.persona import (
    build_chat_prompt,
    build_voice_prompt,
    load_persona,
    mood_addon,
    mood_temperature,
    validate_persona_text,
)
from src.memory import (
    STREAMER_THREAD,
    ConversationMemory,
    apply_catchphrase_budget,
    ensure_chat_mention,
    for_speech,
    is_generic_filler,
    never_say_phrases,
    repeats_recent,
    sanitize_reply,
    streamer_smalltalk_fallback,
)
from src.policy import chat_action, mention_waits
from src.safety import DEFLECT, clean_chat_output, fence_viewer_text, is_blocked
from src.settings import ROOT, WHISPER_LANGUAGES, Settings, atomic_write_text, update_config
from src.tts.piper import PiperTTS
from src.twitch.chat import ChatLine, TwitchChat, looks_like_auth_failure
from src.twitch.eventsub import EventSubClient, StreamEvent, _looks_like_auth
from src.twitch.helix import HelixError, create_marker, shoutout, user_id

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


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        hub: Hub,
        data_dir: Path | None = None,
        *,
        llm: OllamaClient | None = None,
        tts: PiperTTS | None = None,
        stt: SpeechToText | None = None,
        mic: MicListener | None = None,
        persona_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.hub = hub
        # Tests pass fakes so importing this class never has to open a mic, model, or speaker.
        self.llm = OllamaClient(settings) if llm is None else llm
        self.stt = SpeechToText(settings) if stt is None else stt
        self.tts = PiperTTS(settings) if tts is None else tts
        self.mic = MicListener(settings, self.on_utterance) if mic is None else mic
        self.duck = Duck(settings)
        self.data_dir = data_dir or (ROOT / "data")
        self.twitch: TwitchChat | None = None
        self.eventsub: EventSubClient | None = None
        self.persona_path = persona_path or (ROOT / "persona.yaml")
        self.persona = load_persona(self.persona_path)
        self.identity: StreamerIdentity = load_identity(
            self.data_dir / "streamer.json",
            default_login=settings.twitch_channel,
        )
        self.booth = load_booth(
            self.data_dir / "booth.json",
            fallback_mute=settings.tts_mute,
            fallback_post=settings.post_voice_replies_to_chat,
        )
        self.mood = self.booth.mood
        self.settings.post_voice_replies_to_chat = self.booth.post_voice_replies_to_chat
        self._refresh_prompts()
        self.memory = ConversationMemory(
            self.data_dir / "memory.json",
            event_limit=settings.memory_events,
            viewer_limit=settings.memory_viewers,
            thread_turns=settings.memory_thread_turns,
            retention_days=settings.retention_days,
        )
        self.memory.set_streamer_display(self.identity.spoken_name())
        self.spoken: deque[tuple[str, str]] = deque(maxlen=settings.spoken_turns)
        self._voice_gen = 0
        self._voice_cancel = asyncio.Event()
        self._chat_pending: deque[ChatLine] = deque(maxlen=60)
        self._reply_times: deque[float] = deque()
        self._last_chat_reply = 0.0
        self._user_reply_at: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._chat_q: asyncio.Queue[int] = asyncio.Queue()
        self._event_q: asyncio.Queue[int] = asyncio.Queue()
        self._chat_scheduled = False
        self._event_scheduled = False
        self._busy_voice = False
        self._speak_depth = 0
        self._event_pending: deque[StreamEvent] = deque(maxlen=40)
        self._follow_names: list[str] = []
        self._follow_task: asyncio.Task | None = None
        self._cheers: list[StreamEvent] = []
        self._cheer_task: asyncio.Task | None = None
        self.tts_mute = self.booth.tts_mute
        self._want_twitch = False
        self._want_events = False
        self._eventsub_error = ""
        self._eventsub_lock = asyncio.Lock()
        self._stopping = False
        self._irc_fatal = False
        self._eventsub_fatal = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_skip = {"reason": "", "user": ""}
        self._catchphrase_at: dict[str, float] = {}
        self._last_raider = ""
        self._last_raider_id = ""
        self._last_shoutout_at = 0.0
        self.timings: dict[str, int | None] = {
            "stt_ms": None,
            "first_token_ms": None,
            "first_audio_ms": None,
            "chat_send_ms": None,
        }

    def _refresh_prompts(self) -> None:
        streamer = self.identity.spoken_name()
        addon = mood_addon(self.mood)
        extra = f"\n{addon}\n" if addon else ""
        self.voice_prompt = build_voice_prompt(self.persona, streamer) + extra
        self.chat_prompt = build_chat_prompt(self.persona, streamer) + extra
        self.system_prompt = self.voice_prompt

    def _phrases(self) -> list[str]:
        raw = self.persona.get("catchphrases") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(p).strip() for p in raw if str(p).strip()]

    def _banned_extra(self) -> list[str]:
        return never_say_phrases(self.persona)

    def _persist_booth(self) -> None:
        self.booth = BoothState(
            listen=self.mic.listen_enabled,
            tts_mute=self.tts_mute,
            post_voice_replies_to_chat=self.settings.post_voice_replies_to_chat,
            mood=self.mood,
        )
        save_booth(self.data_dir / "booth.json", self.booth)

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._stopping = False
        await self.hub.patch_status(
            phase="starting",
            tts_mute=self.tts_mute,
            persona=str(self.persona.get("name") or "Cohost"),
            streamer_name=self.identity.spoken_name(),
            streamer_login=self.identity.twitch_login,
            eventsub="disconnected",
            mood=self.mood,
            duck=self.duck.mode,
        )
        self._tasks = [
            loop.create_task(self._load_models(), name="load-models"),
            loop.create_task(self._chat_worker(), name="chat-worker"),
            loop.create_task(self._event_worker(), name="event-worker"),
            loop.create_task(self._chat_scan_loop(), name="chat-scan"),
            loop.create_task(self._twitch_watchdog(), name="twitch-watch"),
            loop.create_task(self._memory_flush_loop(), name="memory-flush"),
            loop.create_task(self._memory_prune_loop(), name="memory-prune"),
            loop.create_task(self._level_loop(), name="mic-level"),
        ]
        try:
            self.mic.start(loop)
        except Exception as e:
            log.exception("Microphone failed")
            await self.hub.broadcast({"type": "log", "data": f"Microphone failed: {e}"})
        health = await self.llm.health()
        await self.hub.patch_status(
            phase="idle",
            ollama="ok" if health.get("ok") else "down",
        )

    async def _load_models(self) -> None:
        try:
            await asyncio.to_thread(self.stt.load)
            await self.hub.broadcast({"type": "log", "data": f"Whisper ready on {self.stt.device}"})
        except Exception as e:
            log.exception("Whisper failed to load")
            await self.hub.broadcast({"type": "log", "data": f"Whisper load failed: {e}"})
        try:
            await asyncio.to_thread(self.tts.ensure_files)
            msg = "Piper TTS ready" if self.tts.ready else f"Piper: {self.tts.error}"
            await self.hub.broadcast({"type": "log", "data": msg})
        except Exception as e:
            log.exception("Piper setup failed")
            await self.hub.broadcast({"type": "log", "data": f"Piper setup failed: {e}"})
        ready, why = await self._models_ready()
        await self.hub.patch_status(stt_ready=self.stt.model is not None, tts_ready=self.tts.ready or not self.settings.tts_enabled)
        if self.booth.listen:
            if ready:
                await self.set_listen(True)
            else:
                await self.hub.broadcast({"type": "log", "data": f"Not listening yet: {why}"})

    async def _models_ready(self) -> tuple[bool, str]:
        if self.stt.model is None:
            return False, "Whisper is not loaded"
        if self.settings.tts_enabled and not self.tts.ready:
            return False, self.tts.error or "Piper is not ready"
        health = await self.llm.health()
        if not health.get("ok"):
            return False, "Ollama is down"
        if not health.get("has_model"):
            return False, f"Pull the model: ollama pull {self.settings.ollama_model}"
        return True, ""

    async def shutdown(self) -> None:
        self._stopping = True
        for task in (self._follow_task, self._cheer_task):
            if task and not task.done():
                task.cancel()
        await self._emit_follows()
        await self._emit_cheers()
        for event in list(self._event_pending):
            self.memory.remember(
                "twitch_event",
                event.user or "chat",
                event.scene(),
                via="event",
                thread=STREAMER_THREAD,
            )
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        await self.mic.stop()
        if self.eventsub:
            await self.eventsub.disconnect()
        if self.twitch:
            await self.twitch.disconnect()
        self.tts.interrupt()
        self.tts.close()
        self.duck.up()
        await asyncio.to_thread(self.memory.flush)
        await self.llm.aclose()

    def set_identity(self, display_name: str, twitch_login: str) -> StreamerIdentity:
        self.identity.display_name = (display_name or "").strip()
        self.identity.twitch_login = (twitch_login or "").strip().lstrip("#")
        if not self.identity.twitch_login:
            self.identity.twitch_login = (self.settings.twitch_channel or "").strip().lstrip("#")
        save_identity(self.data_dir / "streamer.json", self.identity)
        self.memory.set_streamer_display(self.identity.spoken_name())
        self._refresh_prompts()
        if self.twitch:
            self.twitch.set_allow_from([self.identity.login_key()] if self.identity.login_key() else [])
        asyncio.create_task(
            self.hub.patch_status(
                streamer_name=self.identity.spoken_name(),
                streamer_login=self.identity.twitch_login,
            )
        )
        asyncio.create_task(
            self.hub.broadcast(
                {"type": "log", "data": f"Streamer is {self.identity.spoken_name()} (@{self.identity.twitch_login or '?'})"}
            )
        )
        return self.identity

    def forget_viewer(self, login: str) -> None:
        self.memory.forget(login)
        self.memory.flush()
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))

    def clear_memory(self) -> None:
        self.memory.clear()
        self.spoken.clear()
        self._user_reply_at.clear()
        asyncio.create_task(self.hub.broadcast({"type": "log", "data": "Memory cleared"}))
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))

    def reload_persona(self) -> None:
        self.persona = load_persona(self.persona_path)
        self._refresh_prompts()
        try:
            asyncio.get_running_loop().create_task(self._after_persona_reload())
        except RuntimeError:
            return

    def save_persona_text(self, text: str) -> str:
        data = validate_persona_text(text)
        body = text if text.endswith("\n") else text + "\n"
        atomic_write_text(self.persona_path, body, backup=True)
        self.reload_persona()
        return str(data.get("name"))

    async def _after_persona_reload(self) -> None:
        name = str(self.persona.get("name") or "Cohost")
        await self.hub.patch_status(persona=name)
        await self.hub.broadcast({"type": "log", "data": f"Persona reloaded: {name}"})

    async def set_mood(self, mood: str) -> str:
        mood = (mood or "normal").lower()
        if mood not in ("chill", "normal", "hype"):
            raise ValueError("mood must be chill, normal, or hype")
        self.mood = mood
        self._refresh_prompts()
        self._persist_booth()
        await self.hub.patch_status(mood=mood)
        return mood

    async def set_listen(self, enabled: bool) -> bool:
        if enabled:
            ready, why = await self._models_ready()
            if not ready:
                await self.hub.broadcast({"type": "log", "data": f"Start listening refused: {why}"})
                await self.hub.patch_status(
                    listen=False,
                    stt_ready=self.stt.model is not None,
                    tts_ready=self.tts.ready or not self.settings.tts_enabled,
                    phase="idle",
                )
                return False
        self.mic.set_listen(enabled)
        phase = "listening" if enabled else ("speaking" if self.tts.speaking else "idle")
        self._persist_booth()
        await self.hub.patch_status(
            listen=enabled,
            phase=phase,
            stt_ready=self.stt.model is not None,
            tts_ready=self.tts.ready or not self.settings.tts_enabled,
        )
        return True

    async def set_ptt(self, down: bool) -> None:
        if down:
            ready, why = await self._models_ready()
            if not ready:
                await self.hub.broadcast({"type": "log", "data": f"Hold to talk refused: {why}"})
                return
        self.mic.set_ptt(down)
        await self.hub.patch_status(ptt=down, listen=self.mic.listen_enabled or down)

    async def set_mute(self, mute: bool) -> None:
        self.tts_mute = mute
        self.settings.tts_mute = mute
        if mute:
            self._bump_voice()
        self._persist_booth()
        await self.hub.patch_status(tts_mute=mute)

    def set_post_voice(self, enabled: bool) -> None:
        self.settings.post_voice_replies_to_chat = enabled
        self._persist_booth()

    async def set_vad(self, silence_ms: int, energy: float, max_seconds: float) -> None:
        self.settings.silence_ms = int(silence_ms)
        self.settings.energy_threshold = float(energy)
        self.settings.max_utterance_seconds = float(max_seconds)
        await asyncio.to_thread(
            update_config,
            {
                "audio.silence_ms": int(silence_ms),
                "audio.energy_threshold": float(energy),
                "audio.max_utterance_seconds": float(max_seconds),
            },
        )

    async def set_devices(self, mic_device: int | None, speaker_device: int | None) -> None:
        self.settings.mic_device = mic_device
        self.settings.speaker_device = speaker_device
        await asyncio.to_thread(
            update_config,
            {"audio.mic_device": mic_device, "audio.speaker_device": speaker_device},
        )
        if self._loop is None:
            return
        listen = self.mic.listen_enabled
        ptt = self.mic.ptt_down
        await self.mic.stop()
        self.mic.listen_enabled = listen
        self.mic.ptt_down = ptt
        self.mic.start(self._loop)

    async def set_language(self, language: str) -> None:
        language = (language or "").strip().lower()
        if language not in WHISPER_LANGUAGES:
            raise ValueError(f"unknown language: {language}")
        self.settings.whisper_language = language
        await asyncio.to_thread(update_config, {"whisper.language": language})
        await asyncio.to_thread(self.stt.load)
        await self.hub.patch_status(stt_ready=self.stt.model is not None)

    async def set_voice(self, voice: str) -> None:
        voice = (voice or "").strip()
        if voice not in self.settings.tts_voices:
            raise ValueError(f"unknown voice: {voice}")
        self.tts.close()
        self.settings.tts_voice = voice

        def progress(message: str) -> None:
            if self._loop is None:
                return
            asyncio.run_coroutine_threadsafe(self.hub.broadcast({"type": "log", "data": message}), self._loop)

        self.tts.on_progress = progress
        await asyncio.to_thread(self.tts.ensure_files)
        await asyncio.to_thread(update_config, {"tts.voice": voice})
        await self.hub.patch_status(tts_ready=self.tts.ready)

    def set_duck(self, enabled: bool) -> None:
        self.duck.set_enabled(enabled)
        update_config({"audio.duck_enabled": enabled})

    def _bump_voice(self) -> int:
        self._voice_gen += 1
        self._voice_cancel.set()
        self._voice_cancel = asyncio.Event()
        self.tts.interrupt()
        return self._voice_gen

    async def interrupt(self) -> None:
        self._bump_voice()
        self.duck.up()
        await self.hub.patch_status(
            speaking=False,
            caption="",
            phase="listening" if self.mic.listen_enabled else "idle",
        )

    async def twitch_connect(self) -> None:
        await self.connect_irc()
        await self.connect_events()

    async def connect_irc(self) -> None:
        self.settings.reload_secrets()
        self._want_twitch = True
        self._irc_fatal = False
        await self._irc_connect()

    async def connect_events(self) -> None:
        self.settings.reload_secrets()
        self._want_events = True
        self._eventsub_fatal = False
        self._eventsub_error = ""
        await self._eventsub_connect()
        if self._eventsub_error:
            raise RuntimeError(self._eventsub_error)

    async def _irc_connect(self) -> None:
        if self.twitch:
            await self.twitch.disconnect()
        bot_name = self.settings.twitch_nick
        persona_name = str(self.persona.get("name") or "")
        self.twitch = TwitchChat(
            token=self.settings.twitch_token,
            nick=bot_name,
            channel=self.settings.twitch_channel,
            on_line=self.on_chat,
            on_status=self._twitch_status,
            buffer_size=self.settings.chat_buffer_size,
            bot_names=[bot_name, persona_name],
            allow_from=[self.identity.login_key()] if self.identity.login_key() else [],
            commands=self.settings.chat_commands,
            min_send_gap=self.settings.min_send_gap_seconds,
        )
        try:
            await self.twitch.connect()
        except Exception as e:
            if looks_like_auth_failure(str(e)) or "token rejected" in str(e).lower():
                self._irc_fatal = True
                await self.hub.patch_status(twitch="token rejected", twitch_error=str(e))
            raise

    async def _eventsub_connect(self) -> None:
        async with self._eventsub_lock:
            await self._eventsub_connect_locked()

    async def _eventsub_connect_locked(self) -> None:
        if not self.settings.events_enabled:
            await self.hub.patch_status(eventsub="off")
            return
        if self.eventsub and self.eventsub.alive():
            return
        if self.eventsub:
            await self.eventsub.disconnect()
            self.eventsub = None
        try:
            self.eventsub = EventSubClient(
                token=self.settings.event_token(),
                client_id=self.settings.twitch_client_id,
                channel=self.settings.twitch_channel,
                on_event=self.on_stream_event,
                on_status=self._eventsub_status,
                subscriptions=self.settings.event_subscriptions(),
            )
            await self.eventsub.connect()
            if self.eventsub.fatal:
                self._eventsub_fatal = True
        except Exception as e:
            log.warning("EventSub not connected: %s", e)
            self.eventsub = None
            fatal = _looks_like_auth(str(e)) or "is required" in str(e).lower()
            self._eventsub_fatal = fatal
            self._eventsub_error = str(e)
            state = "token rejected" if fatal else "error"
            await self.hub.patch_status(eventsub=state, eventsub_error=str(e))
            await self.hub.broadcast({"type": "log", "data": f"EventSub: {e}"})

    async def twitch_disconnect(self) -> None:
        await self.disconnect_irc()
        await self.disconnect_events()

    async def disconnect_irc(self) -> None:
        self._want_twitch = False
        self._irc_fatal = False
        if self.twitch:
            await self.twitch.disconnect()
            self.twitch = None

    async def disconnect_events(self) -> None:
        self._want_events = False
        self._eventsub_fatal = False
        self._eventsub_error = ""
        if self.eventsub:
            await self.eventsub.disconnect()
            self.eventsub = None

    async def _twitch_watchdog(self) -> None:
        delay = 5.0
        while not self._stopping:
            try:
                await asyncio.sleep(delay)
                if not self.settings.auto_reconnect_twitch or not (self._want_twitch or self._want_events):
                    delay = 5.0
                    continue
                if self._want_twitch and not (self.twitch and self.twitch.alive()):
                    if self._irc_fatal:
                        await self.hub.patch_status(twitch="token rejected")
                    else:
                        await self.hub.patch_status(twitch="reconnecting")
                        await self.hub.broadcast({"type": "log", "data": "Twitch chat dropped — reconnecting"})
                        try:
                            await self._irc_connect()
                            delay = 5.0
                        except Exception as e:
                            if self._irc_fatal:
                                await self.hub.patch_status(twitch="token rejected", twitch_error=str(e))
                            else:
                                delay = min(delay * 2, 120.0)
                                await self.hub.patch_status(twitch="error", twitch_error=str(e))
                                await self.hub.broadcast({"type": "log", "data": f"Twitch reconnect failed: {e}"})
                if (
                    self._want_events
                    and self.settings.events_enabled
                    and not self._eventsub_lock.locked()
                    and not (self.eventsub and self.eventsub.alive())
                ):
                    if self._eventsub_fatal or (self.eventsub and self.eventsub.fatal):
                        self._eventsub_fatal = True
                    else:
                        await self.hub.patch_status(eventsub="reconnecting")
                        await self.hub.broadcast({"type": "log", "data": "EventSub dropped — reconnecting"})
                        await self._eventsub_connect()
                irc_up = (not self._want_twitch) or bool(self.twitch and self.twitch.alive()) or self._irc_fatal
                events_up = (not self._want_events) or (not self.settings.events_enabled) or self._eventsub_fatal or bool(self.eventsub and self.eventsub.alive())
                if irc_up and events_up:
                    delay = 5.0
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("twitch watchdog failed")

    async def _twitch_status(self, state: str) -> None:
        if state == "token rejected":
            self._irc_fatal = True
        await self.hub.patch_status(twitch=state)

    async def _eventsub_status(self, state: str) -> None:
        if state == "token rejected":
            self._eventsub_fatal = True
        await self.hub.patch_status(eventsub=state)

    async def on_stream_event(self, event: StreamEvent) -> None:
        await self.hub.broadcast(
            {
                "type": "alert",
                "data": {"kind": event.kind, "user": event.user, "people": event.people(), "text": event.scene()},
            }
        )
        await self.hub.broadcast({"type": "log", "data": f"Alert {event.kind}: {event.scene()}"})
        if event.kind == "raid":
            self._last_raider = event.user
            self._last_raider_id = str(event.extra.get("from_id") or "")
        if event.kind == "hype_progress":
            await self.hub.patch_status(hype_level=event.extra.get("level"))
            return
        if event.kind == "redeem":
            action = self.settings.reward_action(str(event.extra.get("title") or ""))
            if action == "ignore":
                await self._note_skip("reward ignored", event.user)
                return
            event.extra["delivery"] = action
        if event.kind == "follow":
            if not self.settings.announce_follows:
                return
            self._follow_names.extend(event.people())
            if not self._follow_task or self._follow_task.done():
                self._follow_task = asyncio.create_task(self._flush_follows(), name="follow-batch")
            return
        if event.kind == "cheer":
            self._cheers.append(event)
            if not self._cheer_task or self._cheer_task.done():
                self._cheer_task = asyncio.create_task(self._flush_cheers(), name="cheer-batch")
            return
        self._event_pending.append(event)
        self._kick_events()

    async def _flush_follows(self) -> None:
        try:
            await asyncio.sleep(self.settings.follow_batch_seconds)
        except asyncio.CancelledError:
            return
        await self._emit_follows()

    async def _emit_follows(self) -> None:
        names = [n for n in self._follow_names if n]
        self._follow_names = []
        if not names:
            return
        batched = StreamEvent(kind="follow", user=names[0], users=names[1:])
        self._event_pending.append(batched)
        self._kick_events()

    async def _flush_cheers(self) -> None:
        try:
            await asyncio.sleep(self.settings.follow_batch_seconds)
        except asyncio.CancelledError:
            return
        await self._emit_cheers()

    async def _emit_cheers(self) -> None:
        cheers = list(self._cheers)
        self._cheers = []
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
        self._event_pending.append(batched)
        self._kick_events()

    def _kick_events(self) -> None:
        if self._event_scheduled or self._stopping:
            return
        self._event_scheduled = True
        try:
            self._event_q.put_nowait(1)
        except Exception:
            self._event_scheduled = False

    def _kick_chat(self) -> None:
        if self._chat_scheduled or self._stopping:
            return
        self._chat_scheduled = True
        try:
            self._chat_q.put_nowait(1)
        except Exception:
            self._chat_scheduled = False

    async def _event_worker(self) -> None:
        while not self._stopping:
            await self._event_q.get()
            self._event_scheduled = False
            try:
                await self._drain_events()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event worker failed")

    async def _drain_events(self) -> None:
        while self._event_pending and not self._stopping:
            if self._busy_voice:
                await self._note_skip("busy on mic", "")
                await asyncio.sleep(0.25)
                self._kick_events()
                return
            event = min(self._event_pending, key=lambda e: EVENT_PRIORITY.get(e.kind, 9))
            try:
                self._event_pending.remove(event)
            except ValueError:
                break
            await self._publish_queue()
            await self._react_event(event)
        if self._chat_pending and not self._busy_voice:
            self._kick_chat()

    async def _react_event(self, event: StreamEvent) -> None:
        if self._busy_voice:
            self._event_pending.appendleft(event)
            self._kick_events()
            return
        people = event.people()
        primary = people[0] if people else (event.user or "chat")
        thread = (
            self.memory.viewer_thread_key(primary)
            if primary and primary.lower() not in ("anonymous", "chat")
            else STREAMER_THREAD
        )
        reward_input = str(event.extra.get("input") or "")
        if event.kind == "redeem" and is_blocked(reward_input):
            self.memory.remember("twitch_event", primary, "[skipped topic]", via="event", thread=thread)
            asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
            await self._emit_cohost(DEFLECT, thread=thread, speak=False, post=self.settings.events_post_to_chat)
            return
        scene = event.scene()
        self.memory.remember("twitch_event", primary, scene, via="event", thread=thread)
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
        delivery = str(event.extra.get("delivery") or ("speak" if self.settings.events_speak else "chat"))
        if delivery == "ignore":
            return
        if is_blocked(scene):
            reply = DEFLECT
        else:
            temp = mood_temperature("hype" if event.kind == "raid" and self.mood == "normal" else self.mood)
            reply = await self._finish_reply(
                [
                    {"role": "system", "content": self.voice_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"You're on the couch next to {self.identity.spoken_name()}. This just happened on stream:\n{scene}\n\n"
                            "Hype it like a cohost for a second, then drop it. No corporate thank-you. "
                            "Words to speak aloud only."
                        ),
                    },
                ],
                temperature=temp,
            )
        if not reply:
            await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle")
            return
        await self._emit_cohost(reply, thread=thread, speak=delivery == "speak" and self.settings.events_speak, post=self.settings.events_post_to_chat)

    async def _finish_reply(self, messages: list[dict[str, str]], *, temperature: float, retry: bool = True) -> str:
        raw = await self.llm.complete(messages, temperature=temperature)
        reply = self._clean(raw)
        if retry and (not reply or is_generic_filler(reply, self._banned_extra())):
            raw = await self.llm.complete(
                messages + [{"role": "user", "content": "shorter, messier, no greeting, no thank-you template."}],
                temperature=min(1.0, temperature + 0.05),
            )
            reply = self._clean(raw)
        if not reply or is_generic_filler(reply, self._banned_extra()):
            return ""
        if is_blocked(reply):
            return DEFLECT
        return reply

    def _clean(self, raw: str) -> str:
        reply = sanitize_reply(raw, str(self.persona.get("name") or ""))
        reply = apply_catchphrase_budget(
            reply,
            self._phrases(),
            self._catchphrase_at,
            window=self.settings.catchphrase_seconds,
        )
        return reply

    def _chat_text(self, text: str, target_user: str | None = None) -> str:
        return clean_chat_output(
            text,
            target_user,
            allow_links=self.settings.allow_links,
            max_chars=self.settings.chat_max_chars,
        )

    async def _emit_cohost(self, reply: str, *, thread: str, speak: bool, post: bool) -> None:
        spoken = for_speech(reply)
        chat_text = self._chat_text(reply)
        self.memory.remember(
            "cohost_voice" if speak else "cohost_chat",
            str(self.persona.get("name") or "Pixel"),
            spoken if speak else chat_text,
            via="voice" if speak else "chat",
            thread=thread,
        )
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
        await self.hub.broadcast({"type": "transcript", "data": {"role": "cohost", "text": spoken}})
        if post and self.twitch and self.twitch.connected:
            if not chat_text:
                await self._note_skip("output guard", "")
            else:
                try:
                    t0 = time.perf_counter()
                    await self.twitch.send(chat_text)
                    self.timings["chat_send_ms"] = int((time.perf_counter() - t0) * 1000)
                    await self.hub.broadcast(
                        {"type": "chat", "data": {"user": self.persona.get("name"), "text": chat_text, "bot": True}}
                    )
                except Exception:
                    log.exception("event chat send failed")
        if speak and not self.tts_mute and not self._busy_voice:
            await self.hub.patch_status(phase="speaking", speaking=True, caption=spoken, timings=dict(self.timings))
            await self._speak(spoken)
        await self.hub.patch_status(
            phase="listening" if self.mic.listen_enabled else "idle",
            speaking=False,
            caption=spoken[-280:],
            timings=dict(self.timings),
        )

    async def _speak(self, text: str) -> None:
        if not text or self.tts_mute:
            return
        self._speak_depth += 1
        if self._speak_depth == 1:
            self.duck.on_speaking(True)
            await self.hub.patch_status(duck=self.duck.mode, speaking=True)
        try:
            await self.tts.speak(text)
        finally:
            self._speak_depth = max(0, self._speak_depth - 1)
            if self._speak_depth == 0:
                self.duck.on_speaking(False)

    async def on_chat(self, line: ChatLine) -> None:
        if self.identity.is_login(line.user):
            name = self.identity.spoken_name()
            stored = "[skipped topic]" if is_blocked(line.text) else line.text
            self.memory.remember("streamer_chat", name, stored, via="chat", thread=STREAMER_THREAD)
            await self.hub.broadcast(
                {
                    "type": "chat",
                    "data": {"user": name, "text": line.text, "mention": line.mention, "streamer": True},
                }
            )
            asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
            self._chat_pending.append(line)
            self._kick_chat()
            return
        self._chat_pending.append(line)
        stored = "[skipped topic]" if is_blocked(line.text) else line.text
        self.memory.remember("viewer", line.user, stored, via="chat", thread=self.memory.viewer_thread_key(line.user))
        await self.hub.broadcast({"type": "chat", "data": {"user": line.user, "text": line.text, "mention": line.mention}})
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
        await self._publish_queue()
        self._kick_chat()

    async def on_utterance(self, audio: np.ndarray) -> None:
        gen = self._bump_voice()
        self._busy_voice = True
        started = time.perf_counter()
        try:
            if self.stt.model is None:
                await self.hub.broadcast({"type": "log", "data": "Whisper is still loading or failed to load."})
                return
            sr = self.settings.sample_rate
            clipped = trim_to_speech(audio, sr)
            await self.hub.patch_status(phase="thinking", speaking=False)
            try:
                text = await asyncio.to_thread(self.stt.transcribe, clipped)
            except Exception as e:
                log.exception("transcribe failed")
                await self.hub.broadcast({"type": "log", "data": f"STT error: {e}"})
                await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle")
                return
            self.timings["stt_ms"] = int((time.perf_counter() - started) * 1000)
            if gen != self._voice_gen:
                return
            if not text:
                await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle", timings=dict(self.timings))
                return
            await self._handle_voice_text(text, gen, started)
        finally:
            self._busy_voice = False
            if self._event_pending:
                self._kick_events()
            elif self._chat_pending:
                self._kick_chat()

    async def pretend(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        gen = self._bump_voice()
        self._busy_voice = True
        try:
            await self._handle_voice_text(text, gen, time.perf_counter())
        finally:
            self._busy_voice = False
            if self._event_pending:
                self._kick_events()
            elif self._chat_pending:
                self._kick_chat()

    async def _handle_voice_text(self, text: str, gen: int, started: float) -> None:
        await self.hub.patch_status(last_user=text, streamer_name=self.identity.spoken_name(), timings=dict(self.timings))
        await self.hub.broadcast(
            {"type": "transcript", "data": {"role": "streamer", "text": text, "name": self.identity.spoken_name()}}
        )
        stored = "[skipped topic]" if is_blocked(text) else text
        self.memory.remember("streamer_voice", self.identity.spoken_name(), stored, via="voice", thread=STREAMER_THREAD)
        await self._voice_reply(text, gen, started)

    async def say_line(self, text: str, post_chat: bool = True) -> None:
        reply = sanitize_reply(text, str(self.persona.get("name") or ""))
        chat_text = self._chat_text(reply)
        if not chat_text:
            await self._note_skip("output guard", "")
            return
        spoken = for_speech(chat_text)
        if post_chat and self.twitch and self.twitch.connected:
            await self.twitch.send(chat_text)
            await self.hub.broadcast(
                {"type": "chat", "data": {"user": self.persona.get("name"), "text": chat_text, "bot": True}}
            )
        await self.hub.broadcast({"type": "transcript", "data": {"role": "cohost", "text": spoken}})
        if not self.tts_mute:
            await self.hub.patch_status(phase="speaking", speaking=True, caption=spoken)
            await self._speak(spoken)
        await self.hub.patch_status(
            phase="listening" if self.mic.listen_enabled else "idle",
            speaking=False,
            caption=spoken[-280:],
        )

    def _chat_context(self, limit: int | None = None) -> str:
        n = self.settings.chat_context_lines if limit is None else limit
        if not self.twitch:
            return "(no chat connected)"
        lines = self.twitch.recent(n)
        rendered = []
        for line in lines:
            if is_blocked(line.text):
                continue
            rendered.append(f"{line.user}: {fence_viewer_text(line.text)}")
        return "\n".join(rendered) if rendered else "(chat is quiet)"

    def _history_messages(self, thread_key: str, limit: int | None = None) -> list[dict[str, str]]:
        banned = self._banned_extra()
        kept = []
        for msg in self.memory.llm_messages(thread_key, limit=limit):
            if msg["role"] == "assistant" and is_generic_filler(msg["content"], banned):
                continue
            kept.append(msg)
        return kept

    def _reply_is_bad(self, reply: str, previous: str, recent: list[str] | None = None) -> bool:
        if not reply or is_generic_filler(reply, self._banned_extra()):
            return True
        earlier = [previous, *(recent or [])]
        return repeats_recent(reply, earlier)

    async def _voice_reply(self, user_text: str, gen: int, started: float) -> None:
        cancel = self._voice_cancel
        name = self.identity.spoken_name()
        if is_blocked(user_text):
            reply = DEFLECT
            if gen != self._voice_gen:
                return
            self.spoken.append(("user", f"{name} (mic): [skipped topic]"))
            self.spoken.append(("assistant", reply))
            if not self.tts_mute:
                await self.hub.patch_status(phase="speaking", speaking=True, caption=reply)
                await self._speak(reply)
            await self._emit_voice_result(name, user_text, reply, gen, started, post=False)
            return
        messages = [
            {"role": "system", "content": self.voice_prompt},
            *self._history_messages(STREAMER_THREAD),
            {
                "role": "user",
                "content": (
                    f"You're on the couch next to {name}. They just said into the mic:\n{fence_viewer_text(user_text)}\n\n"
                    f"{self.memory.prompt_block(STREAMER_THREAD, streamer_name=name, is_voice=True)}\n\n"
                    f"People talking in chat (not {name}):\n{self._chat_context()}\n\n"
                    f"React to {name} like you're sitting next to them. Don't recap. Don't @ anyone."
                ),
            },
        ]
        full: list[str] = []
        first_token = False
        first_audio = False
        async for sentence in self.llm.stream_sentences(
            messages,
            temperature=mood_temperature(self.mood),
            cancel=cancel,
            max_chars=self.settings.max_sentence_chars,
        ):
            if gen != self._voice_gen or cancel.is_set():
                return
            if not first_token:
                first_token = True
                self.timings["first_token_ms"] = int((time.perf_counter() - started) * 1000)
            sentence = self._clean(sentence.strip())
            if not sentence or is_generic_filler(sentence, self._banned_extra()):
                continue
            if is_blocked(sentence):
                sentence = DEFLECT
            full.append(sentence)
            await self.hub.patch_status(phase="speaking", speaking=True, caption=" ".join(full)[-280:], timings=dict(self.timings))
            if not self.tts_mute:
                await self._speak(sentence)
                if not first_audio:
                    first_audio = True
                    self.timings["first_audio_ms"] = int((time.perf_counter() - started) * 1000)
            if gen != self._voice_gen:
                return
        reply = self._clean(" ".join(full).strip())
        if not reply:
            err = self.llm.last_error or "empty model reply"
            await self.hub.broadcast({"type": "log", "data": f"LLM: {err}"})
            await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle", speaking=False)
            return
        if gen != self._voice_gen:
            return
        self.spoken.append(("user", f"{name} (mic): {user_text}"))
        self.spoken.append(("assistant", reply))
        await self._emit_voice_result(name, user_text, reply, gen, started, post=True)

    async def _emit_voice_result(self, name: str, user_text: str, reply: str, gen: int, started: float, post: bool) -> None:
        if gen != self._voice_gen:
            return
        self.memory.remember(
            "cohost_voice",
            str(self.persona.get("name") or "Pixel"),
            reply,
            via="voice",
            thread=STREAMER_THREAD,
        )
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
        await self.hub.broadcast({"type": "transcript", "data": {"role": "cohost", "text": reply}})
        if post and self.settings.post_voice_replies_to_chat and self.twitch and self.twitch.connected:
            if gen != self._voice_gen:
                return
            short = self._chat_text(reply)
            if not short:
                await self._note_skip("output guard", "")
            else:
                try:
                    t0 = time.perf_counter()
                    await self.twitch.send(short)
                    self.timings["chat_send_ms"] = int((time.perf_counter() - t0) * 1000)
                    self.memory.remember(
                        "cohost_chat",
                        str(self.persona.get("name") or "Pixel"),
                        short,
                        via="chat",
                        thread=STREAMER_THREAD,
                    )
                    await self.hub.broadcast(
                        {"type": "chat", "data": {"user": self.persona.get("name"), "text": short, "bot": True}}
                    )
                except Exception:
                    log.exception("failed to echo voice reply to chat")
        await self.hub.patch_status(
            phase="listening" if self.mic.listen_enabled else "idle",
            speaking=False,
            caption=reply[-280:],
            timings=dict(self.timings),
        )

    def _rate_ok(self, *, mention: bool, user: str = "") -> tuple[bool, str]:
        now = time.monotonic()
        while self._reply_times and now - self._reply_times[0] > 60:
            self._reply_times.popleft()
        if len(self._reply_times) >= self.settings.max_replies_per_minute:
            return False, "cooldown"
        if self.identity.is_login(user):
            if now - self._last_chat_reply < 1.5:
                return False, "cooldown"
            return True, ""
        global_wait, user_wait = mention_waits(
            mention,
            self.settings.mention_boost,
            self.settings.cooldown_seconds,
            self.settings.per_user_cooldown_seconds,
        )
        key = (user or "").lower()
        if key:
            last = self._user_reply_at.get(key, 0.0)
            if now - last < user_wait:
                return False, "per-user cooldown"
        if now - self._last_chat_reply < global_wait:
            return False, "cooldown"
        return True, ""

    def _mark_reply(self, user: str = "") -> None:
        now = time.monotonic()
        self._last_chat_reply = now
        self._reply_times.append(now)
        if user:
            self._user_reply_at[user.lower()] = now

    async def _chat_scan_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self.settings.scan_interval_seconds)
                self._kick_chat()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("chat scan failed")

    async def _chat_worker(self) -> None:
        while not self._stopping:
            await self._chat_q.get()
            self._chat_scheduled = False
            try:
                await self._drain_chat()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("chat worker failed")

    async def _drain_chat(self) -> None:
        while self._chat_pending and not self._stopping:
            if self._busy_voice or self._event_pending:
                reason = "busy on mic" if self._busy_voice else "alert queued"
                await self._note_skip(reason, "")
                await asyncio.sleep(0.25)
                self._kick_chat()
                return
            handled = await self._consider_chat_locked()
            if not handled:
                return

    def _pick_chat_target(self) -> ChatLine | None:
        if not self._chat_pending:
            return None
        streamer_lines = [line for line in self._chat_pending if self.identity.is_login(line.user)]
        if streamer_lines:
            return streamer_lines[0]
        commands = [line for line in self._chat_pending if line.command]
        if commands:
            return commands[-1]
        engaging = [line for line in self._chat_pending if line.is_engaging()]
        if engaging:
            return engaging[-1]
        return self._chat_pending[-1]

    async def _consider_chat_locked(self) -> bool:
        if self._busy_voice:
            return False
        if self._event_pending:
            self._kick_events()
            return False
        target = self._pick_chat_target()
        if target is None:
            return False
        action = chat_action(target)
        ok, why = self._rate_ok(mention=action == "speak", user=target.user)
        if not ok:
            await self._note_skip(why, target.user)
            return False
        if self.identity.is_login(target.user):
            kept = [line for line in self._chat_pending if line is not target]
        else:
            kept = [line for line in self._chat_pending if line is not target and line.user.lower() != target.user.lower()]
        self._chat_pending.clear()
        self._chat_pending.extend(kept)
        await self._publish_queue()
        if action == "command" or target.command == "!so" or target.command in self.settings.mod_commands:
            if target.command in self.settings.mod_commands and not self._can_run_mod_command(target):
                await self._note_skip("not a mod", target.user)
                return True
            who = ""
            parts = target.text.split()
            if len(parts) > 1:
                who = parts[1].lstrip("@")
            if not self._shoutout_ready():
                await self._note_skip("shoutout cooldown", target.user)
                return True
            try:
                await self.shoutout(who or None)
            except Exception as e:
                await self.hub.broadcast({"type": "log", "data": f"Shoutout failed: {e}"})
            else:
                self._last_shoutout_at = time.monotonic()
            self._mark_reply(target.user)
            return True
        await self._decide_chat(target, action)
        return True

    async def _decide_chat(self, line: ChatLine, action: str) -> None:
        if self._busy_voice:
            self._chat_pending.append(line)
            return
        await self.hub.patch_status(phase="thinking")
        streamer = self.identity.is_login(line.user)
        previous = ""
        prev = self.memory.viewer(STREAMER_THREAD if streamer else line.user)
        if prev and prev.last_reply:
            previous = prev.last_reply
        recent = [
            msg["content"]
            for msg in self.memory.llm_messages(STREAMER_THREAD if streamer else self.memory.viewer_thread_key(line.user))
            if msg["role"] == "assistant"
        ]
        if is_blocked(line.text):
            reply = DEFLECT
            action = "chat"
        else:
            reply = self._clean(await self._persona_chat_reply(line, ambient=action != "speak"))
            if self._reply_is_bad(reply, previous, recent):
                reply = self._clean(await self._persona_chat_reply(line, ambient=action != "speak", retry=True))
            if self._reply_is_bad(reply, previous, recent):
                fallback = streamer_smalltalk_fallback(line.text) if streamer else ""
                if not fallback:
                    await self._note_skip("filler rejected", line.user)
                    await self.hub.broadcast({"type": "log", "data": f"Chat skip ({line.user}): {line.text[:80]}"})
                    await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle")
                    return
                reply = fallback
            if is_blocked(reply):
                reply = DEFLECT
                action = "chat"
        if streamer:
            chat_text = self._chat_text(sanitize_reply(reply, str(self.persona.get("name") or "")))
        else:
            chat_text = self._chat_text(ensure_chat_mention(reply, line.user), target_user=line.user)
        if not chat_text:
            await self._note_skip("output guard", line.user)
            await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle")
            return
        spoken = for_speech(chat_text)
        thread_key = STREAMER_THREAD if streamer else self.memory.viewer_thread_key(line.user)
        self._mark_reply(line.user)
        self.memory.remember(
            "cohost_chat",
            str(self.persona.get("name") or "Pixel"),
            chat_text,
            via="chat",
            thread=thread_key,
        )
        self.memory.note_reply_to(line.user, chat_text, thread=thread_key)
        if not streamer:
            self.memory.mark_greeted(line.user)
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
        await self.hub.broadcast({"type": "transcript", "data": {"role": "cohost", "text": spoken, "chat": chat_text}})
        await self.hub.broadcast({"type": "chat", "data": {"user": self.persona.get("name"), "text": chat_text, "bot": True}})
        if self.twitch and self.twitch.connected:
            try:
                await self.twitch.send(chat_text)
            except Exception:
                log.exception("chat send failed")
        want_speak = action == "speak" and not self.tts_mute and not self._busy_voice and not self.tts.speaking
        if want_speak:
            await self.hub.patch_status(phase="speaking", speaking=True, caption=spoken)
            await self._speak(spoken)
            await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle", speaking=False)
        else:
            await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle", caption=spoken)

    async def _persona_chat_reply(self, line: ChatLine, ambient: bool = False, retry: bool = False) -> str:
        extra = (
            "\nAnswer only the message above. No greeting by name. "
            "Do not ask what they want to talk about. Do not mention clips or moments.\n"
            if retry
            else ""
        )
        prev = self.memory.viewer(STREAMER_THREAD if self.identity.is_login(line.user) else line.user)
        prev_bit = ""
        if prev and prev.last_reply and not is_generic_filler(prev.last_reply, self._banned_extra()):
            prev_bit = f"\nYou already said to them: {prev.last_reply}\nDon't repeat that.\n"
        name = self.identity.spoken_name()
        fenced = fence_viewer_text(line.text)
        if self.identity.is_login(line.user):
            focus = STREAMER_THREAD
            who_line = f"{name} (the streamer, sitting next to you) just typed:\n{fenced}\n"
            ask = (
                "Reply only to the latest line inside the viewer markers. One short line. "
                "Do not repeat a joke or sentence you already used. "
                f"Do not start with Hey {name}. Do not ask them for memes, clips, or a topic."
            )
        else:
            focus = self.memory.viewer_thread_key(line.user)
            who_line = f"{line.user} in chat:\n{fenced}\nThis is a viewer, not {name}.\n"
            ask = f"One short Twitch chat line starting with @{line.user}."
        history_limit = 4 if ambient else None
        messages = [
            {"role": "system", "content": self.chat_prompt},
            *self._history_messages(focus, limit=history_limit),
            {
                "role": "user",
                "content": (
                    f"{who_line}"
                    f"{prev_bit}"
                    f"{self.memory.prompt_block(focus, streamer_name=name, is_voice=False, include_alerts=not ambient)}\n\n"
                    f"Live chat:\n{self._chat_context(self.settings.ambient_chat_context if ambient else None)}\n"
                    f"{extra}"
                    f"{ask}"
                ),
            },
        ]
        text = self._clean(await self.llm.complete(messages, temperature=mood_temperature(self.mood) if not retry else 0.9))
        if not text:
            return ""
        target = None if self.identity.is_login(line.user) else line.user
        return self._chat_text(text, target_user=target)

    def _can_run_mod_command(self, line: ChatLine) -> bool:
        if line.is_mod or line.is_broadcaster:
            return True
        return self.identity.is_login(line.user)

    def _shoutout_ready(self) -> bool:
        wait = self.settings.shoutout_cooldown_seconds
        if wait <= 0:
            return True
        return (time.monotonic() - self._last_shoutout_at) >= wait

    async def shoutout(self, login: str | None) -> None:
        target = (login or self._last_raider or "").lstrip("@").strip()
        if not target:
            raise ValueError("No raider to shout out")
        if not self.settings.twitch_client_id:
            raise RuntimeError("TWITCH_CLIENT_ID is required for shoutouts")
        if not self.eventsub or not self.eventsub.broadcaster_id or not self.eventsub.moderator_id:
            raise RuntimeError("EventSub is not connected, so the broadcaster id is unknown")
        token = self.settings.event_token()
        if target.lower() == self._last_raider.lower() and self._last_raider_id:
            to_id = self._last_raider_id
        else:
            to_id = await user_id(token, self.settings.twitch_client_id, target)
        try:
            await shoutout(
                token,
                self.settings.twitch_client_id,
                from_broadcaster_id=self.eventsub.broadcaster_id,
                to_broadcaster_id=to_id,
                moderator_id=self.eventsub.moderator_id,
            )
        except HelixError as e:
            if e.status == 429:
                log.warning("Helix shoutout rate limited (HTTP 429): %s", e)
            if e.status in (401, 403):
                await self.hub.patch_status(eventsub="token rejected", eventsub_error=str(e))
            raise
        self._last_shoutout_at = time.monotonic()
        await self.say_line(f"{target}, welcome in.", post_chat=True)

    async def mark_moment(self, note: str) -> dict[str, str]:
        text = (note or "bookmark").strip()
        self.memory.add_note(STREAMER_THREAD, text, is_streamer=True)
        await asyncio.to_thread(self.memory.flush)
        await self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()})
        marker_error = ""
        if self.eventsub and self.eventsub.broadcaster_id and self.settings.twitch_client_id:
            try:
                await create_marker(
                    self.settings.event_token(),
                    self.settings.twitch_client_id,
                    self.eventsub.broadcaster_id,
                    text,
                )
            except Exception as e:
                marker_error = str(e)
                log.info("stream marker skipped: %s", e)
        return {"note": text, "marker_error": marker_error}

    async def _note_skip(self, reason: str, user: str) -> None:
        if self._last_skip.get("reason") == reason and self._last_skip.get("user") == user:
            return
        self._last_skip = {"reason": reason, "user": user}
        await self.hub.patch_status(skip=dict(self._last_skip))
        await self.hub.broadcast({"type": "skip", "data": dict(self._last_skip)})

    def queue_snapshot(self) -> dict[str, list]:
        alerts = sorted(self._event_pending, key=lambda e: EVENT_PRIORITY.get(e.kind, 9))
        return {
            "chat": [{"user": line.user, "text": line.text[:120]} for line in list(self._chat_pending)[:12]],
            "alerts": [{"kind": event.kind, "text": event.scene()} for event in alerts],
        }

    async def _publish_queue(self) -> None:
        await self.hub.patch_status(queue=self.queue_snapshot())

    async def _memory_prune_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(3600)
                removed = await asyncio.to_thread(self.memory.prune)
                if removed and self.memory.dirty:
                    await asyncio.to_thread(self.memory.flush)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("memory prune failed")

    async def _memory_flush_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(0.4)
                if self.memory.dirty:
                    await asyncio.to_thread(self.memory.flush)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("memory flush failed")

    async def _level_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(0.2)
                if self.mic.listen_enabled or self.mic.ptt_down:
                    await self.hub.broadcast({"type": "level", "data": round(float(self.mic.level), 4)})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("level loop failed")
