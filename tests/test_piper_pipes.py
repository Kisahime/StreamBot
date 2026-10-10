import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

STUB = """\
import json
import sys
import wave

flood = "--flood" in sys.argv
while True:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    if flood:
        for _ in range(80):
            sys.stderr.buffer.write(b"e" * 8000 + b"\\n")
            sys.stderr.buffer.flush()
        flood = False
    req = json.loads(line.decode())
    path = req["output_file"]
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        wf.writeframes(b"\\x00\\x10" * 80)
    sys.stdout.buffer.write((path + "\\n").encode())
    sys.stdout.buffer.flush()
"""


def _write_stub(directory: Path) -> Path:
    path = directory / "piper_stub.py"
    path.write_text(STUB, encoding="utf-8")
    return path


def _ready(settings, directory: Path):
    from src.tts.piper import PiperTTS

    tts = PiperTTS(settings)
    tts.exe = directory / "piper.exe"
    tts.model = directory / "voice.onnx"
    tts.exe.write_bytes(b"")
    tts.model.write_bytes(b"")
    tts.ready = True
    tts.settings.tts_enabled = True
    tts.settings.tts_mute = False
    tts._play = lambda audio, sr: None
    return tts


def _popen(script: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(script), *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_pipe_readers_avoid_deadlock(tmp_path, settings):
    script = _write_stub(tmp_path)
    tts = _ready(settings, tmp_path)
    tts._persistent_timeout = 3
    tts._spawn = lambda: _popen(script, "--flood")
    result: dict = {}

    def run() -> None:
        audio, _sr = tts._synth("hello there")
        result["size"] = int(audio.size)
        result["mode"] = tts.mode

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(8)
    tts.close()
    assert thread.is_alive() is False
    assert result["mode"] == "persistent"
    assert result["size"] > 0


def test_two_hundred_speaks_stay_persistent(tmp_path, settings):
    script = _write_stub(tmp_path)
    tts = _ready(settings, tmp_path)
    tts._persistent_timeout = 3
    tts._spawn = lambda: _popen(script)

    def _no_oneshot(text: str):
        raise AssertionError("fell back to one-shot")

    tts._synth_oneshot = _no_oneshot

    async def soak() -> None:
        for _ in range(200):
            started = time.perf_counter()
            await tts.speak("hello there")
            assert time.perf_counter() - started < 1.0
            assert tts.mode == "persistent"

    try:
        asyncio.run(soak())
    finally:
        tts.close()
    assert tts.mode == "persistent"


def test_persistent_mode_retries_after_the_delay(tmp_path, settings):
    script = _write_stub(tmp_path)
    tts = _ready(settings, tmp_path)
    tts._persistent_timeout = 2
    calls = {"n": 0}

    def spawn():
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.Popen(
                [sys.executable, "-c", "raise SystemExit(3)"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        return _popen(script)

    tts._spawn = spawn
    tts._synth_oneshot = lambda text: (np.zeros(0, dtype=np.float32), 22050)

    try:
        audio, _sr = tts._synth("hi")
        assert int(audio.size) == 0
        assert tts.mode == "oneshot"
        tts._json_retry_at = time.monotonic() - 1
        audio, _sr = tts._synth("hi again")
        assert tts.mode == "persistent"
        assert int(audio.size) > 0
    finally:
        tts.close()
