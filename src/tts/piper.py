from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
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
PIPER_ZIP_SHA256 = "f3c58906402b24f3a96d92145f58acba6d86c9b5db896d207f78dc80811efcea"
VOICE_SHA256 = {
    "en_US-amy-medium.onnx": "b3a6e47b57b8c7fbe6a0ce2518161a50f59a9cdd8a50835c02cb02bdd6206c18",
    "en_US-amy-medium.onnx.json": "95a23eb4d42909d38df73bb9ac7f45f597dbfcde2d1bf9526fdeaf5466977d77",
    "en_US-kristin-medium.onnx": "5849957f929cbf720c258f8458692d6103fff2f0e3d3b19c8259474bb06a18d4",
    "en_US-kristin-medium.onnx.json": "5681426d4aead22195de70531eeeeddb46493cfaffc5764b2ea3db73428b651c",
    "en_US-hfc_female-medium.onnx": "914c473788fc1fa8b63ace1cdcdb44588f4ae523d3ab37df1536616835a140b7",
    "en_US-hfc_female-medium.onnx.json": "03f1fa0622b80463283592d97aca9f6e89aec345a5c56b7257723e0093c58b6c",
}


def piper_voice_urls(voice: str) -> tuple[str, str]:
    """Map 'en_US-amy-medium' to Hugging Face onnx + json URLs."""
    voice = voice.strip()
    lang_region, rest = voice.split("-", 1)
    name, quality = rest.rsplit("-", 1)
    lang = lang_region.split("_", 1)[0]
    base = f"{VOICE_REPO}/{lang}/{lang_region}/{name}/{quality}/{voice}"
    return f"{base}.onnx", f"{base}.onnx.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_to(url: str, part: Path, on_progress) -> None:
    req = Request(url, headers={"User-Agent": "local-twitch-cohost"})
    with urlopen(req, timeout=120) as resp, part.open("wb") as handle:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        next_mark = 10
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            handle.write(chunk)
            got += len(chunk)
            if on_progress and total:
                pct = int(got * 100 / total)
                if pct >= next_mark:
                    on_progress(f"Downloading {part.name.removesuffix('.part')} {pct}%")
                    next_mark = pct + 10


