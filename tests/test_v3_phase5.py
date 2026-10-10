import asyncio
import importlib.util
import re
from pathlib import Path

from src.health import extra_status, snapshot
from tests.test_orchestrator_policy import make_orch

ROOT = Path(__file__).resolve().parents[1]


def _config_keys() -> list[str]:
    text = (ROOT / "src" / "settings.py").read_text(encoding="utf-8")
    found = re.findall(r'(?:_get|_command_list)\(\s*data\s*,\s*"([^"]+)"', text, re.S)
    return list(dict.fromkeys(found))


def test_every_config_key_is_documented():
    docs = "\n".join(
        (ROOT / name).read_text(encoding="utf-8") for name in ("README.md", "config.yaml")
    )
    missing = [key for key in _config_keys() if key not in docs]
    assert missing == []
    assert _config_keys()


def test_readme_covers_phase5_topics():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "endpoint ducking" not in readme.lower()
    assert "not Windows endpoint" in readme
    assert "audio.duck_apps" in readme
    assert "audio.wake_threshold" in readme
    assert "clips:edit" in readme
    assert "[::1]" in readme
    assert "chat.custom_commands" in readme
    assert "COHOST_CONTROL_KEY" in readme
    assert "YYYY-MM-DD-HHMM.md" in readme
    assert "!so` with no name shouts out the last raider" in readme
    assert "Owner checklist" in readme


def test_health_reports_optional_extras(tmp_path, settings, monkeypatch):
    assert extra_status() == {"pycaw": "not installed", "openwakeword": "not installed"}

    def find_spec(name):
        return object() if name == "pycaw" else None

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert extra_status() == {"pycaw": "installed", "openwakeword": "not installed"}

    async def run():
        orch = make_orch(tmp_path, settings)

        class _Wake:
            def status(self):
                return {"enabled": False}

        orch.mic.wake = _Wake()
        health = await snapshot(settings, orch)
        assert health["extras"]["pycaw"] == "installed"
        assert health["extras"]["openwakeword"] == "not installed"

    asyncio.run(run())


def test_changelog_lists_merged_prs_and_matches_version():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.2.0"' in pyproject
    assert "0.2.0" in changelog
    assert "v0.2.0" in changelog
    for number in range(1, 12):
        assert f"#{number} " in changelog
