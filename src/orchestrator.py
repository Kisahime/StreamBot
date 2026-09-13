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
from src.llm.ollama import OllamaClient
from src.llm.persona import build_system_prompt, load_persona
from src.memory import ConversationMemory, ensure_chat_mention, for_speech, is_generic_filler, sanitize_reply
from src.settings import ROOT, Settings
from src.tts.piper import PiperTTS
from src.twitch.chat import ChatLine, TwitchChat

log = logging.getLogger("cohost.orch")


class Orchestrator:
    def __init__(self, settings: Settings, hub: Hub) -> None:
        self.settings = settings
        self.hub = hub
        self.llm = OllamaClient(settings)
        self.stt = SpeechToText(settings)
        self.tts = PiperTTS(settings)
        self.mic = MicListener(settings, self.on_utterance)
        self.twitch: Optional[TwitchChat] = None
        self.persona = load_persona()
        self.system_prompt = build_system_prompt(self.persona)
        self.memory = ConversationMemory(
            ROOT / "data" / "memory.json",
            event_limit=settings.memory_events,
            viewer_limit=settings.memory_viewers,
        )
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
        self.tts_mute = settings.tts_mute
        self._want_twitch = False

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        await self.hub.patch_status(
            phase="starting",
            tts_mute=self.tts_mute,
            persona=str(self.persona.get("name") or "Cohost"),
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
        if self.twitch:
            await self.twitch.disconnect()
        self.tts.interrupt()

    def clear_memory(self) -> None:
        self.memory.clear()
        self.spoken.clear()
        self._user_reply_at.clear()
        asyncio.create_task(self.hub.broadcast({"type": "log", "data": "Memory cleared"}))
        asyncio.create_task(self.hub.broadcast({"type": "memory", "data": self.memory.snapshot()}))

    def reload_persona(self) -> None:
        self.persona = load_persona()
        self.system_prompt = build_system_prompt(self.persona)
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
        if self.twitch:
            await self.twitch.disconnect()
        self._want_twitch = True
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
        )
        await self.twitch.connect()

    async def twitch_disconnect(self) -> None:
        self._want_twitch = False
        if self.twitch:
            await self.twitch.disconnect()
            self.twitch = None

    async def _twitch_watchdog(self) -> None:
        while True:
            try:
                await asyncio.sleep(20)
                if not self.settings.auto_reconnect_twitch or not self._want_twitch:
                    continue
                if self.twitch and self.twitch.alive():
                    continue
                await self.hub.broadcast({"type": "log", "data": "Twitch dropped — reconnecting"})
                try:
                    await self.twitch_connect()
                except Exception as e:
                    await self.hub.broadcast({"type": "log", "data": f"Twitch reconnect failed: {e}"})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("twitch watchdog failed")

    async def _twitch_status(self, state: str) -> None:
        await self.hub.patch_status(twitch=state)

    async def on_chat(self, line: ChatLine) -> None:
        self._chat_pending.append(line)
        self.memory.remember("viewer", line.user, line.text)
        await self.hub.broadcast({"type": "chat", "data": {"user": line.user, "text": line.text, "mention": line.mention}})
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
            await self.hub.patch_status(last_user=text)
            await self.hub.broadcast({"type": "transcript", "data": {"role": "streamer", "text": text}})
            self.memory.remember("streamer", "Streamer", text)
            await self._voice_reply(text, gen)
        finally:
            self._busy_voice = False
            if self._chat_pending:
                asyncio.create_task(self._consider_chat(force=False))

    def _chat_context(self) -> str:
        if not self.twitch:
            live = "(no chat connected)"
        else:
            lines = self.twitch.recent(self.settings.chat_context_lines)
            live = "\n".join(l.render() for l in lines) if lines else "(chat is quiet)"
        return live

    def _history_messages(self) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = []
        for role, text in self.spoken:
            msgs.append({"role": role, "content": text})
        return msgs

    async def _voice_reply(self, user_text: str, gen: int) -> None:
        messages = [
            {"role": "system", "content": self.system_prompt},
            *self._history_messages(),
            {
                "role": "user",
                "content": (
                    "[mode=voice]\n"
                    f"Streamer said: {user_text}\n\n"
                    f"{self.memory.prompt_block()}\n\n"
                    f"Recent Twitch chat:\n{self._chat_context()}\n\n"
                    "Reply as the cohost, out loud, to the streamer. You may mention chatters by name if relevant."
                ),
            },
        ]
        full: list[str] = []
        async for sentence in self.llm.stream_sentences(messages):
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
        self.spoken.append(("user", user_text))
        self.spoken.append(("assistant", reply))
        self.memory.remember("cohost_voice", str(self.persona.get("name") or "Pixel"), reply)
        await self.hub.broadcast({"type": "transcript", "data": {"role": "cohost", "text": reply}})
        if self.settings.post_voice_replies_to_chat and self.twitch and self.twitch.connected:
            short = reply[: self.settings.chat_max_chars]
            try:
                await self.twitch.send(short)
                self.memory.remember("cohost_chat", str(self.persona.get("name") or "Pixel"), short)
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
        spoken = for_speech(chat_text)
        self._mark_reply(line.user)
        self.memory.remember("cohost_chat", str(self.persona.get("name") or "Pixel"), chat_text)
        self.memory.note_reply_to(line.user, chat_text)
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
            extra = (
                "\nIMPORTANT: Do not greet them again. Do not say assist/help. "
                "Answer their latest line like a stream cohost, not a receptionist.\n"
            )
        prev = self.memory.viewer(line.user)
        prev_bit = ""
        if prev and prev.last_reply:
            prev_bit = f"\nYou already said to them: {prev.last_reply}\nDo not repeat that.\n"
        messages = [
            {"role": "system", "content": self.system_prompt},
            *self._history_messages(),
            {
                "role": "user",
                "content": (
                    "[mode=chat]\n"
                    f"{line.user} just typed: {line.text}\n"
                    f"{prev_bit}"
                    f"{self.memory.prompt_block(focus_user=line.user)}\n\n"
                    f"Recent chat:\n{self._chat_context()}\n"
                    f"{extra}"
                    f"Write one short in-character chat line starting with @{line.user}. "
                    "No JSON. No action/reply labels."
                ),
            },
        ]
        text = sanitize_reply(await self.llm.complete(messages, temperature=0.85 if retry else 0.75), str(self.persona.get("name") or ""))
        if not text:
            return "ignore", ""
        return "speak", text[: self.settings.chat_max_chars]
