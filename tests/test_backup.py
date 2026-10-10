import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backup.ps1"


def _pwsh() -> str:
    found = shutil.which("pwsh")
    if not found:
        pytest.skip("pwsh is not installed")
    return found


def test_backup_script_skips_env_and_keeps_ten_zips(tmp_path: Path):
    booth = tmp_path / "booth"
    (booth / "data").mkdir(parents=True)
    (booth / "data" / "memory.json").write_text('{"people": []}\n', encoding="utf-8")
    (booth / "data" / ".env").write_text("SECRET=inside-data\n", encoding="utf-8")
    (booth / "persona.yaml").write_text("name: Pixel\n", encoding="utf-8")
    (booth / "config.yaml").write_text("server:\n  host: 127.0.0.1\n", encoding="utf-8")
    (booth / ".env").write_text("TWITCH_TOKEN=oauth:do-not-zip\n", encoding="utf-8")
    backups = booth / "backups"
    backups.mkdir()
    for index in range(12):
        path = backups / f"20000101-{index:04d}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("old.txt", "x")
        stamp = 1_000_000 + index
        os.utime(path, (stamp, stamp))

    subprocess.run(
        [_pwsh(), "-NoProfile", "-File", str(SCRIPT), "-Root", str(booth)],
        check=True,
    )
    zips = list(backups.glob("*.zip"))
    assert len(zips) == 10
    newest = max(zips, key=lambda path: path.stat().st_mtime)
    with zipfile.ZipFile(newest) as archive:
        names = archive.namelist()
    assert any(name.endswith("persona.yaml") for name in names)
    assert any(name.endswith("config.yaml") for name in names)
    assert any(name.endswith("memory.json") for name in names)
    assert not any(name == ".env" or name.endswith("/.env") or name.endswith("\\.env") for name in names)
    joined = "\n".join(names)
    assert "oauth:do-not-zip" not in joined
    assert "SECRET" not in joined