def _download(url: str, dest: Path, *, expected_sha256: str | None = None, on_progress=None) -> None:
    """Download to ``dest.name.part`` and replace ``dest`` only after the file is complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            log.info("Downloading %s -> %s", url, dest)
            _stream_to(url, part, on_progress)
            if expected_sha256 and _sha256(part) != expected_sha256.lower():
                part.unlink(missing_ok=True)
                raise RuntimeError("checksum mismatch")
            os.replace(part, dest)
            return
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = exc
            log.warning("download attempt %s failed: %s", attempt + 1, exc)
            time.sleep(0.25 * (attempt + 1))
    raise RuntimeError(f"download failed: {last_error}")


def _ensure_pinned_file(url: str, dest: Path, expected_sha256: str | None, on_progress=None) -> None:
    """Leave a matching file in place. A different file on disk is a checksum mismatch."""
    if dest.exists():
        if expected_sha256 and _sha256(dest) != expected_sha256.lower():
            raise RuntimeError("checksum mismatch")
        return
    _download(url, dest, expected_sha256=expected_sha256, on_progress=on_progress)


def _safe_extract(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            parts = [part for part in name.split("/") if part not in ("", ".")]
            if name.startswith("/") or ".." in parts or Path(name).is_absolute():
                raise RuntimeError(f"unsafe zip entry: {info.filename}")
            target = (root / name).resolve()
            if os.path.commonpath([str(root), str(target)]) != str(root):
                raise RuntimeError(f"unsafe zip entry: {info.filename}")
        for info in archive.infolist():
            archive.extract(info, dest)


class PiperTTS:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.piper_dir = settings.piper_dir
        self.exe: Path | None = None
        self.model: Path | None = None
        self.sample_rate = 22050
        self._play_lock = asyncio.Lock()
        self._gen = 0
        self._output = None
        self.ready = False
        self.error: str | None = None
        self.speaking = False
        self._proc: subprocess.Popen | None = None
        self._json_input = True
        self._json_retry_at = 0.0
        self._json_retry_after = 60.0
        self._persistent_timeout = 25.0
        self._stdout_q: queue.Queue[str] = queue.Queue()
        self._reader_gen = 0
        self._pending_deletes: set[str] = set()
        self.on_progress = None

    @property
    def mode(self) -> str:
        return "persistent" if self._json_input else "oneshot"

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
                _ensure_pinned_file(PIPER_ZIP, zip_path, PIPER_ZIP_SHA256, self.on_progress)
                _safe_extract(zip_path, self.piper_dir)
                if not exe.exists():
                    for p in self.piper_dir.rglob("piper.exe"):
                        exe = p
                        break
        self.exe = exe
        voice = self.settings.tts_voice
        voice_onnx = self.piper_dir / f"{voice}.onnx"
        voice_json = self.piper_dir / f"{voice}.onnx.json"
        onnx_url, json_url = piper_voice_urls(voice)
        _ensure_pinned_file(onnx_url, voice_onnx, VOICE_SHA256.get(voice_onnx.name), self.on_progress)
        _ensure_pinned_file(json_url, voice_json, VOICE_SHA256.get(voice_json.name), self.on_progress)
        self.model = voice_onnx
        if self.exe and self.exe.exists():
            self.ready = True
            self.error = None
        else:
            self.ready = False
            self.error = "piper.exe not found after download"

    def interrupt(self) -> None:
        self._gen += 1
        self._stop_output()

    def _stop_output(self) -> None:
        stream = self._output
        self._output = None
        if stream is not None:
            try:
                abort = getattr(stream, "abort", None)
                if abort:
                    abort()
                else:
                    stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        try:
            sd.stop()
        except Exception:
            pass

    def close(self) -> None:
        self._sweep_pending()
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

    def _start_readers(self, proc: subprocess.Popen) -> None:
        """Drain stdout and stderr so a full pipe cannot stall Piper."""
        self._reader_gen += 1
        gen = self._reader_gen
        lines = self._stdout_q

        def _read_stdout() -> None:
            stream = proc.stdout
            if stream is None:
                return
            try:
                while gen == self._reader_gen:
                    raw = stream.readline()
                    if not raw:
                        break
                    lines.put(raw.decode("utf-8", errors="replace"))
            except Exception:
                log.debug("piper stdout reader stopped", exc_info=True)

        def _read_stderr() -> None:
            stream = proc.stderr
            if stream is None:
                return
            try:
                while gen == self._reader_gen:
                    raw = stream.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        log.debug("piper: %s", line[:500])
            except Exception:
                log.debug("piper stderr reader stopped", exc_info=True)

        for target in (_read_stdout, _read_stderr):
            threading.Thread(target=target, name="piper-pipe", daemon=True).start()

    def _fail_persistent(self, proc: subprocess.Popen | None, reason: str) -> None:
        log.warning("%s; retrying persistent Piper in %.0fs", reason, self._json_retry_after)
        self._json_input = False
        self._json_retry_at = time.monotonic() + self._json_retry_after
        if proc is not None and proc is self._proc:
            self.close()
        elif proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def _maybe_retry_persistent(self) -> None:
        if self._json_input or not self._json_retry_at:
            return
        if time.monotonic() >= self._json_retry_at:
            log.info("Retrying persistent Piper")
            self._json_input = True

    def _delete_output(self, out_path: str) -> None:
        try:
            os.unlink(out_path)
        except OSError:
            return
        self._pending_deletes.discard(out_path)

    def _abandon_output(self, out_path: str) -> None:
        """Remember a wav the interrupted request may still write.

        The placeholder file is removed now, but the path stays pending: Piper
        can create it again after the interrupt, and the next synth or the
        ready line deletes that copy.
        """
        if not out_path:
            return
        self._pending_deletes.add(out_path)
        try:
            os.unlink(out_path)
        except OSError:
            pass

    def _sweep_pending(self) -> None:
        for path in list(self._pending_deletes):
            if os.path.exists(path):
                self._delete_output(path)

    def _claim_ready_line(self, line: str) -> None:
        for path in list(self._pending_deletes):
            if not self._output_ready(line, path):
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
            self._pending_deletes.discard(path)

    def _output_ready(self, line: str, out_path: str) -> bool:
        text = (line or "").strip()
        if not text:
            return False
        if out_path in text:
            return True
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False
        if not isinstance(data, dict):
            return False
        reported = str(data.get("output_file") or "")
        return bool(reported) and (reported == out_path or out_path in reported)

    async def speak(self, text: str) -> None:
        text = (text or "").strip()
        if not text or not self.settings.tts_enabled or self.settings.tts_mute:
            return
        if not self.ready or not self.exe or not self.model:
            log.warning("TTS not ready: %s", self.error)
            return
        gen = self._gen
        async with self._play_lock:
            if gen != self._gen:
                return
            self.speaking = True
            try:
                audio, sr = await asyncio.to_thread(self._synth, text, gen)
                if gen != self._gen or audio.size == 0:
                    return
                await asyncio.to_thread(self._play, audio, sr, gen)
            finally:
                self.speaking = False

    def _synth(self, text: str, gen: int | None = None) -> tuple[np.ndarray, int]:
        if gen is None:
            gen = self._gen
        text = (text or "").strip()[: self.settings.max_sentence_chars]
        if not text:
            return np.zeros(0, dtype=np.float32), self.sample_rate
        self._maybe_retry_persistent()
        if self._json_input:
            audio = self._synth_persistent(text, gen)
            if audio is not None:
                return audio
        return self._synth_oneshot(text)

    def _synth_persistent(self, text: str, gen: int) -> tuple[np.ndarray, int] | None:
        if not self._proc_alive():
            self._stdout_q = queue.Queue()
            self._proc = self._spawn()
            if self._proc is not None:
                self._start_readers(self._proc)
        self._sweep_pending()
        proc = self._proc
        if proc is None or proc.stdin is None:
            self._fail_persistent(proc, "Piper json-input did not start")
            return None
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out_path = tmp.name
        try:
            payload = json.dumps({"text": text, "output_file": out_path}) + "\n"
            proc.stdin.write(payload.encode("utf-8"))
            proc.stdin.flush()
        except Exception as e:
            log.warning("Piper stdin failed (%s); falling back to one-shot", e)
            self._fail_persistent(proc, "Piper stdin failed")
            self._abandon_output(out_path)
            return None
        deadline = time.monotonic() + self._persistent_timeout
        while time.monotonic() < deadline:
            if gen != self._gen:
                self._abandon_output(out_path)
                return np.zeros(0, dtype=np.float32), self.sample_rate
            if proc.poll() is not None:
                self._proc = None
                self._fail_persistent(None, f"Piper json-input exited ({proc.returncode})")
                self._abandon_output(out_path)
                return None
            try:
                line = self._stdout_q.get(timeout=0.05)
            except queue.Empty:
                continue
            self._claim_ready_line(line)
            if not self._output_ready(line, out_path):
                continue
            try:
                return self._read_wav(out_path)
            finally:
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
        self._fail_persistent(proc, "Piper json-input timed out")
        self._abandon_output(out_path)
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

    def _play(self, audio: np.ndarray, sr: int, gen: int | None = None) -> None:
        if gen is None:
            gen = self._gen
        if gen != self._gen or audio.size == 0:
            return
        stream = sd.OutputStream(
            samplerate=sr,
            channels=1,
            dtype="float32",
            device=self.settings.speaker_device,
        )
        self._output = stream
        try:
            stream.start()
            frame = max(1, int(sr * 0.05))
            pos = 0
            while pos < audio.size:
                if gen != self._gen:
                    break
                stream.write(audio[pos : pos + frame])
                pos += frame
        finally:
            if self._output is stream:
                self._stop_output()
