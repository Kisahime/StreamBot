from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from src.settings import ROOT

_OAUTH = re.compile(r"(?i)oauth:[^\s'\"]+")
_BEARER = re.compile(r"(?i)bearer\s+\S+")


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) > 8]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for secret in self.secrets:
            msg = msg.replace(secret, "***")
        msg = _OAUTH.sub("oauth:***", msg)
        msg = _BEARER.sub("Bearer ***", msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logging(
    secrets: list[str] | None = None,
    *,
    level: str = "INFO",
    debug_modules: list[str] | None = None,
) -> None:
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    redactor = RedactFilter(secrets)
    file_handler = RotatingFileHandler(
        log_dir / "cohost.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    stream = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler.setFormatter(fmt)
    stream.setFormatter(fmt)
    file_handler.addFilter(redactor)
    stream.addFilter(redactor)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))
    root.addHandler(stream)
    root.addHandler(file_handler)
    for name in debug_modules or []:
        logging.getLogger(str(name)).setLevel(logging.DEBUG)
