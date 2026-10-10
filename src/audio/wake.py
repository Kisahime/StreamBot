from __future__ import annotations

import logging

import numpy as np

from src.settings import Settings

log = logging.getLogger("cohost.wake")


class WakeWord:
    """Local wake word, off by default, never the only way to talk.

    Hold-to-talk and the listen toggle keep working. If audio.wake_word is
    turned on and openwakeword is installed, a detected wake arms one utterance.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = None
        self.reason = "off"
        if not settings.wake_word:
            return
        try:
            from openwakeword.model import Model  # type: ignore

            self.model = Model()
            self.reason = "ready"
            log.info("Wake word ready (openwakeword). Push-to-talk still works.")
        except Exception as e:
            self.reason = f"openwakeword is not installed ({e}); wake word stays off"
            log.warning(self.reason)

    def heard(self, audio: np.ndarray) -> bool:
        if self.model is None or audio.size < 160:
            return False
        try:
            scores = self.model.predict(audio)
        except Exception:
            return False
        if not isinstance(scores, dict):
            return False
        return any(float(v) >= 0.5 for v in scores.values())

    def status(self) -> dict[str, object]:
        return {
            "enabled": bool(self.settings.wake_word),
            "ready": self.model is not None,
            "reason": self.reason,
        }
