from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Callable, Awaitable, Optional

import numpy as np
import sounddevice as sd

from src.audio.vad import rms, has_speech_silero
from src.settings import Settings

log = logging.getLogger("cohost.mic")

UtteranceHandler = Callable[[np.ndarray], Awaitable[None]]


class MicListener:
    def __init__(self, settings: Settings, on_utterance: UtteranceHandler) -> None:
        self.settings = settings
        self.on_utterance = on_utterance
        self.listen_enabled = False
        self.ptt_down = False
        self._stream: Optional[sd.InputStream] = None
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.level = 0.0

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        sr = self.settings.sample_rate
        device = self.settings.mic_device

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            if status:
                log.debug("mic status: %s", status)
            chunk = np.copy(indata[:, 0]).astype(np.float32)
            if self._loop:
                try:
                    self._loop.call_soon_threadsafe(self._queue.put_nowait, chunk)
                except RuntimeError:
                    pass

        self._stream = sd.InputStream(
            samplerate=sr,
            channels=1,
            dtype="float32",
            device=device,
            blocksize=int(sr * 0.03),
            callback=callback,
        )
        self._stream.start()
        self._task = loop.create_task(self._run(), name="mic-listener")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def set_listen(self, enabled: bool) -> None:
        self.listen_enabled = enabled

    def set_ptt(self, down: bool) -> None:
        self.ptt_down = down

    async def _run(self) -> None:
        sr = self.settings.sample_rate
        silence_needed = int(sr * (self.settings.silence_ms / 1000.0))
        min_len = int(sr * (self.settings.min_utterance_ms / 1000.0))
        max_len = int(sr * self.settings.max_utterance_seconds)
        capturing = False
        buf: deque[np.ndarray] = deque()
        captured = 0
        silent = 0
        ptt_was_down = False

        while True:
            chunk = await self._queue.get()
            self.level = rms(chunk)
            armed = self.listen_enabled or self.ptt_down

            if not armed:
                if capturing and captured >= min_len:
                    await self._flush(buf, captured, sr)
                capturing = False
                buf.clear()
                captured = 0
                silent = 0
                ptt_was_down = self.ptt_down
                continue

            energy_hit = self.level >= self.settings.energy_threshold
            if not capturing:
                if self.ptt_down or energy_hit:
                    capturing = True
                    buf.clear()
                    buf.append(chunk)
                    captured = len(chunk)
                    silent = 0
                ptt_was_down = self.ptt_down
                continue

            buf.append(chunk)
            captured += len(chunk)
            if energy_hit:
                silent = 0
            else:
                silent += len(chunk)

            ptt_released = ptt_was_down and not self.ptt_down
            ptt_was_down = self.ptt_down

            ended = False
            if self.ptt_down:
                ended = captured >= max_len
            elif ptt_released:
                ended = True
            else:
                ended = silent >= silence_needed or captured >= max_len

            if ended:
                await self._flush(buf, captured, sr)
                capturing = False
                buf.clear()
                captured = 0
                silent = 0

    async def _flush(self, buf: deque[np.ndarray], captured: int, sr: int) -> None:
        min_len = int(sr * (self.settings.min_utterance_ms / 1000.0))
        if captured < min_len:
            return
        audio = np.concatenate(list(buf)) if buf else np.zeros(0, dtype=np.float32)
        if audio.size < min_len:
            return
        if not has_speech_silero(audio, sr) and rms(audio) < self.settings.energy_threshold:
            return
        try:
            await self.on_utterance(audio)
        except Exception:
            log.exception("utterance handler failed")
