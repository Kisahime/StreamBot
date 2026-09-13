from __future__ import annotations

import numpy as np

_AVAILABLE = False

try:
    from faster_whisper.vad import get_speech_timestamps, VadOptions

    _AVAILABLE = True
except Exception:
    get_speech_timestamps = None  # type: ignore
    VadOptions = None  # type: ignore


def rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame.astype(np.float32)))))


def has_speech_silero(audio: np.ndarray, sample_rate: int) -> bool:
    """True if Silero VAD (bundled with faster-whisper) finds speech."""
    if not _AVAILABLE or audio.size < sample_rate * 0.1:
        return False
    try:
        opts = VadOptions(min_silence_duration_ms=200, speech_pad_ms=80)
        ts = get_speech_timestamps(audio.astype(np.float32), vad_options=opts, sampling_rate=sample_rate)
        return bool(ts)
    except Exception:
        return False


def trim_to_speech(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    if not _AVAILABLE or audio.size == 0:
        return audio
    try:
        opts = VadOptions(min_silence_duration_ms=200, speech_pad_ms=120)
        ts = get_speech_timestamps(audio.astype(np.float32), vad_options=opts, sampling_rate=sample_rate)
        if not ts:
            return audio
        first, last = ts[0], ts[-1]
        start = first["start"] if isinstance(first, dict) else first[0]
        end = last["end"] if isinstance(last, dict) else last[1]
        return audio[start:end]
    except Exception:
        return audio
