from pathlib import Path

from src.settings import update_config


def test_update_config_keeps_comments_and_backup(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# booth header\n"
        "audio:\n"
        "  # silence comment stays on its own line\n"
        "  silence_ms: 800\n"
        "  energy_threshold: 0.012\n"
        "\n"
        "chat:\n"
        "  cooldown_seconds: 12\n",
        encoding="utf-8",
    )
    update_config(
        {
            "audio.silence_ms": 900,
            "chat.brand_new": "kept",
            "fresh.flag": False,
            "skipme": "no dot",
        },
        path=cfg,
    )
    text = cfg.read_text(encoding="utf-8")
    assert "# booth header" in text
    assert "# silence comment stays on its own line" in text
    assert "silence_ms: 900" in text
    assert "energy_threshold: 0.012" in text
    assert "brand_new: kept" in text
    assert "fresh:" in text
    assert "flag: false" in text
    assert "skipme" not in text
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8").startswith("# booth header")


def test_update_config_preserves_crlf(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_bytes(b"audio:\r\n  silence_ms: 800\r\n")
    update_config({"audio.silence_ms": 700}, path=cfg)
    raw = cfg.read_bytes()
    assert b"\r\n" in raw
    assert b"silence_ms: 700" in raw
