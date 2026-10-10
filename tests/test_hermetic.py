from pathlib import Path

from src.main import _booth_port, create_app
from tests.test_orchestrator_policy import make_orch


def test_broken_root_config_still_builds_a_booth(tmp_path, settings, monkeypatch):
    import src.main as main

    repo = Path(__file__).resolve().parents[1]
    target = repo / "config.yaml"
    original = target.read_bytes()
    try:
        target.write_text(":\n  [broken\n", encoding="utf-8")
        monkeypatch.setattr(main, "ROOT", repo)
        assert _booth_port(None) == 8080
        orch = make_orch(tmp_path, settings)
        app = create_app(prepared=(settings, orch.hub, orch))
        assert app is not None
    finally:
        target.write_bytes(original)
