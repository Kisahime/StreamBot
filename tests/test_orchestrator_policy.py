import asyncio
import time
from pathlib import Path

from src.hub import Hub
from src.orchestrator import Orchestrator
from src.twitch.chat import ChatLine

PERSONA_FIXTURE = Path(__file__).parent / "fixtures" / "persona.yaml"


class FakeLLM:
    def __init__(self, text="hey there", bump=None):
        self.text = text
        self.bump = bump
        self.last_error = None

    async def complete(self, messages, temperature=0.7, cancel=None):
        return self.text

    async def stream_sentences(self, messages, temperature=0.7, cancel=None, max_chars=280):
        if self.bump:
            self.bump()
        yield self.text

    async def health(self):
        return {"ok": True, "has_model": True, "models": ["qwen2.5:3b"]}

    async def aclose(self):
        return None


class FakeTTS:
    def __init__(self):
        self.spoken = []
        self.speaking = False
        self.ready = True
        self.error = None

    def interrupt(self):
        self.speaking = False

    async def speak(self, text):
        self.spoken.append(text)

    def close(self):
        return None


class FakeSTT:
    def __init__(self):
        self.model = None
        self.device = "cpu"
        self.compute_type = "int8"
        self.error = None

    def load(self):
        return None

    def transcribe(self, audio):
        return ""


class FakeMic:
    def __init__(self):
        self.listen_enabled = False
        self.ptt_down = False
        self.level = 0.0

    def start(self, loop):
        return None

    async def stop(self):
        return None

    def set_listen(self, enabled: bool) -> None:
        self.listen_enabled = enabled

    def set_ptt(self, down: bool) -> None:
        self.ptt_down = down


class FakeChat:
    def __init__(self):
        self.connected = True
        self.sent = []

    async def send(self, text):
        self.sent.append(text)

    def recent(self, n):
        return []


def make_orch(tmp_path, settings):
    orch = Orchestrator(
        settings,
        Hub(),
        data_dir=tmp_path,
        llm=FakeLLM(),
        tts=FakeTTS(),
        stt=FakeSTT(),
        mic=FakeMic(),
        persona_path=PERSONA_FIXTURE,
    )
    orch.twitch = FakeChat()
    return orch


def _repo_files(root: Path) -> set[Path]:
    skip = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    found: set[Path] = set()

    def walk(directory: Path) -> None:
        for path in directory.iterdir():
            if path.name in skip:
                continue
            if path.is_dir():
                walk(path)
            elif path.is_file():
                found.add(path.relative_to(root))

    walk(root)
    return found


def test_injected_fakes_skip_hardware_and_use_fixture_persona(tmp_path, settings, sounddevice_stub):
    before = sounddevice_stub.InputStream.started
    root = Path(__file__).resolve().parents[1]
    files_before = _repo_files(root)
    orch = make_orch(tmp_path, settings)
    assert isinstance(orch.llm, FakeLLM)
    assert isinstance(orch.tts, FakeTTS)
    assert isinstance(orch.stt, FakeSTT)
    assert isinstance(orch.mic, FakeMic)
    assert orch.data_dir == tmp_path
    assert orch.persona.get("relationship") == "You sit next to the streamer."
    assert "receptionist" not in orch.voice_prompt
    assert orch.settings.twitch_token == "oauth:fake-token-not-real"
    assert sounddevice_stub.InputStream.started == before
    assert _repo_files(root) == files_before


def test_ambient_is_typed_and_engaging_is_spoken(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        await orch._decide_chat(ChatLine(user="bob", text="nice play"), "chat")
        assert orch.tts.spoken == []
        assert orch.twitch.sent and orch.twitch.sent[0].startswith("@bob")

        orch.llm = FakeLLM("oh hey")
        orch.tts = FakeTTS()
        orch.twitch = FakeChat()
        await orch._decide_chat(ChatLine(user="sam", text="hey", mention=True), "speak")
        assert orch.tts.spoken
        assert orch.twitch.sent

    asyncio.run(run())


def test_per_user_cooldown(tmp_path, settings):
    orch = make_orch(tmp_path, settings)
    orch._mark_reply("bob")
    ok, why = orch._rate_ok(mention=False, user="bob")
    assert not ok
    assert why == "per-user cooldown"


def test_streamer_is_not_mentioned(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        orch.identity.twitch_login = "luna"
        orch.llm = FakeLLM("hello there")
        await orch._decide_chat(ChatLine(user="luna", text="you there?"), "speak")
        assert orch.twitch.sent
        assert not orch.twitch.sent[0].lower().startswith("@luna")

    asyncio.run(run())


def test_follow_batch_is_one_event(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        orch._follow_names = ["Ann", "Bea", "Cy"]
        await orch._emit_follows()
        assert len(orch._event_pending) == 1
        event = orch._event_pending[0]
        assert event.kind == "follow"
        assert event.people() == ["Ann", "Bea", "Cy"]

    asyncio.run(run())


def test_discarded_voice_generation_does_not_post(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        gen = orch._voice_gen

        def bump():
            orch._voice_gen += 1

        orch.llm = FakeLLM("should not send", bump=bump)
        orch.twitch = FakeChat()
        orch.tts = FakeTTS()
        await orch._voice_reply("hello", gen, time.perf_counter())
        assert orch.twitch.sent == []
        assert orch.tts.spoken == []

    asyncio.run(run())


def test_interrupt_bumps_generation(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        before = orch._voice_gen
        await orch.interrupt()
        assert orch._voice_gen == before + 1
        assert orch._voice_cancel.is_set() is False or orch._voice_gen > before

    asyncio.run(run())
