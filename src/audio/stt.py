from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from src.settings import Settings

log = logging.getLogger("cohost.stt")


def pick_whisper_device(pref: str) -> tuple[str, str]:
    pref = (pref or "auto").lower()
    if pref == "cpu":
        return "cpu", "int8"
    cuda_ok = False
    try:
        import ctranslate2

        cuda_ok = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        cuda_ok = False
    if pref == "cuda" and not cuda_ok:
        log.warning("CUDA requested for Whisper but not available; using CPU")
        return "cpu", "int8"
    if pref in ("auto", "cuda") and cuda_ok:
        return "cuda", "float16"
    return "cpu", "int8"


class SpeechToText:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = None
        self.device = "cpu"
        self.compute_type = "int8"
        self.error: Optional[str] = None

    def load(self) -> None:
        from faster_whisper import WhisperModel

        device, compute = pick_whisper_device(self.settings.whisper_device)
        if device == "cuda":
            compute = self.settings.compute_type_cuda
        else:
            compute = self.settings.compute_type_cpu
        try:
            log.info("Loading Whisper %s on %s (%s)", self.settings.whisper_model, device, compute)
            self.model = WhisperModel(self.settings.whisper_model, device=device, compute_type=compute)
            self.device = device
            self.compute_type = compute
            self.error = None
        except Exception as e:
            if device != "cpu":
                log.warning("Whisper CUDA load failed (%s); retrying CPU", e)
                self.model = WhisperModel(
                    self.settings.whisper_model, device="cpu", compute_type=self.settings.compute_type_cpu
                )
                self.device = "cpu"
                self.compute_type = self.settings.compute_type_cpu
                self.error = None
            else:
                self.error = str(e)
                raise

    def transcribe(self, audio: np.ndarray) -> str:
        if self.model is None:
            return ""
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 1.0:
            audio = audio / peak
        segments, _info = self.model.transcribe(
            audio,
            language=self.settings.whisper_language or None,
            vad_filter=True,
            beam_size=1,
            without_timestamps=True,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text
