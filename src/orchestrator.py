from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional

import numpy as np

from src.audio.mic import MicListener
from src.audio.stt import SpeechToText
from src.audio.vad import trim_to_speech
from src.hub import Hub
from src.identity import StreamerIdentity, load_identity, save_identity
from src.llm.ollama import OllamaClient
from src.llm.persona import build_chat_prompt, build_voice_prompt, load_persona
from src.memory import STREAMER_THREAD, ConversationMemory, ensure_chat_mention, for_speech, is_generic_filler, sanitize_reply
from src.settings import ROOT, Settings
from src.tts.piper import PiperTTS
from src.twitch.chat import ChatLine, TwitchChat
from src.twitch.eventsub import EventSubClient, StreamEvent

log = logging.getLogger("cohost.orch")

EVENT_PRIORITY = {"raid": 0, "gift": 1, "sub": 2, "resub": 3, "follow": 4}


class Orchestrator:
    def __init__(self, settings: Settings, hub: Hub) -> None:
        self.settings = settings
        self.hub = hub
        self.llm = OllamaClient(settings)
        self.stt = SpeechToText(settings)
        self.tts = PiperTTS(settings)
        self.mic = MicListener(settings, self.on_utterance)
        self.twitch: Optional[TwitchChat] = None
        self.eventsub: Optional[EventSubClient] = None
        self.persona = load_persona()
        self.identity: StreamerIdentity = load_identity(
            ROOT / "data" / "streamer.json",
            default_login=settings.twitch_channel,
        )
        self._refresh_prompts()
        self.memory = ConversationMemory(
            ROOT / "data" / "memory.json",
            event_limit=settings.memory_events,
            viewer_limit=settings.memory_viewers,
            thread_turns=settings.memory_thread_turns,
        )
        self.memory.set_streamer_display(self.identity.spoken_name())
        self.spoken: deque[tuple[str, str]] = deque(maxlen=settings.spoken_turns)
        self._voice_gen = 0
        self._chat_pending: deque[ChatLine] = deque(maxlen=60)
        self._reply_times: deque[float] = deque()
        self._last_chat_reply = 0.0
        self._user_reply_at: dict[str, float] = {}
        self._scan_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._busy_voice = False
        self._chat_lock = asyncio.Lock()
        self._event_lock = asyncio.Lock()
        self._event_pending: deque[StreamEvent] = deque(maxlen=40)
        self._follow_names: list[str] = []
        self._follow_task: Optional[asyncio.Task] = None
        self.tts_mute = settings.tts_mute
        self._want_twitch = False

    def _refresh_prompts(self) -> None:
        streamer = self.identity.spoken_name()
        self.voice_prompt = build_voice_prompt(self.persona, streamer)
        self.chat_prompt = build_chat_prompt(self.persona, streamer)
        self.system_prompt = self.voice_prompt

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        await self.hub.patch_status(
            phase="starting",
            tts_mute=self.tts_mute,
            persona=str(self.persona.get("name") or "Cohost"),
            streamer_name=self.identity.spoken_name(),
            streamer_login=self.identity.twitch_login,
            eventsub="disconnected",
        )
        loop.create_task(self._load_models())
        try:
            self.mic.start(loop)
        except Exception as e:
            log.exception("Microphone failed")
            await self.hub.broadcast({"type": "log", "data": f"Microphone failed: {e}"})
        self._scan_task = loop.create_task(self._chat_scan_loop(), name="chat-scan")
        self._reconnect_task = loop.create_task(self._twitch_watchdog(), name="twitch-watch")
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
            await self.hub.broadcast({"type": "log", "data": "Piper TTS ready" if self.tts.ready else f"Piper: {self.tts.error}"})
        except Exception as e:
            log.exception("Piper setup failed")
            await self.hub.broadcast({"type": "log", "data": f"Piper setup failed: {e}"})

    async def shutdown(self) -> None:
        if self._scan_task:
            self._scan_task.cancel()
        if self._reconnect_task:
            self._reconnect_task.cancel()
        await self.mic.stop()
        if self._follow_task:
            self._follow_task.cancel()
        if self.eventsub:
            await self.eventsub.disconnect()
        if self.twitch:
            await self.twitch.disconnect()
        self.tts.interrupt()

    def set_identity(self, display_name: str, twitch_login: str) -> StreamerIdentity:
        self.identity.display_name = (display_name or "").strip()
        self.identity.twitch_login = (twitch_login or "").strip().lstrip("#")
        if not self.identity.twitch_login:
            self.identity.twitch_login = (self.settings.twitch_channel or "").strip().lstrip("#")
        save_identity(ROOT / "data" / "streamer.json", self.identity)
        self.memory.set_streamer_display(self.identity.spoken_name())
        self._refresh_prompts()
        asyncio.create_task(
            self.hub.patch_status(
                streamer_name=self.identity.spoken_name(),
                streamer_login=self.identity.twitch_login,
            )
        )
        asyncio.create_task(self.hub.broadcast({"type": "log", "data": f"Streamer is {self.identity.spoken_name()} (@{self.identity.twitch_login or '?'})"}))
        return self.identity

    def clear_memory(self) -> None:
        self.memory.clear()
        self.spoken.clear()
        self._user_reply_at.clear()
        asyncio.create_task(self.hub.broadcast({"type": "log", "data": "Memory cleared"}))
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))

    def reload_persona(self) -> None:
        self.persona = load_persona()
        self._refresh_prompts()
        asyncio.create_task(self._after_persona_reload())

    async def _after_persona_reload(self) -> None:
        name = str(self.persona.get("name") or "Cohost")
        await self.hub.patch_status(persona=name)
        await self.hub.broadcast({"type": "log", "data": f"Persona reloaded: {name}"})

    async def set_listen(self, enabled: bool) -> None:
        self.mic.set_listen(enabled)
        phase = "listening" if enabled else ("speaking" if self.tts.speaking else "idle")
        await self.hub.patch_status(listen=enabled, phase=phase)

    async def set_ptt(self, down: bool) -> None:
        self.mic.set_ptt(down)
        await self.hub.patch_status(ptt=down, listen=self.mic.listen_enabled or down)

    async def set_mute(self, mute: bool) -> None:
        self.tts_mute = mute
        self.settings.tts_mute = mute
        if mute:
            self.tts.interrupt()
        await self.hub.patch_status(tts_mute=mute)

    async def interrupt(self) -> None:
        self._voice_gen += 1
        self.tts.interrupt()
        await self.hub.patch_status(speaking=False, caption="", phase="listening" if self.mic.listen_enabled else "idle")

    async def twitch_connect(self) -> None:
        self.settings.reload_secrets()
        self._want_twitch = True
        await self._irc_connect()
        await self._eventsub_connect()

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
        )
        await self.twitch.connect()

    async def _eventsub_connect(self) -> None:
        if not self.settings.events_enabled:
            await self.hub.patch_status(eventsub="off")
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
            )
            await self.eventsub.connect()
        except Exception as e:
            log.warning("EventSub not connected: %s", e)
            self.eventsub = None
            await self.hub.patch_status(eventsub="error")
            await self.hub.broadcast({"type": "log", "data": f"EventSub: {e}"})

    async def twitch_disconnect(self) -> None:
        self._want_twitch = False
        if self.eventsub:
            await self.eventsub.disconnect()
            self.eventsub = None
        if self.twitch:
            await self.twitch.disconnect()
            self.twitch = None

    async def _twitch_watchdog(self) -> None:
        while True:
            try:
                await asyncio.sleep(20)
                if not self.settings.auto_reconnect_twitch or not self._want_twitch:
                    continue
                if not (self.twitch and self.twitch.alive()):
                    await self.hub.broadcast({"type": "log", "data": "Twitch chat dropped — reconnecting"})
                    try:
                        await self._irc_connect()
                    except Exception as e:
                        await self.hub.broadcast({"type": "log", "data": f"Twitch reconnect failed: {e}"})
                if self.settings.events_enabled and not (self.eventsub and self.eventsub.alive()):
                    await self.hub.broadcast({"type": "log", "data": "EventSub dropped — reconnecting"})
                    await self._eventsub_connect()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("twitch watchdog failed")

    async def _twitch_status(self, state: str) -> None:
        await self.hub.patch_status(twitch=state)

    async def _eventsub_status(self, state: str) -> None:
        await self.hub.patch_status(eventsub=state)

    async def on_stream_event(self, event: StreamEvent) -> None:
        await self.hub.broadcast(
            {
                "type": "alert",
                "data": {"kind": event.kind, "user": event.user, "people": event.people(), "text": event.scene()},
            }
        )
        await self.hub.broadcast({"type": "log", "data": f"Alert {event.kind}: {event.scene()}"})
        if event.kind == "follow":
            if not self.settings.announce_follows:
                return
            self._follow_names.extend(event.people())
            if not self._follow_task or self._follow_task.done():
                self._follow_task = asyncio.create_task(self._flush_follows(), name="follow-batch")
            return
        self._event_pending.append(event)
        asyncio.create_task(self._consider_events())

    async def _flush_follows(self) -> None:
        try:
            await asyncio.sleep(self.settings.follow_batch_seconds)
        except asyncio.CancelledError:
            return
        names = [n for n in self._follow_names if n]
        self._follow_names = []
        if not names:
            return
        batched = StreamEvent(kind="follow", user=names[0], users=names[1:])
        self._event_pending.append(batched)
        await self._consider_events()

    async def _consider_events(self) -> None:
        if self._event_lock.locked():
            return
        async with self._event_lock:
            while self._event_pending:
                if self._busy_voice:
                    return
                event = min(self._event_pending, key=lambda e: EVENT_PRIORITY.get(e.kind, 9))
                try:
                    self._event_pending.remove(event)
                except ValueError:
                    break
                await self._react_event(event)
            if self._chat_pending and not self._busy_voice:
                asyncio.create_task(self._consider_chat(force=False))

    async def _react_event(self, event: StreamEvent) -> None:
        if self._busy_voice:
            self._event_pending.appendleft(event)
            return
        scene = event.scene()
        people = event.people()
        primary = people[0] if people else (event.user or "chat")
        thread = self.memory.viewer_thread_key(primary) if primary and primary.lower() not in ("anonymous", "chat") else STREAMER_THREAD
        self.memory.remember("twitch_event", primary, scene, via="event", thread=thread)
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
        name = self.identity.spoken_name()
        messages = [
            {"role": "system", "content": self.voice_prompt},
            {
                "role": "user",
                "content": (
                    f"You're on the couch next to {name}. This just happened on stream:\n{scene}\n\n"
                    "Hype it like a cohost for a second, then drop it. No corporate thank-you. "
                    "No 'thank you so much for the follow'. Words to speak aloud only."
                ),
            },
        ]
        await self.hub.patch_status(phase="thinking")
        raw = await self.llm.complete(messages, temperature=0.9)
        reply = sanitize_reply(raw, str(self.persona.get("name") or ""))
        if not reply or is_generic_filler(reply):
            raw = await self.llm.complete(
                messages
                + [{"role": "user", "content": "shorter, messier, no greeting, no thank-you template."}],
                temperature=0.95,
            )
            reply = sanitize_reply(raw, str(self.persona.get("name") or ""))
        if not reply or is_generic_filler(reply):
            await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle")
            return
        spoken = for_speech(reply)
        chat_text = reply[: self.settings.chat_max_chars]
        self.memory.remember(
            "cohost_voice",
            str(self.persona.get("name") or "Pixel"),
            spoken,
            via="voice",
            thread=thread,
        )
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
        await self.hub.broadcast({"type": "transcript", "data": {"role": "cohost", "text": spoken}})
        if self.settings.events_post_to_chat and self.twitch and self.twitch.connected:
            try:
                await self.twitch.send(chat_text)
                await self.hub.broadcast(
                    {"type": "chat", "data": {"user": self.persona.get("name"), "text": chat_text, "bot": True}}
                )
            except Exception:
                log.exception("event chat send failed")
        want_speak = self.settings.events_speak and not self.tts_mute and not self._busy_voice
        if want_speak:
            await self.hub.patch_status(phase="speaking", speaking=True, caption=spoken)
            await self.tts.speak(spoken)
        await self.hub.patch_status(
            phase="listening" if self.mic.listen_enabled else "idle",
            speaking=False,
            caption=spoken[-280:],
        )

    async def on_chat(self, line: ChatLine) -> None:
        if self.identity.is_login(line.user):
            name = self.identity.spoken_name()
            self.memory.remember("streamer_chat", name, line.text, via="chat", thread=STREAMER_THREAD)
            await self.hub.broadcast(
                {
                    "type": "chat",
                    "data": {"user": name, "text": line.text, "mention": line.mention, "streamer": True},
                }
            )
            asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
            if line.mention:
                asyncio.create_task(self._consider_chat(force=True, prefer=line))
            return
        self._chat_pending.append(line)
        self.memory.remember("viewer", line.user, line.text, via="chat", thread=self.memory.viewer_thread_key(line.user))
        await self.hub.broadcast({"type": "chat", "data": {"user": line.user, "text": line.text, "mention": line.mention}})
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
        asyncio.create_task(self._consider_chat(force=line.is_engaging(), prefer=line))

    async def on_utterance(self, audio: np.ndarray) -> None:
        self._voice_gen += 1
        gen = self._voice_gen
        self._busy_voice = True
        self.tts.interrupt()
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
            if gen != self._voice_gen:
                return
            if not text:
                await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle")
                return
            await self.hub.patch_status(last_user=text, streamer_name=self.identity.spoken_name())
            await self.hub.broadcast({"type": "transcript", "data": {"role": "streamer", "text": text, "name": self.identity.spoken_name()}})
            self.memory.remember("streamer_voice", self.identity.spoken_name(), text, via="voice", thread=STREAMER_THREAD)
            await self._voice_reply(text, gen)
        finally:
            self._busy_voice = False
            if self._event_pending:
                asyncio.create_task(self._consider_events())
            elif self._chat_pending:
                asyncio.create_task(self._consider_chat(force=False))

    def _chat_context(self) -> str:
        if not self.twitch:
            live = "(no chat connected)"
        else:
            lines = self.twitch.recent(self.settings.chat_context_lines)
            live = "\n".join(l.render() for l in lines) if lines else "(chat is quiet)"
        return live

    def _history_messages(self, thread_key: str) -> list[dict[str, str]]:
        return self.memory.llm_messages(thread_key)

    async def _voice_reply(self, user_text: str, gen: int) -> None:
        name = self.identity.spoken_name()
        messages = [
            {"role": "system", "content": self.voice_prompt},
            *self._history_messages(STREAMER_THREAD),
            {
                "role": "user",
                "content": (
                    f"You're on the couch next to {name}. They just said into the mic:\n\"{user_text}\"\n\n"
                    f"{self.memory.prompt_block(STREAMER_THREAD, streamer_name=name, is_voice=True)}\n\n"
                    f"People talking in chat (not {name}):\n{self._chat_context()}\n\n"
                    f"React to {name} like you're sitting next to them. Don't recap. Don't @ anyone."
                ),
            },
        ]
        full: list[str] = []
        async for sentence in self.llm.stream_sentences(messages, temperature=0.9):
            if gen != self._voice_gen:
                return
            sentence = sanitize_reply(sentence.strip(), str(self.persona.get("name") or ""))
            if not sentence or is_generic_filler(sentence):
                continue
            full.append(sentence)
            await self.hub.patch_status(phase="speaking", speaking=True, caption=" ".join(full)[-280:])
            if not self.tts_mute:
                await self.tts.speak(sentence)
            if gen != self._voice_gen:
                return
        reply = sanitize_reply(" ".join(full).strip(), str(self.persona.get("name") or ""))
        if not reply:
            err = self.llm.last_error or "empty model reply"
            await self.hub.broadcast({"type": "log", "data": f"LLM: {err}"})
            await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle", speaking=False)
            return
        self.spoken.append(("user", f"{name} (mic): {user_text}"))
        self.spoken.append(("assistant", reply))
        self.memory.remember(
            "cohost_voice",
            str(self.persona.get("name") or "Pixel"),
            reply,
            via="voice",
            thread=STREAMER_THREAD,
        )
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
        await self.hub.broadcast({"type": "transcript", "data": {"role": "cohost", "text": reply}})
        if self.settings.post_voice_replies_to_chat and self.twitch and self.twitch.connected:
            short = reply[: self.settings.chat_max_chars]
            try:
                await self.twitch.send(short)
                self.memory.remember(
                    "cohost_chat",
                    str(self.persona.get("name") or "Pixel"),
                    short,
                    via="chat",
                    thread=STREAMER_THREAD,
                )
                await self.hub.broadcast({"type": "chat", "data": {"user": self.persona.get("name"), "text": short, "bot": True}})
            except Exception:
                log.exception("failed to echo voice reply to chat")
        await self.hub.patch_status(
            phase="listening" if self.mic.listen_enabled else "idle",
            speaking=False,
            caption=reply[-280:],
        )

    def _rate_ok(self, *, mention: bool, user: str = "") -> bool:
        now = time.monotonic()
        while self._reply_times and now - self._reply_times[0] > 60:
            self._reply_times.popleft()
        if len(self._reply_times) >= self.settings.max_replies_per_minute:
            return False
        key = (user or "").lower()
        if key:
            last = self._user_reply_at.get(key, 0.0)
            wait = min(8.0, self.settings.per_user_cooldown_seconds) if mention else self.settings.per_user_cooldown_seconds
            if now - last < wait:
                return False
        if mention:
            return now - self._last_chat_reply >= min(4.0, self.settings.cooldown_seconds)
        return now - self._last_chat_reply >= self.settings.cooldown_seconds

    def _mark_reply(self, user: str = "") -> None:
        now = time.monotonic()
        self._last_chat_reply = now
        self._reply_times.append(now)
        if user:
            self._user_reply_at[user.lower()] = now

    async def _chat_scan_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.settings.scan_interval_seconds)
                await self._consider_chat(force=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("chat scan failed")

    async def _consider_chat(self, force: bool = False, prefer: Optional[ChatLine] = None) -> None:
        if self._chat_lock.locked():
            return
        async with self._chat_lock:
            await self._consider_chat_locked(force=force, prefer=prefer)

    async def _consider_chat_locked(self, force: bool = False, prefer: Optional[ChatLine] = None) -> None:
        if self._busy_voice:
            return
        if self._event_pending:
            asyncio.create_task(self._consider_events())
            return
        if not self._chat_pending and not prefer:
            return
        engaging = bool(prefer and prefer.is_engaging()) or any(l.is_engaging() for l in self._chat_pending)
        target = prefer
        if target is None:
            mentioned = [l for l in self._chat_pending if l.is_engaging()]
            target = mentioned[-1] if mentioned else (self._chat_pending[-1] if self._chat_pending else None)
        if target is None:
            return
        if not self._rate_ok(mention=engaging or force, user=target.user):
            return
        kept = [l for l in self._chat_pending if l is not target and l.user.lower() != target.user.lower()]
        self._chat_pending.clear()
        self._chat_pending.extend(kept)
        await self._decide_chat(target)

    async def _decide_chat(self, line: ChatLine) -> None:
        if self._busy_voice:
            return
        await self.hub.patch_status(phase="thinking")
        action, reply = await self._persona_chat_reply(line)
        reply = sanitize_reply(reply, str(self.persona.get("name") or ""))
        if action == "ignore" or not reply or is_generic_filler(reply):
            if is_generic_filler(reply):
                action, reply = await self._persona_chat_reply(line, retry=True)
                reply = sanitize_reply(reply, str(self.persona.get("name") or ""))
            if action == "ignore" or not reply or is_generic_filler(reply):
                await self.hub.broadcast(
                    {"type": "log", "data": f"Chat skip ({line.user}): {line.text[:80]}"}
                )
                await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle")
                return
        if line.is_engaging():
            action = "speak"
        chat_text = ensure_chat_mention(reply, line.user)[: self.settings.chat_max_chars]
        if self.identity.is_login(line.user):
            chat_text = sanitize_reply(reply, str(self.persona.get("name") or ""))[: self.settings.chat_max_chars]
        spoken = for_speech(chat_text)
        thread_key = STREAMER_THREAD if self.identity.is_login(line.user) else self.memory.viewer_thread_key(line.user)
        self._mark_reply(line.user)
        self.memory.remember(
            "cohost_chat",
            str(self.persona.get("name") or "Pixel"),
            chat_text,
            via="chat",
            thread=thread_key,
        )
        self.memory.note_reply_to(line.user, chat_text, thread=thread_key)
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))
        self.spoken.append(("user", f"{line.user} in chat: {line.text}"))
        self.spoken.append(("assistant", chat_text))
        await self.hub.broadcast({"type": "transcript", "data": {"role": "cohost", "text": spoken, "chat": chat_text}})
        await self.hub.broadcast({"type": "chat", "data": {"user": self.persona.get("name"), "text": chat_text, "bot": True}})
        if self.twitch and self.twitch.connected:
            try:
                await self.twitch.send(chat_text)
            except Exception:
                log.exception("chat send failed")
        want_speak = action == "speak" and not self.tts_mute
        if want_speak and (self._busy_voice or self.tts.speaking):
            want_speak = False
        if want_speak:
            await self.hub.patch_status(phase="speaking", speaking=True, caption=spoken)
            await self.tts.speak(spoken)
            await self.hub.patch_status(
                phase="listening" if self.mic.listen_enabled else "idle",
                speaking=False,
            )
        else:
            await self.hub.patch_status(phase="listening" if self.mic.listen_enabled else "idle", caption=spoken)

    async def _persona_chat_reply(self, line: ChatLine, retry: bool = False) -> tuple[str, str]:
        extra = ""
        if retry:
            extra = "\nShorter, messier, no greeting, no helpdesk energy.\n"
        prev = self.memory.viewer(STREAMER_THREAD if self.identity.is_login(line.user) else line.user)
        prev_bit = ""
        if prev and prev.last_reply:
            prev_bit = f"\nYou already said to them: {prev.last_reply}\nDon't repeat that.\n"
        name = self.identity.spoken_name()
        if self.identity.is_login(line.user):
            focus = STREAMER_THREAD
            who_line = f'{name} (the streamer) just typed in chat: "{line.text}"\n'
            ask = f"One short chat line to {name}. Don't @ them."
        else:
            focus = self.memory.viewer_thread_key(line.user)
            who_line = f'{line.user} in chat: "{line.text}"\nThis is a viewer, not {name}.\n'
            ask = f"One short Twitch chat line starting with @{line.user}."
        messages = [
            {"role": "system", "content": self.chat_prompt},
            *self._history_messages(focus),
            {
                "role": "user",
                "content": (
                    f"{who_line}"
                    f"{prev_bit}"
                    f"{self.memory.prompt_block(focus, streamer_name=name, is_voice=False)}\n\n"
                    f"Live chat:\n{self._chat_context()}\n"
                    f"{extra}"
                    f"{ask}"
                ),
            },
        ]
        text = sanitize_reply(
            await self.llm.complete(messages, temperature=0.9 if retry else 0.8),
            str(self.persona.get("name") or ""),
        )
        if not text:
            return "ignore", ""
        return "speak", text[: self.settings.chat_max_chars]
