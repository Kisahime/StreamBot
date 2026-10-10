from __future__ import annotations

import logging
import sys

from src.settings import Settings

log = logging.getLogger("cohost.duck")

OBS_NOTE = (
    "Ducking is off in-app. In OBS, put music on its own track and sidechain a compressor "
    "to the cohost output (a VB-Audio cable works). audio.duck_enabled stays false unless you opt in."
)


class Duck:
    """Optional local volume duck. Off by default.

    If pycaw is installed and ducking is enabled, the default output endpoint is
    lowered while Pixel speaks and restored after. Otherwise the booth reports
    the OBS sidechain note and does not touch other apps.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mode = "off"
        self.note = OBS_NOTE
        self._saved: float | None = None
        self._volume = None
        self._warned = False
        if settings.duck_enabled:
            self._try_bind()

    def _try_bind(self) -> None:
        if sys.platform != "win32":
            self.mode = "obs-sidechain"
            self._warn_once()
            return
        try:
            from pycaw.pycaw import AudioUtilities  # type: ignore

            device = AudioUtilities.GetSpeakers()
            self._volume = device.EndpointVolume
            self.mode = "endpoint"
        except Exception:
            self.mode = "obs-sidechain"
            self._warn_once()

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
            self._volume = None
            return
        self._try_bind()

    def on_speaking(self, speaking: bool) -> None:
        if speaking:
            self.down()
        else:
            self.up()

    def down(self) -> None:
        if not self.settings.duck_enabled or self.mode != "endpoint" or self._volume is None:
            if self.settings.duck_enabled:
                self._warn_once()
            return
        try:
            if self._saved is None:
                self._saved = float(self._volume.GetMasterVolumeLevelScalar())
            target = max(0.0, min(1.0, self._saved * self.settings.duck_gain))
            self._volume.SetMasterVolumeLevelScalar(target, None)
        except Exception:
            log.exception("duck down failed")
            self.mode = "obs-sidechain"

    def up(self) -> None:
        if self._volume is None or self._saved is None:
            return
        try:
            self._volume.SetMasterVolumeLevelScalar(self._saved, None)
        except Exception:
            log.exception("duck restore failed")
        self._saved = None

    def status(self) -> dict[str, str]:
        return {"mode": self.mode, "note": self.note if self.mode == "obs-sidechain" else ""}
