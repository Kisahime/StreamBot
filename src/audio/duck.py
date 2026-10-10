from __future__ import annotations

import atexit
import json
import logging
import os
import sys
from pathlib import Path

from src.settings import Settings

log = logging.getLogger("cohost.duck")

OBS_NOTE = (
    "Ducking is off in-app. In OBS, put music on its own track and sidechain a compressor "
    "to the cohost output (a VB-Audio cable works). audio.duck_enabled stays false unless you opt in."
)
REFUSE_NOTE = (
    "Ducking is off: speaker_device is null or is the default output, so Pixel's own voice would be ducked. "
    "Set speaker_device to a different device, or sidechain music in OBS."
)


class _PycawSessions:
    """Other apps' per-session volumes. Pixel's own process is left alone."""

    def __init__(self) -> None:
        self._volumes: dict[str, object] = {}

    def snapshot(self) -> dict[str, float]:
        from pycaw.pycaw import AudioUtilities  # type: ignore

        levels: dict[str, float] = {}
        self._volumes = {}
        for session in AudioUtilities.GetAllSessions():
            proc = getattr(session, "Process", None)
            volume = getattr(session, "SimpleAudioVolume", None)
            if proc is None or volume is None or getattr(proc, "pid", None) == os.getpid():
                continue
            try:
                name = str(proc.name())
            except Exception:
                name = "app"
            key = f"{name}:{proc.pid}"
            levels[key] = float(volume.GetMasterVolume())
            self._volumes[key] = volume
        return levels

    def set_volume(self, key: str, level: float) -> None:
        volume = self._volumes.get(key)
        if volume is None:
            return
        volume.SetMasterVolume(float(level), None)  # type: ignore[attr-defined]


class Duck:
    """Optional per-session volume duck. Off by default.

    Endpoint ducking is refused unless ``speaker_device`` is a different device
    than the default output, so Pixel's own voice is not lowered. Saved levels
    go to ``data/duck.json`` and are restored at startup and on exit.
    """

    def __init__(self, settings: Settings, *, backend=None, data_dir: Path | None = None) -> None:
        self.settings = settings
        self.mode = "off"
        self.note = OBS_NOTE
        self._backend = backend
        self._saved: dict[str, float] = {}
        self._warned = False
        root = data_dir or (settings.root / "data")
        self._path = Path(root) / "duck.json"
        self._restore_pending()
        atexit.register(self.restore_saved)
        if settings.duck_enabled:
            self._try_bind()

    def _default_output(self):
        try:
            import sounddevice as sd

            _mic, speaker = sd.default.device
            return speaker
        except Exception:
            return None

    def _speaker_is_separate(self) -> bool:
        speaker = self.settings.speaker_device
        if speaker is None or speaker == "":
            return False
        default = self._default_output()
        if default is None:
            return False
        try:
            return int(speaker) != int(default)
        except (TypeError, ValueError):
            return False

    def _try_bind(self) -> None:
        if not self._speaker_is_separate():
            self.mode = "obs-sidechain"
            self.note = REFUSE_NOTE
            self._warn_once()
            return
        if self._backend is None:
            if sys.platform != "win32":
                self.mode = "obs-sidechain"
                self.note = OBS_NOTE
                self._warn_once()
                return
            try:
                self._backend = _PycawSessions()
            except Exception:
                self.mode = "obs-sidechain"
                self.note = OBS_NOTE
                self._warn_once()
                return
        self.mode = "sessions"
        self.note = ""

    def _warn_once(self) -> None:
        if self._warned:
            return
        self._warned = True
        log.info(self.note)

    def set_enabled(self, enabled: bool) -> None:
        self.settings.duck_enabled = enabled
        if not enabled:
            self.up()
            self.mode = "off"
            self.note = OBS_NOTE
            return
        self._warned = False
        self._try_bind()

    def on_speaking(self, speaking: bool) -> None:
        if speaking:
            self.down()
        else:
            self.up()

    def _write_file(self, sessions: dict[str, float]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"sessions": sessions})
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._path)

    def _clear_file(self) -> None:
        try:
            self._path.unlink()
        except OSError:
            pass

    def _apply(self, sessions: dict[str, float]) -> None:
        if self._backend is None:
            return
        for key, level in sessions.items():
            try:
                self._backend.set_volume(key, float(level))
            except Exception:
                log.exception("duck session %s failed", key)

    def _restore_pending(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("duck.json is unreadable")
            return
        sessions = data.get("sessions") or {}
        if not isinstance(sessions, dict) or not sessions:
            self._clear_file()
            return
        if self._backend is None and sys.platform == "win32":
            try:
                self._backend = _PycawSessions()
            except Exception:
                log.info("Saved duck levels are in %s but pycaw is not available yet", self._path)
                return
        if self._backend is None:
            log.info("Saved duck levels are in %s; restore them on Windows with pycaw", self._path)
            return
        self._apply({str(key): float(value) for key, value in sessions.items()})
        self._saved = {}
        self._clear_file()

    def down(self) -> None:
        if not self.settings.duck_enabled or self.mode != "sessions" or self._backend is None:
            if self.settings.duck_enabled:
                self._warn_once()
            return
        if self._saved:
            return
        try:
            current = {str(key): float(value) for key, value in (self._backend.snapshot() or {}).items()}
        except Exception:
            log.exception("duck snapshot failed")
            self.mode = "obs-sidechain"
            self.note = OBS_NOTE
            return
        if not current:
            return
        self._saved = current
        self._write_file(current)
        gain = max(0.0, min(1.0, float(self.settings.duck_gain)))
        self._apply({key: max(0.0, min(1.0, level * gain)) for key, level in current.items()})

    def up(self) -> None:
        if not self._saved:
            self._restore_pending()
            return
        self._apply(self._saved)
        self._saved = {}
        self._clear_file()

    def restore_saved(self) -> None:
        """atexit hook. Puts the pre-duck levels back if the process is leaving mid-duck."""
        try:
            self.up()
        except Exception:
            log.exception("duck restore failed")

    def status(self) -> dict[str, str]:
        note = self.note if self.mode != "sessions" else ""
        return {"mode": self.mode, "note": note}
