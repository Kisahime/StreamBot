from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from src.settings import atomic_write_text

log = logging.getLogger("cohost.identity")


@dataclass
class StreamerIdentity:
    display_name: str = ""
    twitch_login: str = ""

    def login_key(self) -> str:
        return (self.twitch_login or "").strip().lstrip("#").lower()

    def spoken_name(self) -> str:
        return (self.display_name or self.twitch_login or "Streamer").strip() or "Streamer"

    def is_login(self, name: str) -> bool:
        key = self.login_key()
        if not key or not name:
            return False
        return name.strip().lstrip("@").lower() == key


def _preserve_bad(path: Path) -> None:
    bad = path.with_name(path.name + ".bad")
    try:
        shutil.copy2(path, bad)
    except Exception:
        log.exception("could not copy corrupt %s", path.name)
    log.error("%s is unreadable; kept a copy at %s", path.name, bad.name)


def load_identity(path: Path, default_login: str = "") -> StreamerIdentity:
    ident = StreamerIdentity(twitch_login=(default_login or "").strip().lstrip("#"))
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ident.display_name = str(data.get("display_name") or ident.display_name).strip()
            login = str(data.get("twitch_login") or ident.twitch_login).strip().lstrip("#")
            ident.twitch_login = login
        except Exception:
            log.exception("streamer.json is unreadable")
            _preserve_bad(path)
    if not ident.twitch_login and default_login:
        ident.twitch_login = default_login.strip().lstrip("#")
    return ident


def save_identity(path: Path, ident: StreamerIdentity) -> None:
    body = json.dumps(asdict(ident), ensure_ascii=False, indent=2)
    atomic_write_text(path, body + "\n")
