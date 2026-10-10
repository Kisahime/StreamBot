import sys

import pytest

from src.audio.duck import Duck


class _Sessions:
    def __init__(self):
        self.levels = {
            "Spotify.exe:8": 0.8,
            "chrome:4": 0.6,
            "obs64.exe:9": 0.7,
            "Discord.exe:3": 0.5,
        }

    def snapshot(self):
        return dict(self.levels)

    def set_volume(self, key, level):
        self.levels[key] = level


def test_skip_list_and_allow_list(settings, tmp_path):
    sessions = _Sessions()
    settings.speaker_device = None
    settings.duck_gain = 0.25
    ducked = Duck(settings, backend=sessions, data_dir=tmp_path)
    ducked.set_enabled(True)
    ducked.down()
    assert sessions.levels["Spotify.exe:8"] == pytest.approx(0.2)
    assert sessions.levels["chrome:4"] == pytest.approx(0.15)
    assert sessions.levels["obs64.exe:9"] == pytest.approx(0.7)
    assert sessions.levels["Discord.exe:3"] == pytest.approx(0.5)
    ducked.up()

    sessions.levels = {
        "Spotify.exe:8": 0.8,
        "chrome:4": 0.6,
        "obs64.exe:9": 0.7,
    }
    settings.duck_apps = ["Spotify.exe"]
    listed = Duck(settings, backend=sessions, data_dir=tmp_path)
    listed.set_enabled(True)
    listed.down()
    assert sessions.levels["Spotify.exe:8"] == pytest.approx(0.2)
    assert sessions.levels["chrome:4"] == pytest.approx(0.6)
    assert sessions.levels["obs64.exe:9"] == pytest.approx(0.7)


def test_non_windows_without_a_backend_stays_on_obs_sidechain(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    ducked = Duck(settings, data_dir=tmp_path)
    ducked.set_enabled(True)
    assert ducked.mode == "obs-sidechain"
    ducked.down()
    assert ducked.mode == "obs-sidechain"
