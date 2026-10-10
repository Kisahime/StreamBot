"""Phase 3: mic loop, echo gate, wake frames, ducking, mentions, TTS generation, EventSub."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.audio.devices import match_device
from src.audio.duck import REFUSE_NOTE, Duck
from src.audio.mic import MicListener
from src.audio.stt import SpeechToText
from src.audio.wake import WAKE_FRAME, to_wake_frames
from src.health import clear_device_cache, load_devices, snapshot
from src.llm.ollama import OllamaClient, model_names_match
from src.llm.persona import build_voice_prompt
from src.main import create_app
from src.memory import STREAMER_THREAD, ConversationMemory, streamer_smalltalk_fallback
from src.tts.piper import PiperTTS
from src.twitch.chat import ChatLine, text_mentions
from src.twitch.eventsub import KEEPALIVE_GRACE, EventSubClient, next_backoff
from tests.test_orchestrator_policy import FakeLLM, FakeTTS, make_orch

ROOT = Path(__file__).resolve().parents[1]


def _loud(n: int, level: float = 0.2) -> np.ndarray:
    return np.full(n, level, dtype=np.float32)


def test_wake_frames_are_1280_int16():
    frames = to_wake_frames(np.ones(WAKE_FRAME, dtype=np.float32))
    assert len(frames) == 1
    assert frames[0].dtype == np.int16
    assert frames[0].shape == (WAKE_FRAME,)
    assert int(frames[0][0]) == 32767
    assert len(to_wake_frames(np.ones(WAKE_FRAME * 2, dtype=np.float32))) == 2
    assert to_wake_frames(np.ones(100, dtype=np.float32)) == []


def test_mic_handler_does_not_block_the_loop_and_barge_cancels(settings):
    settings.min_utterance_ms = 20
    settings.silence_ms = 20
    settings.energy_threshold = 0.01
    settings.echo_gate = False
    settings.max_utterance_seconds = 5
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(_audio):
        started.set()
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def run():
        mic = MicListener(settings, handler)
        mic.listen_enabled = True
        task = asyncio.create_task(mic._run())
        try:
            for _ in range(4):
                mic._enqueue(_loud(160))
            for _ in range(8):
                mic._enqueue(np.zeros(160, dtype=np.float32))
            await asyncio.wait_for(started.wait(), 1)
            mic._enqueue(_loud(160, 0.5))
            await asyncio.sleep(0.05)
            assert mic.level > 0.4
            for _ in range(4):
                mic._enqueue(_loud(160))
            for _ in range(8):
                mic._enqueue(np.zeros(160, dtype=np.float32))
            await asyncio.wait_for(cancelled.wait(), 1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_queue_drops_oldest_when_full(settings):
    mic = MicListener(settings, lambda _audio: asyncio.sleep(0))
    for i in range(250):
        mic._enqueue(np.full(4, i, dtype=np.float32))
    assert mic._queue.qsize() == 200
    first = mic._queue.get_nowait()
    assert float(first[0]) == 50


def test_echo_gate_ignores_speaker_bleed_unless_ptt_or_loud(settings):
    settings.energy_threshold = 0.012
    settings.echo_gate = True
    settings.echo_gate_ratio = 2.5
    settings.barge_in = True
    settings.min_utterance_ms = 10
    settings.silence_ms = 10
    barges: list[int] = []
    seen: list[int] = []

    async def handler(_audio):
        seen.append(1)

    async def run():
        speaking = {"on": True}
        mic = MicListener(
            settings,
            handler,
            speaking=lambda: speaking["on"],
            on_barge=lambda: barges.append(1),
        )
        mic.listen_enabled = True
        task = asyncio.create_task(mic._run())
        try:
            mic._enqueue(_loud(320, 0.02))
            await asyncio.sleep(0.05)
            assert barges == []
            assert seen == []
            mic.ptt_down = True
            mic._enqueue(_loud(320, 0.02))
            await asyncio.sleep(0.05)
            assert barges
            mic.ptt_down = False
            speaking["on"] = False
            mic._spoke_at = time.monotonic()
            mic._enqueue(_loud(320, 0.02))
            await asyncio.sleep(0.05)
            quiet_during_tail = len(seen)
            await asyncio.sleep(0.35)
            for _ in range(2):
                mic._enqueue(_loud(320, 0.02))
            mic._enqueue(np.zeros(320, dtype=np.float32))
            await asyncio.sleep(0.1)
            assert len(seen) > quiet_during_tail
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_wake_score_runs_off_the_loop_and_arms_one_utterance(settings):
    settings.min_utterance_ms = 10
    settings.silence_ms = 10
    settings.energy_threshold = 0.01
    settings.echo_gate = False
    calls: list[tuple] = []
    heard = asyncio.Event()

    class FakeModel:
        def predict(self, frame):
            calls.append((frame.dtype, frame.shape))
            time.sleep(0.15)
            return {"hey_jarvis": 0.9, "alexa": 0.99}

    async def handler(_audio):
        heard.set()

    async def run():
        mic = MicListener(settings, handler)
        mic.wake.model = FakeModel()
        mic.wake.models = ["hey_jarvis"]
        mic.wake.threshold = 0.5
        task = asyncio.create_task(mic._run())
        try:
            mic._enqueue(_loud(WAKE_FRAME, 0.2))
            await asyncio.sleep(0.02)
            assert mic.level > 0
            await asyncio.wait_for(mic._wake_task, 1)
            assert mic.wake.score == pytest.approx(0.9)
            assert mic._wake_latch
            assert calls and calls[0] == (np.dtype(np.int16), (WAKE_FRAME,))
            mic._enqueue(_loud(WAKE_FRAME, 0.2))
            mic._enqueue(np.zeros(WAKE_FRAME, dtype=np.float32))
            await asyncio.wait_for(heard.wait(), 1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


class _Sessions:
    def __init__(self):
        self.levels = {"chrome:4": 0.8}

    def snapshot(self):
        return dict(self.levels)

    def set_volume(self, key, level):
        self.levels[key] = level


def test_duck_refuses_default_output_and_restores_saved_volume(settings, tmp_path, monkeypatch):
    import sounddevice as sd

    sessions = _Sessions()
    monkeypatch.setattr(sd.default, "device", (0, 1), raising=False)
    settings.speaker_device = None
    ducked = Duck(settings, backend=sessions, data_dir=tmp_path)
    ducked.set_enabled(True)
    assert ducked.mode == "obs-sidechain"
    assert "speaker_device" in ducked.note
    assert ducked.note == REFUSE_NOTE
    ducked.down()
    assert sessions.levels["chrome:4"] == 0.8

    settings.speaker_device = 1
    same = Duck(settings, backend=sessions, data_dir=tmp_path)
    same.set_enabled(True)
    assert same.mode == "obs-sidechain"

    settings.speaker_device = 3
    settings.duck_gain = 0.25
    separate = Duck(settings, backend=sessions, data_dir=tmp_path)
    separate.set_enabled(True)
    assert separate.mode == "sessions"
    separate.down()
    assert sessions.levels["chrome:4"] == pytest.approx(0.2)
    saved = json.loads((tmp_path / "duck.json").read_text(encoding="utf-8"))
    assert saved["sessions"]["chrome:4"] == pytest.approx(0.8)
    separate.up()
    assert sessions.levels["chrome:4"] == pytest.approx(0.8)
    assert not (tmp_path / "duck.json").exists()

    sessions.levels["chrome:4"] = 0.2
    (tmp_path / "duck.json").write_text(json.dumps({"sessions": {"chrome:4": 0.8}}), encoding="utf-8")
    Duck(settings, backend=sessions, data_dir=tmp_path)
    assert sessions.levels["chrome:4"] == pytest.approx(0.8)
    assert not (tmp_path / "duck.json").exists()


def test_mentions_are_words_and_stale_lines_drop(tmp_path, settings):
    assert not text_mentions("pixelated", "pixel")
    assert text_mentions("@Pixel hi", "pixel")
    assert text_mentions("pixel?", "Pixel")
    assert not text_mentions("hey", "")

    async def run():
        orch = make_orch(tmp_path, settings)
        settings.chat_max_age_seconds = 45
        old = ChatLine(user="bob", text="hello there")
        old.ts = time.monotonic() - 50
        fresh = ChatLine(user="sam", text="still here")
        streamer = ChatLine(user="testchannel", text="old but mine")
        streamer.ts = time.monotonic() - 90
        command = ChatLine(user="bob", text="!so someone", command="!so")
        command.ts = time.monotonic() - 90
        orch._chat_pending.extend([old, fresh, streamer, command])
        await orch._expire_stale_chat()
        users = [line.user for line in orch._chat_pending]
        assert "bob" not in users or any(line.command == "!so" for line in orch._chat_pending)
        assert old not in list(orch._chat_pending)
        assert fresh in list(orch._chat_pending)
        assert streamer in list(orch._chat_pending)
        assert command in list(orch._chat_pending)
        assert orch._last_skip["reason"] == "stale"

    asyncio.run(run())


def test_tts_generation_drops_queued_speech_after_interrupt(settings):
    async def run():
        tts = PiperTTS(settings)
        tts.ready = True
        tts.exe = Path("piper")
        tts.model = Path("voice.onnx")
        tts.settings.tts_enabled = True
        tts.settings.tts_mute = False
        played: list[int] = []
        started = threading.Event()
        release = threading.Event()

        def synth(text, gen):
            return np.ones(8, dtype=np.float32), 16000

        def play(audio, sr, gen):
            played.append(gen)
            if len(played) == 1:
                started.set()
                release.wait(2)

        tts._synth = synth
        tts._play = play
        tasks = [asyncio.create_task(tts.speak(text)) for text in ("one", "two", "three")]
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0.05)
        tts.interrupt()
        release.set()
        await asyncio.gather(*tasks)
        assert played == [0]

    asyncio.run(run())


def _welcome(sid: str = "sid", keepalive: float = 10):
    return {
        "metadata": {"message_type": "session_welcome", "message_id": sid},
        "payload": {"session": {"id": sid, "keepalive_timeout_seconds": keepalive}},
    }


class _Socket:
    def __init__(self, messages, events, name):
        self.messages = list(messages)
        self.events = events
        self.name = name
        self.closed = False

    async def recv(self):
        if self.closed:
            raise RuntimeError("closed")
        if not self.messages:
            await asyncio.Future()
        item = self.messages.pop(0)
        if item == "HANG":
            await asyncio.Future()
        return json.dumps(item)

    async def close(self):
        self.closed = True
        self.events.append(f"close-{self.name}")


def test_eventsub_keepalive_reconnect_and_revocation(monkeypatch, tmp_path, settings):
    events: list[str] = []
    statuses: list[str] = []

    async def on_event(_event):
        return None

    async def on_status(state):
        statuses.append(state)

    async def run():
        monkeypatch.setattr("src.twitch.eventsub.KEEPALIVE_GRACE", 0.05)
        client = EventSubClient("oauth:tok", "cid", "chan", on_event, on_status)
        subscribed: list[str] = []

        async def subscribe(sid):
            subscribed.append(sid)
            client.feed_status["raid"] = "ok"

        client._subscribe_all = subscribe
        sockets = {"n": 0}

        async def connect(url, **_kwargs):
            sockets["n"] += 1
            events.append(f"open-{sockets['n']}-{url}")
            if sockets["n"] == 1:
                return _Socket([_welcome(keepalive=0.05), "HANG"], events, "old")
            client._stop = True
            return _Socket([], events, "next")

        monkeypatch.setattr("src.twitch.eventsub.websockets.connect", connect)

        async def fast_sleep(_seconds):
            return None

        monkeypatch.setattr("src.twitch.eventsub.asyncio.sleep", fast_sleep)
        await client._run()
        assert sockets["n"] >= 2
        assert any("keepalive" in (client.last_error or "") for _ in [0])
        assert "EventSub keepalive timeout" in (client.last_error or "")

        client2 = EventSubClient("oauth:tok", "cid", "chan", on_event, on_status)
        client2._subscribe_all = subscribe
        order: list[str] = []

        async def connect_handover(url, **_kwargs):
            if "rejoin" in url:
                order.append("open-new")
                sock = _Socket([_welcome("new")], order, "new")

                async def close_and_stop():
                    client2._stop = True

                orig = sock.recv

                async def recv():
                    raw = await orig()
                    await close_and_stop()
                    return raw

                sock.recv = recv
                return sock
            order.append("open-old")
            return _Socket(
                [
                    _welcome("old"),
                    {
                        "metadata": {"message_type": "session_reconnect"},
                        "payload": {"session": {"reconnect_url": "wss://example.test/rejoin"}},
                    },
                ],
                order,
                "old",
            )

        monkeypatch.setattr("src.twitch.eventsub.websockets.connect", connect_handover)
        before = len(subscribed)
        await client2._run()
        assert subscribed[before:] == ["old"]
        assert order.index("open-new") < order.index("close-old")

        client3 = EventSubClient("oauth:tok", "cid", "chan", on_event, on_status)

        async def subscribe_ok(sid):
            client3.feed_status["raid"] = "ok"

        async def status_stop(state):
            statuses.append(state)
            if state == "revoked":
                client3._stop = True

        client3.on_status = status_stop
        client3._subscribe_all = subscribe_ok

        async def connect_revoke(url, **_kwargs):
            return _Socket(
                [
                    _welcome("rev"),
                    {
                        "metadata": {"message_type": "revocation"},
                        "payload": {"subscription": {"type": "channel.raid", "status": "user_removed"}},
                    },
                ],
                events,
                "rev",
            )

        monkeypatch.setattr("src.twitch.eventsub.websockets.connect", connect_revoke)
        await client3._run()
        assert client3.feed_status["raid"].startswith("revoked")
        assert "revoked" in statuses

        class _Wake:
            def status(self):
                return {"enabled": False}

        orch = make_orch(tmp_path, settings)
        orch.mic.wake = _Wake()
        orch.eventsub = client3
        payload = await snapshot(settings, orch)
        assert "revoked" in payload["twitch"]["feeds"]["raid"]

    asyncio.run(run())
    assert KEEPALIVE_GRACE == 3.0
    assert next_backoff(2) == 4
    assert next_backoff(32) == 60
    assert next_backoff(60) == 60


def test_language_swap_waits_for_transcribe_and_devices_refresh(settings, tmp_path, monkeypatch):
    stt = SpeechToText(settings)
    entered = threading.Event()
    release = threading.Event()

    class Model:
        def transcribe(self, audio, language=None, **_kwargs):
            self.language = language
            entered.set()
            assert release.wait(2)
            return [type("Seg", (), {"text": "hi"})()], None

    stt.model = Model()
    audio = np.zeros(1600, dtype=np.float32)
    worker = threading.Thread(target=lambda: stt.transcribe(audio))
    worker.start()
    assert entered.wait(1)
    stt.settings.whisper_language = "es"
    loaded = threading.Event()

    def do_load():
        stt.load()
        loaded.set()

    loader = threading.Thread(target=do_load)
    loader.start()
    time.sleep(0.05)
    assert not loaded.is_set()
    release.set()
    worker.join(2)
    loader.join(2)
    assert loaded.is_set()

    class Rec:
        def transcribe(self, audio, language=None, **_kwargs):
            self.language = language
            return [type("Seg", (), {"text": "hola"})()], None

    rec = Rec()
    stt.model = rec
    assert "hola" in stt.transcribe(audio)
    assert rec.language == "es"

    calls = {"n": 0}

    def fake_list():
        calls["n"] += 1
        return {
            "inputs": [{"index": 5, "name": "USB Mic"}],
            "outputs": [{"index": 2, "name": "Speakers"}],
            "default_input": 1,
            "default_output": 2,
        }

    monkeypatch.setattr("src.health.list_devices", fake_list)
    clear_device_cache()
    first = asyncio.run(load_devices())
    second = asyncio.run(load_devices())
    assert first["inputs"][0]["name"] == "USB Mic"
    assert second == first
    assert calls["n"] == 1
    refreshed = asyncio.run(load_devices(refresh=True))
    assert calls["n"] == 2
    assert refreshed["inputs"][0]["index"] == 5
    assert match_device(refreshed["inputs"], 1, "USB Mic") == 5
    assert match_device(refreshed["outputs"], 9, "missing") == 9

    orch = make_orch(tmp_path, settings)
    app = create_app(prepared=(settings, orch.hub, orch))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        body = client.get("/api/devices?refresh=1")
        assert body.status_code == 200
        assert body.json()["inputs"][0]["name"] == "USB Mic"


def test_background_tasks_log_and_ollama_options(tmp_path, settings, caplog):
    src = (ROOT / "src" / "orchestrator.py").read_text(encoding="utf-8")
    hits = [line.strip() for line in src.splitlines() if "asyncio.create_task(" in line]
    assert hits == ["task = asyncio.create_task(coro, name=name)"]

    async def run():
        orch = make_orch(tmp_path, settings)
        assert orch._ollama_interval == 30

        async def boom():
            raise RuntimeError("broadcast blew up")

        with caplog.at_level("ERROR"):
            task = orch._spawn(boom(), "boom")
            with pytest.raises(RuntimeError):
                await task
            await asyncio.sleep(0)
        assert "broadcast blew up" in caplog.text

        orch._ollama_interval = 0.01

        async def health():
            return {"ok": False, "has_model": False}

        orch.llm.health = health
        watcher = asyncio.create_task(orch._ollama_watch())
        await asyncio.sleep(0.05)
        orch._stopping = True
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher
        assert orch.hub.status.get("ollama") == "down"

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content.decode())
            line = json.dumps({"message": {"content": "hi"}, "done": True}) + "\n"
            return httpx.Response(200, text=line)

        settings.ollama_num_ctx = 2048
        settings.ollama_num_predict_chat = 40
        settings.ollama_keep_alive = "5m"
        client = OllamaClient(settings)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        text = await client.complete([{"role": "user", "content": "hi"}])
        assert text == "hi"
        assert seen["body"]["keep_alive"] == "5m"
        assert seen["body"]["options"]["num_ctx"] == 2048
        assert seen["body"]["options"]["num_predict"] == 40
        await client.aclose()

        settings.chat_timeout_seconds = 0.05
        orch.llm = FakeLLM("late")

        async def slow(messages, temperature=0.7, cancel=None):
            await asyncio.sleep(2)
            return "late"

        orch.llm.complete = slow
        await orch._decide_chat(ChatLine(user="bob", text="hello there"), "chat")
        assert orch._last_skip["reason"] == "llm timeout"
        assert orch.twitch.sent == []

    asyncio.run(run())
    assert model_names_match("qwen2.5:3b", "qwen2.5:3b")
    assert model_names_match("qwen2.5:3b", "qwen2.5:3b:latest")
    assert not model_names_match("qwen2.5:3b", "qwen2.5:3b-instruct")


def test_nova_persona_has_no_pixel_in_output_or_fallback(tmp_path, settings):
    persona = tmp_path / "nova.yaml"
    persona.write_text(
        "name: Nova\npronouns: she/her\nrole: couch cohost\ntone: warm\n"
        "relationship: You sit next to the streamer.\n",
        encoding="utf-8",
    )
    prompt = build_voice_prompt(
        {"name": "Nova", "pronouns": "she/her", "role": "couch cohost", "relationship": "friend"},
        "Luna",
    )
    assert "Pixel" not in prompt
    assert "Nova" in prompt
    assert "Role: couch cohost." in prompt
    assert streamer_smalltalk_fallback("what is your name?", "Nova") == "Nova."
    assert "Pixel" not in streamer_smalltalk_fallback("what is your name?", "Nova")

    memory = ConversationMemory(tmp_path / "memory.json")
    memory.set_cohost_name("Nova")
    memory.remember("cohost_voice", "", "hey from the couch", via="voice")
    turn = list(memory._thread(STREAMER_THREAD))[-1]
    assert turn.who == "Nova"
    assert "Pixel" not in turn.who
    assert "Pixel" not in turn.text
    assert "Pixel" not in json.dumps(memory.snapshot())

    async def run():
        from src.hub import Hub
        from src.orchestrator import Orchestrator

        orch = Orchestrator(
            settings,
            Hub(),
            data_dir=tmp_path / "booth",
            llm=FakeLLM("How can I help you today?"),
            tts=FakeTTS(),
            stt=type("S", (), {"model": None, "device": "cpu", "compute_type": "int8", "error": None, "load": lambda self: None, "transcribe": lambda self, audio: ""})(),
            mic=type("M", (), {"listen_enabled": False, "ptt_down": False, "level": 0.0, "start": lambda self, loop: None, "stop": lambda self: None, "set_listen": lambda self, e: None, "set_ptt": lambda self, d: None})(),
            persona_path=persona,
        )
        orch.twitch = type("C", (), {"connected": True, "sent": [], "send": None, "recent": lambda self, n: []})()

        async def send(text):
            orch.twitch.sent.append(text)

        orch.twitch.send = send
        await orch._decide_chat(ChatLine(user="testchannel", text="what is your name?"), "speak")
        dumped = json.dumps(orch.memory.snapshot()) + " ".join(orch.twitch.sent)
        assert "Pixel" not in dumped
        assert any("Nova" in line for line in orch.twitch.sent)

    asyncio.run(run())
