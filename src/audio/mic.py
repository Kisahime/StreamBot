from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

import numpy as np
import sounddevice as sd

from src.audio.vad import has_speech_silero, rms
from src.audio.wake import WAKE_FRAME, WakeWord
from src.settings import Settings

log = logging.getLogger("cohost.mic")

UtteranceHandler = Callable[[np.ndarray], Awaitable[None]]
SpeakingProbe = Callable[[], bool]
BargeHandler = Callable[[], None]
QUEUE_MAX = 200
ECHO_TAIL_SECONDS = 0.3


class MicListener:
    def __init__(
        self,
        settings: Settings,
        on_utterance: UtteranceHandler,
        *,
        speaking: SpeakingProbe | None = None,
        on_barge: BargeHandler | None = None,
    ) -> None:
        self.settings = settings
        self.on_utterance = on_utterance
        self._speaking = speaking or (lambda: False)
        self.on_barge = on_barge
        self.listen_enabled = False
        self.ptt_down = False
        self._stream: sd.InputStream | None = None
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._task: asyncio.Task | None = None
        self._utterance_task: asyncio.Task | None = None
        self._wake_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.level = 0.0
        self.wake = WakeWord(settings)
        self.wake_score = 0.0
        self._wake_carry = np.zeros(0, dtype=np.float32)
        self._wake_backlog: list[np.ndarray] = []
        self._wake_latch = False
        self._spoke_at = 0.0
        self._barge_sent = False
        self._wake_used = False

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
                    self._loop.call_soon_threadsafe(self._enqueue, chunk)
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

    def _enqueue(self, chunk: np.ndarray) -> None:
        queue = self._queue
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(chunk)
        except asyncio.QueueFull:
            pass

    async def stop(self) -> None:
        for task in (self._utterance_task, self._wake_task, self._task):
            if task and not task.done():
                task.cancel()
        for task in (self._utterance_task, self._wake_task, self._task):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._utterance_task = None
        self._wake_task = None
        self._task = None
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def set_listen(self, enabled: bool) -> None:
        self.listen_enabled = enabled

    def set_ptt(self, down: bool) -> None:
        self.ptt_down = down

    def _echo_allows(self, level: float) -> bool:
        if not self.settings.echo_gate:
            return True
        now = time.monotonic()
        speaking = bool(self._speaking())
        if speaking:
            self._spoke_at = now
        gated = speaking or (now - self._spoke_at) < ECHO_TAIL_SECONDS
        if not gated:
            return True
        if self.ptt_down:
            return True
        return level >= self.settings.energy_threshold * self.settings.echo_gate_ratio

    def _request_barge(self) -> None:
        if self._barge_sent or not self.settings.barge_in:
            return
        if not self._speaking():
            return
        self._barge_sent = True
        if self.on_barge is not None:
            self.on_barge()

    def _take_wake_frames(self, chunk: np.ndarray) -> None:
        piece = np.asarray(chunk, dtype=np.float32)
        audio = np.concatenate((self._wake_carry, piece)) if self._wake_carry.size else piece
        count = (int(audio.size) // WAKE_FRAME) * WAKE_FRAME
        if count:
            self._wake_backlog.append(np.ascontiguousarray(audio[:count]))
        self._wake_carry = np.ascontiguousarray(audio[count:])

    def _pump_wake(self, *, force: bool = False) -> None:
        if self._wake_latch or not self._wake_backlog:
            return
        if not force and self._wake_task is not None and not self._wake_task.done():
            return
        batch = self._wake_backlog
        self._wake_backlog = []
        audio = np.concatenate(batch)
        self._wake_task = asyncio.create_task(self._score_wake(audio), name="wake-score")

    async def _score_wake(self, audio: np.ndarray) -> None:
        try:
            score, heard = await asyncio.to_thread(self.wake.evaluate, audio)
        except Exception:
            log.exception("wake score failed")
            return
        self.wake_score = score
        if heard:
            self._wake_latch = True
            return
        self._pump_wake(force=True)

    def _schedule_utterance(self, audio: np.ndarray) -> None:
        previous = self._utterance_task
        if previous is not None and not previous.done():
            previous.cancel()
        self._utterance_task = asyncio.create_task(self._deliver(audio), name="utterance")

    async def _deliver(self, audio: np.ndarray) -> None:
        try:
            await self.on_utterance(audio)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("utterance handler failed")

    async def _run(self) -> None:
        sr = self.settings.sample_rate
        capturing = False
        buf: deque[np.ndarray] = deque()
        captured = 0
        silent = 0
        ptt_was_down = False

        while True:
            chunk = await self._queue.get()
            self.level = rms(chunk)
            silence_needed = int(sr * (self.settings.silence_ms / 1000.0))
            min_len = int(sr * (self.settings.min_utterance_ms / 1000.0))
            max_len = int(sr * self.settings.max_utterance_seconds)
            wake_hit = False
            if self._wake_latch and not self._wake_used:
                self._wake_used = True
                wake_hit = True
            armed = self.listen_enabled or self.ptt_down or self._wake_latch or wake_hit
            if not armed and self.wake.model is not None:
                self._take_wake_frames(chunk)
                self._pump_wake()

            if not armed:
                if capturing and captured >= min_len:
                    await self._flush(buf, captured, sr)
                capturing = False
                buf.clear()
                captured = 0
                silent = 0
                ptt_was_down = self.ptt_down
                continue

            raw_energy = self.level >= self.settings.energy_threshold
            energy_hit = raw_energy and self._echo_allows(self.level)
            if not capturing:
                if self.ptt_down or energy_hit or wake_hit:
                    self._request_barge()
                    capturing = True
                    buf.clear()
                    buf.append(chunk)
                    captured = len(chunk)
                    silent = 0
                ptt_was_down = self.ptt_down
                continue

            buf.append(chunk)
            captured += len(chunk)
            if energy_hit or (self.ptt_down and raw_energy):
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
                self._wake_latch = False
                self._wake_used = False
                self._barge_sent = False
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
        try:
            speech = await asyncio.to_thread(has_speech_silero, audio, sr)
        except Exception:
            log.exception("silero check failed")
            speech = False
        if not speech and rms(audio) < self.settings.energy_threshold:
            return
        self._schedule_utterance(audio)
