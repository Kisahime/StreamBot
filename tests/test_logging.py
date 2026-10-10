import asyncio
import logging

from src.logsetup import setup_logging
from tests.test_orchestrator_policy import make_orch


def test_debug_modules_open_only_those_loggers():
    setup_logging(level="INFO", debug_modules=["cohost.mic"])
    mic = logging.getLogger("cohost.mic")
    tts = logging.getLogger("cohost.tts")
    assert mic.isEnabledFor(logging.DEBUG)
    assert not tts.isEnabledFor(logging.DEBUG)
    seen: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(f"{record.name}:{record.getMessage()}")

    handler = _Grab()
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        mic.debug("mic-only-line")
        tts.debug("tts-should-stay-quiet")
    finally:
        root.removeHandler(handler)
    assert seen == ["cohost.mic:mic-only-line"]


def test_debug_toggle_reverts_after_ten_minutes(tmp_path, settings, monkeypatch):
    setup_logging(level="INFO")
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fast(seconds):
        delays.append(float(seconds))
        if float(seconds) < 600:
            await real_sleep(0)

    monkeypatch.setattr("src.orchestrator.asyncio.sleep", fast)

    async def run():
        orch = make_orch(tmp_path, settings)
        await orch.enable_debug_log()
        assert logging.getLogger("cohost").isEnabledFor(logging.DEBUG)
        await real_sleep(0)
        assert not logging.getLogger("cohost").isEnabledFor(logging.DEBUG)

    asyncio.run(run())
    assert 600 in delays
