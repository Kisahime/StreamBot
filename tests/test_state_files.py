import json
import logging

import pytest

from src.booth import BoothState, load_booth, save_booth
from src.identity import StreamerIdentity, load_identity, save_identity


def test_replace_failure_keeps_the_old_booth_file(tmp_path, monkeypatch):
    path = tmp_path / "booth.json"
    save_booth(path, BoothState(mood="chill", profile="Focus game"))
    before = path.read_text(encoding="utf-8")

    def explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("src.settings.os.replace", explode)
    with pytest.raises(OSError):
        save_booth(path, BoothState(mood="hype"))
    assert path.read_text(encoding="utf-8") == before
    assert load_booth(path).mood == "chill"


def test_corrupt_booth_and_identity_are_logged_and_copied(tmp_path, caplog):
    booth = tmp_path / "booth.json"
    booth.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        loaded = load_booth(booth, fallback_mute=True)
    assert loaded.mood == "normal"
    assert loaded.tts_mute is True
    assert (tmp_path / "booth.json.bad").read_text(encoding="utf-8") == "{not json"
    assert "booth.json" in caplog.text

    ident = tmp_path / "streamer.json"
    ident.write_text("{still bad", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        who = load_identity(ident, default_login="luna")
    assert who.twitch_login == "luna"
    assert (tmp_path / "streamer.json.bad").exists()
    assert "streamer.json" in caplog.text
    save_identity(ident, StreamerIdentity(display_name="Luna", twitch_login="luna"))
    assert json.loads(ident.read_text(encoding="utf-8"))["display_name"] == "Luna"
