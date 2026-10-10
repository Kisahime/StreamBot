import asyncio
import copy
import os
import time
from pathlib import Path

from src.health import snapshot
from src.policy import custom_reply
from src.settings import Settings, load_settings
from src.twitch.chat import ChatLine
from tests.test_api import _client, _headers
from tests.test_orchestrator_policy import PERSONA_FIXTURE, make_orch


def _wake(orch) -> None:
    class _Wake:
        def status(self):
            return {"enabled": False}

    orch.mic.wake = _Wake()


def test_custom_reply_fills_the_user():
    line = ChatLine(user="bob", text="!lurk", command="!Lurk")
    assert custom_reply(line, {"!lurk": "enjoy the lurk {user}"}) == "enjoy the lurk bob"
    assert custom_reply(line, {}) == ""


def test_unknown_profile_voice_is_rejected_at_load(settings):
    data = copy.deepcopy(settings._data)
    data["profiles"] = {"Loud": {"tts_voice": "not-a-voice"}}
    try:
        Settings(data)
    except ValueError as exc:
        assert "unknown voice" in str(exc)
    else:
        raise AssertionError("invalid profile voice was accepted")


def test_profile_swaps_voice_scale_and_persona(tmp_path, settings, monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr("src.orchestrator.update_config", lambda payload: written.append(payload))
    other = tmp_path / "other.yaml"
    other.write_text(PERSONA_FIXTURE.read_text(encoding="utf-8").replace("name: Pixel", "name: Nova"), encoding="utf-8")
    settings.profiles = {
        "Kristin": {
            "mood": "hype",
            "cooldown_seconds": 8,
            "per_user_cooldown_seconds": 12,
            "events_speak": True,
            "post_voice_replies_to_chat": True,
            "max_replies_per_minute": 12,
            "tts_voice": "en_US-kristin-medium",
            "length_scale": 0.9,
            "persona_file": str(other),
        }
    }

    async def run():
        orch = make_orch(tmp_path, settings)
        downloads: list[str] = []

        def ensure():
            assert callable(orch.tts.on_progress)
            orch.tts.on_progress("Downloading voice")
            downloads.append(orch.settings.tts_voice)

        orch.tts.ensure_files = ensure
        assert await orch.apply_profile("Kristin") == "Kristin"
        for task in list(orch._bg):
            await task
        assert orch.settings.tts_voice == "en_US-kristin-medium"
        assert orch.settings.tts_length_scale == 0.9
        assert orch.persona["name"] == "Nova"
        assert downloads == ["en_US-kristin-medium"]
        _wake(orch)
        health = await snapshot(settings, orch)
        assert health["voice"] == "en_US-kristin-medium"
        assert written == []

        assert await orch.apply_profile("") == ""
        for task in list(orch._bg):
            await task
        assert orch.settings.tts_voice == "en_US-amy-medium"
        assert orch.settings.tts_length_scale == 1.05
        assert orch.persona["name"] == "Pixel"
        health = await snapshot(settings, orch)
        assert health["voice"] == "en_US-amy-medium"

    asyncio.run(run())


def test_session_transcript_rotates_and_forget_scrubs_it(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        orch.memory.remember("viewer", "Sam", "hello there", via="chat")
        orch.memory.remember("twitch_event", "Sam", "followed", via="event")
        orch.memory.remember("viewer", "Other", "still here", via="chat")
        path = orch.memory.transcript_path()
        assert path.exists()
        body = path.read_text(encoding="utf-8")
        assert "hello there" in body
        assert "followed" in body

        first = orch.memory.session_id
        orch.memory.start_session()
        orch.memory.remember("viewer", "Other", "next session", via="chat")
        assert (orch.memory.sessions_dir() / f"{first}.jsonl").exists()
        assert "next session" in orch.memory.transcript_path().read_text(encoding="utf-8")
        assert "hello there" not in orch.memory.transcript_path().read_text(encoding="utf-8")

        orch.forget_viewer("sam")
        kept = (orch.memory.sessions_dir() / f"{first}.jsonl").read_text(encoding="utf-8")
        assert "hello there" not in kept
        assert "followed" not in kept
        assert "still here" in kept

        old = orch.memory.sessions_dir() / "oldsession.jsonl"
        old.write_text('{"user":"ghost","text":"old"}\n', encoding="utf-8")
        stale = time.time() - 40 * 86400
        os.utime(old, (stale, stale))
        orch.memory.prune()
        assert not old.exists()
        assert orch.memory.transcript_path().exists()

    asyncio.run(run())


def test_custom_command_posts_once_per_cooldown(tmp_path, settings):
    async def run():
        settings.custom_commands = {"!lurk": "enjoy the lurk {user}", "!discord": "join the discord"}
        settings.custom_command_cooldown_seconds = 30
        orch = make_orch(tmp_path, settings)
        model_calls: list[str] = []
        shoutouts: list[str | None] = []

        async def complete(messages, temperature=0.7, cancel=None):
            model_calls.append("complete")
            return "from the model"

        async def fake_shoutout(login=None):
            shoutouts.append(login)

        orch.llm.complete = complete
        orch.shoutout = fake_shoutout
        assert "!lurk" not in settings.chat_commands
        assert "!lurk" in orch._irc_commands()
        assert "!discord" in orch._irc_commands()

        orch._last_chat_reply = time.monotonic()
        orch._chat_pending.append(ChatLine(user="bob", text="!lurk what is this?", command="!lurk"))
        assert await orch._consider_chat_locked() is True
        assert orch.twitch.sent == ["enjoy the lurk bob"]
        assert model_calls == []
        assert shoutouts == []

        orch._chat_pending.append(ChatLine(user="sam", text="!lurk", command="!lurk"))
        assert await orch._consider_chat_locked() is True
        assert orch.twitch.sent == ["enjoy the lurk bob"]
        assert orch._last_skip["reason"] == "command cooldown"

        orch._custom_at["!lurk"] = 0
        orch._chat_pending.append(ChatLine(user="sam", text="!lurk", command="!lurk"))
        assert await orch._consider_chat_locked() is True
        assert orch.twitch.sent[-1] == "enjoy the lurk sam"
        assert model_calls == []
        assert shoutouts == []

    asyncio.run(run())


def test_latency_window_reports_percentiles_and_warns(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        for sample in range(60):
            orch.note_timing("stt_ms", 100 + sample)
            orch.note_timing("first_audio_ms", 3000)
        assert len(orch.timing_samples["first_audio_ms"]) == 50
        assert orch.timings["first_audio_ms"] == 3000
        _wake(orch)
        health = await snapshot(settings, orch)
        assert health["timings_p50"]["stt_ms"] > 0
        assert health["timings_p95"]["first_audio_ms"] > 2500
        assert health["timing_warning"] == "First-audio p95 is over 2.5 s."
        assert health["timings"]["stt_ms"] == 159

    asyncio.run(run())


def test_shipped_config_keeps_custom_commands_empty():
    loaded = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    assert loaded.custom_commands == {}
    assert loaded.custom_command_cooldown_seconds == 30
    assert "tts_voice" not in loaded.profiles["Just chatting"]
    assert "tts_voice" not in loaded.profiles["Focus game"]


def test_transcript_route_is_on_the_dashboard(tmp_path, settings):
    client, orch = _client(tmp_path, settings)
    orch.memory.remember("viewer", "sam", "line one", via="chat")
    with client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Download transcript" in page.text
        assert 'id="timingWarn"' in page.text
        assert 'id="vram-tips"' in page.text
        body = client.get("/api/transcript")
        assert "line one" in body.text
        client.post("/api/memory/forget", headers=_headers(client), json={"login": "sam"})
        assert "line one" not in client.get("/api/transcript").text
