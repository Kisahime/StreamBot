from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_start_bat_fails_loudly_and_stamps_only_after_pip():
    text = (ROOT / "start.bat").read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n").decode("utf-8")
    assert "py -3.11" in text
    assert "version_info >= (3, 11)" in text
    assert "python" in text
    assert "--no-browser" in text
    assert "pause" in text
    assert "pip install --upgrade pip" not in text
    install_at = text.index("pip install -r requirements.txt")
    error_at = text.index("if errorlevel 1", install_at)
    stamp_at = text.index("requirements.stamp').write_text", install_at)
    assert install_at < error_at < stamp_at
    assert "Failed to create the virtualenv." in text
    assert "Could not read server.host and server.port" in text


def test_shipped_config_keeps_optional_feeds_off():
    from src.settings import load_settings

    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    assert settings.bits_enabled is False
    assert settings.channel_points_enabled is False
    assert settings.hype_train_enabled is False
    assert settings.polls_enabled is False
    assert settings.predictions_enabled is False
    assert settings.allow_links is False
    assert "!so" in settings.mod_commands
    assert settings.shoutout_cooldown_seconds == 120
