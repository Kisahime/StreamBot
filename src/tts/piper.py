from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import wave
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request

import numpy as np
import sounddevice as sd

from src.settings import Settings

log = logging.getLogger("cohost.tts")

PIPER_ZIP = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
VOICE_REPO = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def piper_voice_urls(voice: str) -> tuple[str, str]:
    """Map 'en_US-amy-medium' to Hugging Face onnx + json URLs."""
    voice = voice.strip()
    lang_region, rest = voice.split("-", 1)
    name, quality = rest.rsplit("-", 1)
    lang = lang_region.split("_", 1)[0]
    base = f"{VOICE_REPO}/{lang}/{lang_region}/{name}/{quality}/{voice}"
    return f"{base}.onnx", f"{base}.onnx.json"


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s -> %s", url, dest)
    req = Request(url, headers={"User-Agent": "local-twitch-cohost"})
    with urlopen(req, timeout=120) as resp, dest.open("wb") as f:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)


class PiperTTS:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.piper_dir = settings.piper_dir
        self.exe: Path | None = None
        self.model: Path | None = None
        self.sample_rate = 22050
        self._play_lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self.ready = False
        self.error: str | None = None
        self.speaking = False

    def ensure_files(self) -> None:
        self.piper_dir.mkdir(parents=True, exist_ok=True)
        exe = self.piper_dir / "piper" / "piper.exe"
        if not exe.exists():
            # unpacked zip may put piper.exe at piper_dir/piper.exe
            alt = self.piper_dir / "piper.exe"
            if alt.exists():
                exe = alt
            else:
                zip_path = self.piper_dir / "piper_windows_amd64.zip"
                if not zip_path.exists():
                    _download(PIPER_ZIP, zip_path)
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(self.piper_dir)
                if not exe.exists():
                    for p in self.piper_dir.rglob("piper.exe"):
                        exe = p
                        break
        self.exe = exe
        voice = self.settings.tts_voice
        voice_onnx = self.piper_dir / f"{voice}.onnx"
        voice_json = self.piper_dir / f"{voice}.onnx.json"
        onnx_url, json_url = piper_voice_urls(voice)
        if not voice_onnx.exists():
            _download(onnx_url, voice_onnx)
        if not voice_json.exists():
            _download(json_url, voice_json)
        self.model = voice_onnx
        if self.exe and self.exe.exists():
            self.ready = True
            self.error = None
        else:
            self.ready = False
            self.error = "piper.exe not found after download"

    def interrupt(self) -> None:
        self._cancel.set()
        try:
            sd.stop()
        except Exception:
            pass

    async def speak(self, text: str) -> None:
        text = (text or "").strip()
        if not text or not self.settings.tts_enabled or self.settings.tts_mute:
            return
        if not self.ready or not self.exe or not self.model:
            log.warning("TTS not ready: %s", self.error)
            return
        self._cancel.clear()
        async with self._play_lock:
            if self._cancel.is_set():
                return
            self.speaking = True
            try:
                audio, sr = await asyncio.to_thread(self._synth, text)
                if self._cancel.is_set() or audio.size == 0:
                    return
                await asyncio.to_thread(self._play, audio, sr)
            finally:
                self.speaking = False

    def _synth(self, text: str) -> tuple[np.ndarray, int]:
        assert self.exe and self.model
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out_path = tmp.name
        try:
            creation = 0
            if sys.platform == "win32":
                creation = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            proc = subprocess.run(
                [
                    str(self.exe),
                    "--model",
                    str(self.model),
                    "--output_file",
                    out_path,
                    "--sentence_silence",
                    "0.12",
                    "--length_scale",
                    str(self.settings.tts_length_scale),
                ],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=60,
                creationflags=creation,
                cwd=str(self.exe.parent),
            )
            if proc.returncode != 0:
                log.error("piper failed: %s", proc.stderr.decode("utf-8", errors="replace"))
                return np.zeros(0, dtype=np.float32), self.sample_rate
            with wave.open(out_path, "rb") as wf:
                sr = wf.getframerate()
                n = wf.getnframes()
                raw = wf.readframes(n)
                ch = wf.getnchannels()
                sw = wf.getsampwidth()
            if sw == 2:
                audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                audio = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 255.0 - 0.5
            if ch > 1:
                audio = audio.reshape(-1, ch).mean(axis=1)
            self.sample_rate = sr
            return audio, sr
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    def _play(self, audio: np.ndarray, sr: int) -> None:
        if self._cancel.is_set():
            return
        sd.play(audio, sr, device=self.settings.speaker_device, blocking=True)
        sd.wait()
