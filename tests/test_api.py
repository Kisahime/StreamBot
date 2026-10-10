import asyncio

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.audio.mic import MicListener
from src.main import create_app
from src.policy import chat_action, pixel_control
from src.safety import is_blocked
from src.twitch.chat import ChatLine
from tests.test_orchestrator_policy import PERSONA_FIXTURE, make_orch


class _Wake:
    def status(self):
        return {"enabled": False, "ready": False}


def _client(tmp_path, settings):
    orch = make_orch(tmp_path, settings)
    orch.mic.wake = _Wake()

    async def disconnect():
        return None

    def set_allow_from(_names):
        return None

    orch.twitch.disconnect = disconnect
    orch.twitch.set_allow_from = set_allow_from
    app = create_app(prepared=(settings, orch.hub, orch))
    return TestClient(app, base_url="http://127.0.0.1"), orch


def _headers(client: TestClient) -> dict[str, str]:
    token = client.get("/api/session").json()["token"]
    return {"x-cohost-token": token, "origin": "http://127.0.0.1:8080"}


def test_dashboard_routes_round_trip(tmp_path, settings, monkeypatch):
    monkeypatch.setattr("src.orchestrator.update_config", lambda *_a, **_k: None)
    settings.profiles = {
        "Just chatting": {
            "mood": "hype",
            "cooldown_seconds": 8,
            "per_user_cooldown_seconds": 12,
            "events_speak": True,
            "post_voice_replies_to_chat": True,
            "max_replies_per_minute": 12,
        }
    }
    client, orch = _client(tmp_path, settings)
    persona = (tmp_path / "persona.yaml")
    persona.write_text(PERSONA_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    orch.persona_path = persona
    orch.tts.ensure_files = lambda: None

    async def irc_ok():
        return None

    async def events_ok():
        return None

    orch._irc_connect = irc_ok
    orch._eventsub_connect = events_ok

    with client:
        headers = _headers(client)
        assert client.get("/").status_code == 200
        assert "Twitch Cohost" in client.get("/").text
        assert client.get("/overlay").status_code == 200
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"]["post_voice"] is True
        assert "oauth:" not in health.text
        assert client.get("/api/devices").status_code == 200
        assert client.get("/api/devices?refresh=1").status_code == 200
        assert client.get("/api/queue").json() == {"chat": [], "alerts": [], "questions": []}
        assert client.get("/api/tts/voices").json()["current"] == settings.tts_voice
        assert client.get("/api/persona").status_code == 200
        assert client.get("/api/streamer").json()["spoken_name"]
        assert client.get("/api/memory").status_code == 200

        assert client.post("/api/listen", json={"enabled": True}, headers=headers).status_code == 400
        assert client.post("/api/listen", json={"enabled": False}, headers=headers).json()["ok"] is True
        assert client.post("/api/ptt", json={"down": False}, headers=headers).json()["ptt"] is False
        assert client.post("/api/tts/mute", json={"mute": True}, headers=headers).json()["mute"] is True
        assert client.post("/api/interrupt", json={}, headers=headers).json()["ok"] is True
        assert client.post("/api/say", json={"text": "hello chat", "post_chat": False}, headers=headers).json()["ok"]
        assert client.post("/api/pretend", json={"text": "what is up"}, headers=headers).json()["ok"]
        assert client.post("/api/mood", json={"mood": "hype"}, headers=headers).json()["mood"] == "hype"
        assert client.post("/api/mood", json={"mood": "nope"}, headers=headers).status_code == 400
        saved = client.post("/api/persona", json={"text": persona.read_text(encoding="utf-8")}, headers=headers)
        assert saved.status_code == 200
        assert client.post("/api/persona", json={"text": "hello: 1"}, headers=headers).status_code == 400
        assert persona.read_text(encoding="utf-8").startswith("name:")
        assert client.post("/api/persona/reload", json={}, headers=headers).json()["name"]
        streamer = client.post(
            "/api/streamer",
            json={"display_name": "Luna", "twitch_login": "luna"},
            headers=headers,
        )
        assert streamer.json()["spoken_name"] == "Luna"
        assert client.post("/api/memory/clear", json={}, headers=headers).json()["ok"] is True
        assert client.post("/api/memory/forget", json={"login": "someone"}, headers=headers).json()["ok"] is True
        marker = client.post("/api/marker", json={"note": "clip"}, headers=headers)
        assert marker.json()["note"] == "clip"
        vad = client.post(
            "/api/audio/vad",
            json={"silence_ms": 900, "energy_threshold": 0.02, "max_utterance_seconds": 15},
            headers=headers,
        )
        assert vad.status_code == 200
        devices = client.post(
            "/api/audio/devices",
            json={"mic_device": None, "speaker_device": None},
            headers=headers,
        )
        assert devices.status_code == 200
        duck = client.post("/api/audio/duck", json={"enabled": False}, headers=headers)
        assert duck.status_code == 200
        assert orch.hub.status["duck_enabled"] is False
        assert client.post("/api/stt/language", json={"language": "en"}, headers=headers).json()["language"] == "en"
        assert client.post("/api/stt/language", json={"language": "xx"}, headers=headers).status_code == 400
        voice = client.post("/api/tts/voice", json={"voice": settings.tts_voice}, headers=headers)
        assert voice.status_code == 200
        assert client.post("/api/tts/voice", json={"voice": "missing"}, headers=headers).status_code == 400
        profile = client.post("/api/profile", json={"name": "Just chatting"}, headers=headers)
        assert profile.json()["profile"] == "Just chatting"
        assert client.post("/api/profile", json={"name": "nope"}, headers=headers).status_code == 400
        assert client.post("/api/queue/drop", json={"id": "missing"}, headers=headers).status_code == 404
        assert client.post("/api/queue/answer", json={"id": "missing"}, headers=headers).status_code == 404
        assert client.post("/api/queue/replay", json={}, headers=headers).json()["ok"] is True
        recap = client.post("/api/recap", json={"vibe": False}, headers=headers)
        assert recap.status_code == 200
        assert "markdown" in recap.json()
        assert client.post("/api/twitch/irc", json={"enabled": True}, headers=headers).json()["ok"] is True
        assert client.post("/api/twitch/events", json={"enabled": True}, headers=headers).json()["ok"] is True
        assert client.post("/api/twitch/connect", json={}, headers=headers).json()["ok"] is True
        assert client.post("/api/twitch/disconnect", json={}, headers=headers).json()["ok"] is True
        shout = client.post("/api/twitch/shoutout", json={}, headers=headers)
        assert shout.status_code == 400

        async def boom():
            raise RuntimeError("irc down")

        orch._irc_connect = boom
        failed = client.post("/api/twitch/irc", json={"enabled": True}, headers=headers)
        assert failed.status_code == 400
        assert "irc down" in failed.json()["error"]

        async def broken_devices(*_a, **_k):
            raise RuntimeError("no devices")

        monkeypatch.setattr("src.health.load_devices", broken_devices)
        assert client.get("/api/devices?refresh=1").status_code == 500


def test_post_voice_reaches_a_second_websocket(tmp_path, settings):
    client, orch = _client(tmp_path, settings)
    with client:
        headers = _headers(client)
        with client.websocket_connect("/ws", headers={"host": "127.0.0.1"}) as left:
            with client.websocket_connect("/ws", headers={"host": "127.0.0.1"}) as right:
                assert left.receive_json()["type"] == "status"
                assert right.receive_json()["type"] == "status"
                response = client.post("/api/chat/post_voice", json={"enabled": False}, headers=headers)
                assert response.status_code == 200
                assert orch.settings.post_voice_replies_to_chat is False
                for sock in (left, right):
                    message = sock.receive_json()
                    assert message["type"] == "status"
                    assert message["data"]["post_voice"] is False


def test_is_blocked_and_policy_edges():
    assert is_blocked("you are a retard") is True
    assert is_blocked("what meds should i take") is True
    assert is_blocked("nice stream") is False
    bare = ChatLine(user="mod", text="!pixel", command="!pixel", is_mod=True)
    assert pixel_control(bare) == ""
    assert chat_action(bare) == "speak"
    steered = ChatLine(user="mod", text="!pixel stop", command="!pixel", is_mod=True)
    assert pixel_control(steered) == "stop"
    assert chat_action(steered) == "control"
    other = ChatLine(user="viewer", text="!foo later", command="!foo")
    assert chat_action(other) == "command"


def test_mic_state_machine_emits_a_loud_chunk(settings, monkeypatch):
    heard: list[np.ndarray] = []

    async def on_utterance(audio):
        heard.append(audio)

    settings.sample_rate = 16000
    settings.silence_ms = 100
    settings.min_utterance_ms = 50
    settings.energy_threshold = 0.01
    settings.echo_gate = False
    settings.barge_in = False
    monkeypatch.setattr("src.audio.mic.has_speech_silero", lambda _audio, _sr: True)
    mic = MicListener(settings, on_utterance)
    mic.listen_enabled = True

    async def run():
        task = asyncio.create_task(mic._run())
        loud = np.full(480, 0.2, dtype=np.float32)
        quiet = np.zeros(480, dtype=np.float32)
        for _ in range(3):
            mic._enqueue(loud)
        for _ in range(6):
            mic._enqueue(quiet)
        for _ in range(40):
            if heard:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert heard
    assert heard[0].size >= 800


def test_mic_drops_the_oldest_chunk_when_the_queue_is_full(settings):
    mic = MicListener(settings, lambda _audio: asyncio.sleep(0))
    for index in range(205):
        mic._enqueue(np.array([index], dtype=np.float32))
    assert mic._queue.qsize() == 200
    first = mic._queue.get_nowait()
    assert int(first[0]) == 5


def test_mic_ignores_chunks_until_it_is_armed(settings, monkeypatch):
    heard: list[np.ndarray] = []

    async def on_utterance(audio):
        heard.append(audio)

    settings.sample_rate = 16000
    settings.silence_ms = 30
    settings.min_utterance_ms = 20
    settings.energy_threshold = 0.01
    monkeypatch.setattr("src.audio.mic.has_speech_silero", lambda _audio, _sr: True)
    mic = MicListener(settings, on_utterance)

    async def run():
        task = asyncio.create_task(mic._run())
        loud = np.full(480, 0.4, dtype=np.float32)
        for _ in range(8):
            mic._enqueue(loud)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert heard == []


def test_control_key_is_limited_to_booth_actions(tmp_path, settings, monkeypatch):
    monkeypatch.setenv("COHOST_CONTROL_KEY", "deck-secret")
    client, _orch = _client(tmp_path, settings)
    key = {
        "x-cohost-token": "deck-secret",
        "origin": "http://127.0.0.1:8080",
    }
    with client:
        assert client.post("/api/interrupt", headers=key, json={}).status_code == 200
        assert client.post("/api/mood", headers=key, json={"mood": "hype"}).status_code == 200
        assert client.post("/api/tts/mute", headers=key, json={"mute": True}).status_code == 200
        assert client.post("/api/profile", headers=key, json={"name": ""}).status_code == 200
        assert client.post("/api/persona", headers=key, json={"text": "name: nope"}).status_code == 401
        assert client.post("/api/say", headers=key, json={"text": "hi"}).status_code == 401
        assert client.post("/api/interrupt", headers=_headers(client), json={}).status_code == 200


def test_unset_control_key_changes_nothing(tmp_path, settings, monkeypatch):
    monkeypatch.delenv("COHOST_CONTROL_KEY", raising=False)
    client, _orch = _client(tmp_path, settings)
    key = {
        "x-cohost-token": "deck-secret",
        "origin": "http://127.0.0.1:8080",
    }
    with client:
        assert client.post("/api/interrupt", headers=key, json={}).status_code == 401
        assert client.post("/api/persona", headers=key, json={"text": "name: nope"}).status_code == 401
        assert client.post("/api/interrupt", headers=_headers(client), json={}).status_code == 200
