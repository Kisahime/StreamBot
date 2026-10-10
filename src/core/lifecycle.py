"""Startup, profiles, mood, and model lifecycle for the orchestrator."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path

from src.audio.duck import Duck
from src.audio.mic import MicListener
from src.audio.stt import SpeechToText
from src.booth import BoothState, load_booth, save_booth
from src.hub import Hub
from src.identity import StreamerIdentity, load_identity, save_identity
from src.llm.ollama import OllamaClient
from src.llm.persona import (
    build_chat_prompt,
    build_voice_prompt,
    load_persona,
    mood_addon,
    validate_persona_text,
)
from src.memory import (
    STREAMER_THREAD,
    ConversationMemory,
    never_say_phrases,
)
from src.settings import WHISPER_LANGUAGES, Settings, atomic_write_text
from src.tts.piper import PiperTTS
from src.twitch.chat import ChatLine, TwitchChat
from src.twitch.eventsub import EventSubClient, StreamEvent

log = logging.getLogger("cohost.orch")


def _live(name: str):
    """Look up a name tests patch on src.orchestrator, at call time."""
    import src.orchestrator as mod

    return getattr(mod, name)


def bind(
    orch,
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
    orch.settings = settings
    orch.hub = hub
    orch.llm = OllamaClient(settings) if llm is None else llm
    orch.stt = SpeechToText(settings) if stt is None else stt
    orch.tts = PiperTTS(settings) if tts is None else tts
    orch.mic = (
        MicListener(settings, orch.on_utterance, speaking=orch._tts_speaking, on_barge=orch._barge_in)
        if mic is None
        else mic
    )
    orch.data_dir = data_dir or _live("ROOT") / "data"
    orch.duck = Duck(settings, data_dir=orch.data_dir)
    orch.twitch: TwitchChat | None = None
    orch.eventsub: EventSubClient | None = None
    orch.persona_path = persona_path or _live("ROOT") / "persona.yaml"
    orch.persona = load_persona(orch.persona_path)
    orch.identity: StreamerIdentity = load_identity(
        orch.data_dir / "streamer.json", default_login=settings.twitch_channel
    )
    orch._runtime_base = {
        "mood": "normal",
        "cooldown_seconds": float(settings.cooldown_seconds),
        "per_user_cooldown_seconds": float(settings.per_user_cooldown_seconds),
        "events_speak": bool(settings.events_speak),
        "post_voice_replies_to_chat": bool(settings.post_voice_replies_to_chat),
        "max_replies_per_minute": int(settings.max_replies_per_minute),
    }
    orch.booth = load_booth(
        orch.data_dir / "booth.json",
        fallback_mute=settings.tts_mute,
        fallback_post=settings.post_voice_replies_to_chat,
    )
    orch._base_mood = orch.booth.mood
    orch.mood_override = orch.booth.mood_override
    orch.mood = orch.booth.effective_mood()
    orch.profile_name = ""
    orch.settings.post_voice_replies_to_chat = orch.booth.post_voice_replies_to_chat
    orch._refresh_prompts()
    orch.memory = ConversationMemory(
        orch.data_dir / "memory.json",
        event_limit=settings.memory_events,
        viewer_limit=settings.memory_viewers,
        thread_turns=settings.memory_thread_turns,
        retention_days=settings.retention_days,
    )
    orch.memory.set_streamer_display(orch.identity.spoken_name())
    orch.memory.set_cohost_name(orch._cohost_name())
    if orch.booth.profile:
        try:
            orch._install_profile(orch.booth.profile, persist=False)
        except ValueError:
            orch.profile_name = ""
    orch._voice_gen = 0
    orch._voice_cancel = asyncio.Event()
    orch._chat_pending: deque[ChatLine] = deque(maxlen=60)
    orch._reply_times: deque[float] = deque()
    orch._last_chat_reply = 0.0
    orch._user_reply_at: dict[str, float] = {}
    orch._tasks: list[asyncio.Task] = []
    orch._bg: set[asyncio.Task] = set()
    orch._ollama_interval = 30.0
    orch._chat_q: asyncio.Queue[int] = asyncio.Queue()
    orch._event_q: asyncio.Queue[int] = asyncio.Queue()
    orch._chat_scheduled = False
    orch._event_scheduled = False
    orch._busy_voice = False
    orch._speak_depth = 0
    orch._event_pending: deque[StreamEvent] = deque(maxlen=40)
    orch._follow_names: list[str] = []
    orch._follow_task: asyncio.Task | None = None
    orch._cheers: list[StreamEvent] = []
    orch._cheer_task: asyncio.Task | None = None
    orch.tts_mute = orch.booth.tts_mute
    orch._want_twitch = False
    orch._want_events = False
    orch._eventsub_error = ""
    orch._eventsub_lock = asyncio.Lock()
    orch._stopping = False
    orch._irc_fatal = False
    orch._eventsub_fatal = False
    orch._token_check_at = 0.0
    orch.irc_auth_note = ""
    orch._loop: asyncio.AbstractEventLoop | None = None
    orch._last_skip = {"reason": "", "user": ""}
    orch._catchphrase_at: dict[str, float] = {}
    orch._last_raider = ""
    orch._last_raider_id = ""
    orch._last_shoutout_at = 0.0
    orch._last_control_at = 0.0
    orch._reward_at: dict[str, float] = {}
    orch._last_caption = ""
    orch._queue_seq = 0
    orch.timings: dict[str, int | None] = {
        "stt_ms": None,
        "first_token_ms": None,
        "first_audio_ms": None,
        "chat_send_ms": None,
    }


def _refresh_prompts(orch) -> None:
    streamer = orch.identity.spoken_name()
    addon = mood_addon(orch.mood)
    extra = f"\n{addon}\n" if addon else ""
    orch.voice_prompt = build_voice_prompt(orch.persona, streamer) + extra
    orch.chat_prompt = build_chat_prompt(orch.persona, streamer) + extra
    if getattr(orch, "memory", None) is not None:
        orch.memory.set_cohost_name(orch._cohost_name())


def _cohost_name(orch) -> str:
    return str(orch.persona.get("name") or "Cohost")


def _tts_speaking(orch) -> bool:
    return bool(getattr(orch.tts, "speaking", False))


def _barge_in(orch) -> None:
    orch._spawn(orch.interrupt(), "barge-in")


def _phrases(orch) -> list[str]:
    raw = orch.persona.get("catchphrases") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(p).strip() for p in raw if str(p).strip()]


def _banned_extra(orch) -> list[str]:
    return never_say_phrases(orch.persona)


def _persist_booth(orch) -> None:
    orch.booth = BoothState(
        listen=orch.mic.listen_enabled,
        tts_mute=orch.tts_mute,
        post_voice_replies_to_chat=orch.settings.post_voice_replies_to_chat,
        mood=orch._base_mood,
        profile=orch.profile_name,
        mood_override=orch.mood_override,
    )
    save_booth(orch.data_dir / "booth.json", orch.booth)


def _install_profile(orch, name: str, *, persist: bool) -> str:
    wanted = (name or "").strip()
    canonical = ""
    if wanted:
        for key in orch.settings.profiles:
            if key.lower() == wanted.lower():
                canonical = key
                break
        if not canonical:
            raise ValueError(f"Unknown profile: {name}")
    orch.profile_name = canonical
    values = dict(orch._runtime_base)
    if canonical:
        values.update(orch.settings.profiles[canonical])
    mood = str(values.get("mood") or "normal").lower()
    if mood not in ("chill", "normal", "hype"):
        mood = "normal"
    orch._base_mood = mood
    if persist:
        orch.mood_override = ""
        orch.mood = mood
    elif orch.mood_override:
        orch.mood = orch.mood_override
    else:
        orch.mood = mood
    orch.settings.cooldown_seconds = float(values["cooldown_seconds"])
    orch.settings.per_user_cooldown_seconds = float(values["per_user_cooldown_seconds"])
    orch.settings.events_speak = bool(values["events_speak"])
    orch.settings.post_voice_replies_to_chat = bool(values["post_voice_replies_to_chat"])
    orch.settings.max_replies_per_minute = int(values["max_replies_per_minute"])
    orch._refresh_prompts()
    if persist:
        orch._persist_booth()
    return canonical


async def apply_profile(orch, name: str) -> str:
    canonical = orch._install_profile(name, persist=True)
    await orch.hub.patch_status(
        mood=orch.mood, profile=canonical, post_voice=bool(orch.settings.post_voice_replies_to_chat)
    )
    return canonical


async def start(orch, loop: asyncio.AbstractEventLoop) -> None:
    orch._loop = loop
    orch._stopping = False
    await orch.hub.patch_status(
        phase="starting",
        tts_mute=orch.tts_mute,
        persona=str(orch.persona.get("name") or "Cohost"),
        streamer_name=orch.identity.spoken_name(),
        streamer_login=orch.identity.twitch_login,
        eventsub="disconnected",
        mood=orch.mood,
        duck=orch.duck.mode,
        post_voice=bool(orch.settings.post_voice_replies_to_chat),
        duck_enabled=bool(orch.settings.duck_enabled),
        wake=orch._wake_status(),
    )
    await orch._resolve_audio_devices()
    orch._tasks = [
        orch._spawn(orch._load_models(), "load-models"),
        orch._spawn(orch._chat_worker(), "chat-worker"),
        orch._spawn(orch._event_worker(), "event-worker"),
        orch._spawn(orch._chat_scan_loop(), "chat-scan"),
        orch._spawn(orch._twitch_watchdog(), "twitch-watch"),
        orch._spawn(orch._memory_flush_loop(), "memory-flush"),
        orch._spawn(orch._memory_prune_loop(), "memory-prune"),
        orch._spawn(orch._level_loop(), "mic-level"),
        orch._spawn(orch._ollama_watch(), "ollama-watch"),
    ]
    try:
        orch.mic.start(loop)
    except Exception as e:
        log.exception("Microphone failed")
        await orch.hub.broadcast({"type": "log", "data": f"Microphone failed: {e}"})
    health = await orch.llm.health()
    await orch.hub.patch_status(phase="idle", ollama="ok" if health.get("ok") else "down")


async def _load_models(orch) -> None:
    try:
        await asyncio.to_thread(orch.stt.load)
        await orch.hub.broadcast({"type": "log", "data": f"Whisper ready on {orch.stt.device}"})
    except Exception as e:
        log.exception("Whisper failed to load")
        await orch.hub.broadcast({"type": "log", "data": f"Whisper load failed: {e}"})
    try:
        await asyncio.to_thread(orch.tts.ensure_files)
        msg = "Piper TTS ready" if orch.tts.ready else f"Piper: {orch.tts.error}"
        await orch.hub.broadcast({"type": "log", "data": msg})
    except Exception as e:
        log.exception("Piper setup failed")
        await orch.hub.broadcast({"type": "log", "data": f"Piper setup failed: {e}"})
    ready, why = await orch._models_ready()
    await orch.hub.patch_status(
        stt_ready=orch.stt.model is not None, tts_ready=orch.tts.ready or not orch.settings.tts_enabled
    )
    if orch.booth.listen:
        if ready:
            await orch.set_listen(True)
        else:
            await orch.hub.broadcast({"type": "log", "data": f"Not listening yet: {why}"})


async def _models_ready(orch) -> tuple[bool, str]:
    if orch.stt.model is None:
        return (False, "Whisper is not loaded")
    if orch.settings.tts_enabled and (not orch.tts.ready):
        return (False, orch.tts.error or "Piper is not ready")
    health = await orch.llm.health()
    if not health.get("ok"):
        return (False, "Ollama is down")
    if not health.get("has_model"):
        return (False, f"Pull the model: ollama pull {orch.settings.ollama_model}")
    return (True, "")


async def shutdown(orch) -> None:
    orch._stopping = True
    for task in (orch._follow_task, orch._cheer_task):
        if task and (not task.done()):
            task.cancel()
    await orch._emit_follows()
    await orch._emit_cheers()
    for event in list(orch._event_pending):
        orch.memory.remember(
            "twitch_event", event.user or "chat", event.scene(), via="event", thread=STREAMER_THREAD
        )
    for task in list(orch._bg):
        task.cancel()
    for task in list(orch._bg):
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    orch._bg.clear()
    orch._tasks.clear()
    await orch.mic.stop()
    if orch.eventsub:
        await orch.eventsub.disconnect()
    if orch.twitch:
        await orch.twitch.disconnect()
    orch.tts.interrupt()
    orch.tts.close()
    orch.duck.up()
    await asyncio.to_thread(orch.memory.flush)
    await orch.llm.aclose()


def set_identity(orch, display_name: str, twitch_login: str) -> StreamerIdentity:
    orch.identity.display_name = (display_name or "").strip()
    orch.identity.twitch_login = (twitch_login or "").strip().lstrip("#")
    if not orch.identity.twitch_login:
        orch.identity.twitch_login = (orch.settings.twitch_channel or "").strip().lstrip("#")
    save_identity(orch.data_dir / "streamer.json", orch.identity)
    orch.memory.set_streamer_display(orch.identity.spoken_name())
    orch._refresh_prompts()
    if orch.twitch:
        orch.twitch.set_allow_from([orch.identity.login_key()] if orch.identity.login_key() else [])
    orch._spawn(
        orch.hub.patch_status(
            streamer_name=orch.identity.spoken_name(), streamer_login=orch.identity.twitch_login
        ),
        "identity-status",
    )
    login = orch.identity.twitch_login or "?"
    orch._spawn(
        orch.hub.broadcast({"type": "log", "data": f"Streamer is {orch.identity.spoken_name()} (@{login})"}),
        "identity-log",
    )
    return orch.identity


def forget_viewer(orch, login: str) -> None:
    orch.memory.forget(login)
    orch.memory.flush()
    orch._spawn(orch.hub.broadcast({"type": "memory", "data": orch.memory.snapshot()}), "memory")


def clear_memory(orch) -> None:
    orch.memory.clear()
    orch._user_reply_at.clear()
    orch._spawn(orch.hub.broadcast({"type": "log", "data": "Memory cleared"}), "memory-cleared")
    orch._spawn(orch.hub.broadcast({"type": "memory", "data": orch.memory.snapshot()}), "memory")


def reload_persona(orch) -> None:
    orch.persona = load_persona(orch.persona_path)
    orch._refresh_prompts()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    orch._spawn(orch._after_persona_reload(), "persona-reload")


def save_persona_text(orch, text: str) -> str:
    data = validate_persona_text(text)
    body = text if text.endswith("\n") else text + "\n"
    atomic_write_text(orch.persona_path, body, backup=True)
    orch.reload_persona()
    return str(data.get("name"))


async def _after_persona_reload(orch) -> None:
    name = str(orch.persona.get("name") or "Cohost")
    await orch.hub.patch_status(persona=name)
    await orch.hub.broadcast({"type": "log", "data": f"Persona reloaded: {name}"})


async def set_mood(orch, mood: str) -> str:
    mood = (mood or "normal").lower()
    if mood not in ("chill", "normal", "hype"):
        raise ValueError("mood must be chill, normal, or hype")
    orch.mood = mood
    orch.mood_override = mood
    orch._refresh_prompts()
    orch._persist_booth()
    await orch.hub.patch_status(mood=mood)
    return mood


async def set_listen(orch, enabled: bool) -> bool:
    if enabled:
        ready, why = await orch._models_ready()
        if not ready:
            await orch.hub.broadcast({"type": "log", "data": f"Start listening refused: {why}"})
            await orch.hub.patch_status(
                listen=False,
                stt_ready=orch.stt.model is not None,
                tts_ready=orch.tts.ready or not orch.settings.tts_enabled,
                phase="idle",
            )
            return False
    orch.mic.set_listen(enabled)
    phase = "listening" if enabled else "speaking" if orch.tts.speaking else "idle"
    orch._persist_booth()
    await orch.hub.patch_status(
        listen=enabled,
        phase=phase,
        stt_ready=orch.stt.model is not None,
        tts_ready=orch.tts.ready or not orch.settings.tts_enabled,
    )
    return True


async def set_ptt(orch, down: bool) -> None:
    if down:
        ready, why = await orch._models_ready()
        if not ready:
            await orch.hub.broadcast({"type": "log", "data": f"Hold to talk refused: {why}"})
            return
    orch.mic.set_ptt(down)
    await orch.hub.patch_status(ptt=down, listen=orch.mic.listen_enabled or down)


async def set_mute(orch, mute: bool) -> None:
    orch.tts_mute = mute
    orch.settings.tts_mute = mute
    if mute:
        orch._bump_voice()
    orch._persist_booth()
    await orch.hub.patch_status(tts_mute=mute)


def set_post_voice(orch, enabled: bool) -> None:
    orch.settings.post_voice_replies_to_chat = bool(enabled)
    orch.hub.status["post_voice"] = bool(enabled)
    orch._persist_booth()


def _wake_status(orch) -> dict:
    wake = getattr(orch.mic, "wake", None)
    if wake is None or not hasattr(wake, "status"):
        return {"enabled": False}
    try:
        reported = wake.status()
    except Exception:
        return {"enabled": False}
    return reported if isinstance(reported, dict) else {"enabled": False}


async def set_vad(orch, silence_ms: int, energy: float, max_seconds: float) -> None:
    orch.settings.silence_ms = int(silence_ms)
    orch.settings.energy_threshold = float(energy)
    orch.settings.max_utterance_seconds = float(max_seconds)
    await asyncio.to_thread(
        _live("update_config"),
        {
            "audio.silence_ms": int(silence_ms),
            "audio.energy_threshold": float(energy),
            "audio.max_utterance_seconds": float(max_seconds),
        },
    )


async def set_devices(
    orch,
    mic_device: int | None,
    speaker_device: int | None,
    mic_name: str | None = None,
    speaker_name: str | None = None,
) -> None:
    orch.settings.mic_device = mic_device
    orch.settings.speaker_device = speaker_device
    if mic_name is not None:
        orch.settings.mic_device_name = mic_name or None
    if speaker_name is not None:
        orch.settings.speaker_device_name = speaker_name or None
    await asyncio.to_thread(
        _live("update_config"),
        {
            "audio.mic_device": mic_device,
            "audio.speaker_device": speaker_device,
            "audio.mic_device_name": orch.settings.mic_device_name,
            "audio.speaker_device_name": orch.settings.speaker_device_name,
        },
    )
    if orch._loop is None:
        return
    listen = orch.mic.listen_enabled
    ptt = orch.mic.ptt_down
    await orch.mic.stop()
    orch.mic.listen_enabled = listen
    orch.mic.ptt_down = ptt
    orch.mic.start(orch._loop)


async def set_language(orch, language: str) -> None:
    language = (language or "").strip().lower()
    if language not in WHISPER_LANGUAGES:
        raise ValueError(f"unknown language: {language}")
    orch.settings.whisper_language = language
    await asyncio.to_thread(_live("update_config"), {"whisper.language": language})
    await asyncio.to_thread(orch.stt.load)
    await orch.hub.patch_status(stt_ready=orch.stt.model is not None)


async def set_voice(orch, voice: str) -> None:
    voice = (voice or "").strip()
    if voice not in orch.settings.tts_voices:
        raise ValueError(f"unknown voice: {voice}")
    orch.tts.close()
    orch.settings.tts_voice = voice

    def progress(message: str) -> None:
        if orch._loop is None:
            return
        asyncio.run_coroutine_threadsafe(orch.hub.broadcast({"type": "log", "data": message}), orch._loop)

    orch.tts.on_progress = progress
    await asyncio.to_thread(orch.tts.ensure_files)
    await asyncio.to_thread(_live("update_config"), {"tts.voice": voice})
    await orch.hub.patch_status(tts_ready=orch.tts.ready)


def set_duck(orch, enabled: bool) -> None:
    orch.duck.set_enabled(enabled)
    orch.hub.status["duck_enabled"] = bool(enabled)
    orch.hub.status["duck"] = orch.duck.mode
    _live("update_config")({"audio.duck_enabled": enabled})


async def _note_skip(orch, reason: str, user: str) -> None:
    if orch._last_skip.get("reason") == reason and orch._last_skip.get("user") == user:
        return
    orch._last_skip = {"reason": reason, "user": user}
    await orch.hub.patch_status(skip=dict(orch._last_skip))
    await orch.hub.broadcast({"type": "skip", "data": dict(orch._last_skip)})


async def _memory_prune_loop(orch) -> None:
    while not orch._stopping:
        try:
            await asyncio.sleep(3600)
            removed = await asyncio.to_thread(orch.memory.prune)
            if removed and orch.memory.dirty:
                await asyncio.to_thread(orch.memory.flush)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("memory prune failed")


async def _memory_flush_loop(orch) -> None:
    while not orch._stopping:
        try:
            await asyncio.sleep(0.4)
            if orch.memory.dirty:
                await asyncio.to_thread(orch.memory.flush)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("memory flush failed")


async def _resolve_audio_devices(orch) -> None:
    from src.audio.devices import list_devices, match_device

    try:
        devices = await asyncio.to_thread(list_devices)
    except Exception:
        log.exception("device resolve failed")
        return
    orch.settings.mic_device = match_device(
        list(devices.get("inputs") or []), orch.settings.mic_device, orch.settings.mic_device_name
    )
    orch.settings.speaker_device = match_device(
        list(devices.get("outputs") or []), orch.settings.speaker_device, orch.settings.speaker_device_name
    )


async def _ollama_watch(orch) -> None:
    while not orch._stopping:
        try:
            await asyncio.sleep(orch._ollama_interval)
            if orch._stopping:
                return
            health = await orch.llm.health()
            await orch.hub.patch_status(ollama="ok" if health.get("ok") else "down")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ollama watch failed")


def restore_log_level(orch) -> None:
    previous = getattr(orch, "_debug_log_level", logging.NOTSET)
    logging.getLogger("cohost").setLevel(previous)


async def enable_debug_log(orch, seconds: float = 600) -> None:
    logger = logging.getLogger("cohost")
    if not getattr(orch, "_debug_log_active", False):
        orch._debug_log_level = logger.level
        orch._debug_log_active = True
    logger.setLevel(logging.DEBUG)
    orch._spawn(orch._debug_log_window(seconds), "debug-log")


async def _debug_log_window(orch, seconds: float) -> None:
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        raise
    orch._debug_log_active = False
    orch.restore_log_level()
