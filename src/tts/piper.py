from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import wave
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

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
        self._proc: subprocess.Popen | None = None
        self._json_input = True

    def ensure_files(self) -> None:
        if sys.platform != "win32":
            self.ready = False
            self.error = "Piper setup is Windows-only (piper_windows_amd64). TTS will stay off on this OS."
            log.error(self.error)
            return
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

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    def _spawn(self) -> subprocess.Popen | None:
        if not self.exe or not self.model:
            return None
        creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
        cmd = [
            str(self.exe),
            "--model",
            str(self.model),
            "--json-input",
            "--sentence_silence",
            "0.12",
            "--length_scale",
            str(self.settings.tts_length_scale),
        ]
        try:
            return subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.exe.parent),
                creationflags=creation,
            )
        except Exception as e:
            log.warning("long-lived Piper failed to start: %s", e)
            return None

    def _proc_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._json_input

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
        text = (text or "").strip()[: self.settings.max_sentence_chars]
        if not text:
            return np.zeros(0, dtype=np.float32), self.sample_rate
        if self._json_input:
            audio = self._synth_persistent(text)
            if audio is not None:
                return audio
        return self._synth_oneshot(text)

    def _synth_persistent(self, text: str) -> tuple[np.ndarray, int] | None:
        if not self._proc_alive():
            self._proc = self._spawn()
        proc = self._proc
        if proc is None or proc.stdin is None:
            self._json_input = False
            return None
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out_path = tmp.name
        try:
            payload = json.dumps({"text": text, "output_file": out_path}) + "\n"
            proc.stdin.write(payload.encode("utf-8"))
            proc.stdin.flush()
        except Exception as e:
            log.warning("Piper stdin failed (%s); falling back to one-shot", e)
            self._json_input = False
            self.close()
            return None
        deadline = time.time() + 25
        while time.time() < deadline:
            if self._cancel.is_set():
                return np.zeros(0, dtype=np.float32), self.sample_rate
            if proc.poll() is not None:
                err = ""
                if proc.stderr:
                    err = proc.stderr.read().decode("utf-8", errors="replace")[:300]
                log.warning("Piper json-input exited (%s); using one-shot", err or proc.returncode)
                self._json_input = False
                self._proc = None
                return None
            if os.path.exists(out_path) and os.path.getsize(out_path) > 44:
                time.sleep(0.05)
                if os.path.getsize(out_path) > 44:
                    try:
                        return self._read_wav(out_path)
                    finally:
                        try:
                            os.unlink(out_path)
                        except OSError:
                            pass
            time.sleep(0.02)
        log.warning("Piper json-input timed out; using one-shot")
        self._json_input = False
        self.close()
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return None

    def _synth_oneshot(self, text: str) -> tuple[np.ndarray, int]:
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
            return self._read_wav(out_path)
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    def _read_wav(self, out_path: str) -> tuple[np.ndarray, int]:
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

    def _play(self, audio: np.ndarray, sr: int) -> None:
        if self._cancel.is_set():
            return
        sd.play(audio, sr, device=self.settings.speaker_device, blocking=True)
        sd.wait()
