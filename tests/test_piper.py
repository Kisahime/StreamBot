import hashlib
from pathlib import Path

import pytest

from src.tts.piper import PIPER_ZIP_SHA256, VOICE_SHA256, _download, _ensure_pinned_file, piper_voice_urls


def test_piper_voice_urls_and_pins():
    onnx, meta = piper_voice_urls("en_US-amy-medium")
    assert onnx.endswith("/en/en_US/amy/medium/en_US-amy-medium.onnx")
    assert meta.endswith("/en/en_US/amy/medium/en_US-amy-medium.onnx.json")
    assert len(PIPER_ZIP_SHA256) == 64
    assert "en_US-amy-medium.onnx" in VOICE_SHA256


class _Body:
    def __init__(self, data: bytes):
        self._data = data
        self.headers = {"Content-Length": str(len(data))}

    def read(self, size: int) -> bytes:
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_download_checks_the_pin_before_replacing(tmp_path: Path, monkeypatch):
    body = b"voice-bytes"
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr("src.tts.piper.urlopen", lambda *_a, **_k: _Body(body))
    dest = tmp_path / "voice.onnx"
    _download("http://example.test/voice.onnx", dest, expected_sha256=digest)
    assert dest.read_bytes() == body
    assert not (tmp_path / "voice.onnx.part").exists()

    monkeypatch.setattr("src.tts.piper.urlopen", lambda *_a, **_k: _Body(b"tampered"))
    bad = tmp_path / "bad.onnx"
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        _download("http://example.test/bad.onnx", bad, expected_sha256=digest)
    assert not bad.exists()
    assert not (tmp_path / "bad.onnx.part").exists()


def test_existing_file_with_the_wrong_checksum_is_left_alone(tmp_path: Path):
    dest = tmp_path / "en_US-amy-medium.onnx"
    dest.write_bytes(b"already here")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        _ensure_pinned_file("http://example.test/voice.onnx", dest, "a" * 64)
    assert dest.read_bytes() == b"already here"
