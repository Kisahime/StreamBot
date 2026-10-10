from __future__ import annotations

import logging

import numpy as np

from src.settings import Settings

log = logging.getLogger("cohost.wake")

WAKE_FRAME = 1280


def to_wake_frames(audio: np.ndarray) -> list[np.ndarray]:
    """1280-sample int16 frames for openwakeword. Short tails are dropped."""
    pcm = np.clip(np.asarray(audio, dtype=np.float32) * 32767.0, -32768, 32767).astype(np.int16)
    frames: list[np.ndarray] = []
    for start in range(0, int(pcm.size) - WAKE_FRAME + 1, WAKE_FRAME):
        frames.append(np.ascontiguousarray(pcm[start : start + WAKE_FRAME]))
    return frames


class WakeWord:
    """Local wake word, off by default, never the only way to talk.

    Hold-to-talk and the listen toggle keep working. If audio.wake_word is
    turned on and openwakeword is installed, a detected wake arms one utterance.
    Scoring runs on 1,280-sample int16 frames and only reads configured models.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = None
        self.reason = "off"
        self.score = 0.0
        self.models = [str(name) for name in (settings.wake_models or ["hey_jarvis"]) if str(name).strip()]
        self.threshold = float(settings.wake_threshold)
        if not settings.wake_word:
            return
        try:
            from openwakeword.model import Model  # type: ignore

            self.model = Model(wakeword_models=self.models or ["hey_jarvis"])
            self.reason = "ready"
            log.info("Wake word ready (%s). Push-to-talk still works.", ", ".join(self.models))
        except Exception as e:
            self.reason = f"openwakeword is not installed ({e}); wake word stays off"
            log.warning(self.reason)

    def _wanted(self, key: str) -> bool:
        name = str(key)
        if not self.models:
            return True
        return any(name == model or name.startswith(f"{model}_") or model in name for model in self.models)

    def evaluate(self, audio: np.ndarray) -> tuple[float, bool]:
        """Return (best configured score, heard). Safe to call off the event loop."""
        if self.model is None:
            self.score = 0.0
            return 0.0, False
        best = 0.0
        frames = to_wake_frames(audio)
        if not frames and audio.size >= 160:
            # A short buffer still gets one padded frame so a phrase can arm.
            pcm = np.clip(np.asarray(audio, dtype=np.float32) * 32767.0, -32768, 32767).astype(np.int16)
            frame = np.zeros(WAKE_FRAME, dtype=np.int16)
            frame[: min(WAKE_FRAME, pcm.size)] = pcm[:WAKE_FRAME]
            frames = [frame]
        for frame in frames:
            try:
                scores = self.model.predict(frame)
            except Exception:
                continue
            if not isinstance(scores, dict):
                continue
            for key, value in scores.items():
                if not self._wanted(str(key)):
                    continue
                best = max(best, float(value))
        self.score = best
        return best, best >= self.threshold

    def heard(self, audio: np.ndarray) -> bool:
        _score, heard = self.evaluate(audio)
        return heard

    def status(self) -> dict[str, object]:
        return {
            "enabled": bool(self.settings.wake_word),
            "ready": self.model is not None,
            "reason": self.reason,
            "score": round(float(self.score), 4),
            "models": list(self.models),
            "threshold": self.threshold,
        }
