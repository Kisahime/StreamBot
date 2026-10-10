from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _package_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def test_runtime_pins_exclude_dev_tools():
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    packages = _package_lines(runtime)
    assert packages
    for line in packages:
        assert "==" in line
        assert ">=" not in line
        name = line.split("==", 1)[0].split("[", 1)[0].lower()
        assert name not in {"pytest", "ruff"}
    assert "fastapi==0.141.1" in runtime
    assert "nvidia-cublas-cu12==12.9.2.10" in runtime
    assert "-r requirements.txt" in dev
    assert "pytest==9.1.1" in dev
    assert "ruff==0.16.10" in dev
    assert not (ROOT / "requirements.lock.txt").exists()
