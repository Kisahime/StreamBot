import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.health import snapshot
from src.main import create_app
from src.memory import STREAMER_THREAD
from src.policy import chat_action
from src.safety import fence_viewer_text
from src.settings import RewardSpec, Settings, load_settings
from src.twitch.chat import ChatLine, message_is_first
from src.twitch.eventsub import StreamEvent, _parse_notification
from tests.test_orchestrator_policy import FakeLLM, make_orch


class CaptureLLM(FakeLLM):
    def __init__(self, text="hey there"):
        super().__init__(text)
        self.seen: list[list[dict]] = []

    async def complete(self, messages, temperature=0.7, cancel=None):
        self.seen.append(messages)
        return self.text


def _wake(orch) -> None:
    class _Wake:
        def status(self):
            return {"enabled": False}

    orch.mic.wake = _Wake()


def _profiles() -> dict:
    return {
        "Just chatting": {
            "mood": "hype",
            "cooldown_seconds": 8,
            "per_user_cooldown_seconds": 12,
            "events_speak": True,
            "post_voice_replies_to_chat": True,
            "max_replies_per_minute": 12,
        },
        "Focus game": {
            "mood": "chill",
            "cooldown_seconds": 30,
            "per_user_cooldown_seconds": 45,
            "events_speak": False,
            "post_voice_replies_to_chat": False,
            "max_replies_per_minute": 3,
        },
    }


def test_reward_mapping_prompt_cooldown_and_string_form(tmp_path, settings):
    async def run():
        settings.channel_point_rewards = {
            "hydrate": RewardSpec("speak"),
            "roast me": RewardSpec(
                "speak",
                "Playfully roast {user} in one line. They wrote: {input}",
                120,
            ),
        }
        orch = make_orch(tmp_path, settings)
        llm = CaptureLLM("toasted")
        orch.llm = llm
        assert settings.reward_action("Hydrate") == "speak"
        assert settings.reward_spec("hydrate").prompt == ""

        await orch.on_stream_event(
            StreamEvent(kind="redeem", user="Sam", extra={"title": "Roast Me", "input": "be nice"})
        )
        assert len(orch._event_pending) == 1
        await orch._react_event(orch._event_pending[0])
        content = "\n".join(message["content"] for batch in llm.seen for message in batch)
        assert "Playfully roast Sam" in content
        assert fence_viewer_text("be nice") in content
        assert "Hype it like a cohost" not in content

        await orch.on_stream_event(
            StreamEvent(kind="redeem", user="Sam", extra={"title": "roast me", "input": "again"})
        )
        assert orch._last_skip["reason"] == "reward cooldown"
        assert len(orch._event_pending) == 1

        llm.seen.clear()
        hydrate = StreamEvent(kind="redeem", user="Sam", extra={"title": "hydrate", "input": ""})
        await orch.on_stream_event(hydrate)
        queued = [event for event in orch._event_pending if event.extra.get("title") == "hydrate"]
        assert queued and queued[0].extra.get("delivery") == "speak"
        await orch._react_event(queued[0])
        assert "Hype it like a cohost" in json.dumps(llm.seen)

    asyncio.run(run())


def test_reward_specs_parse_both_shapes():
    built = Settings(
        {
            "events": {
                "channel_point_rewards": {
                    "hydrate": "speak",
                    "roast me": {"action": "chat", "prompt": "hi {user}", "cooldown": 5},
                    "nope": {"action": "dance"},
                }
            }
        }
    )
    assert built.reward_action("hydrate") == "speak"
    spec = built.reward_spec("Roast Me")
    assert spec.action == "chat"
    assert spec.prompt == "hi {user}"
    assert spec.cooldown == 5
    assert built.reward_action("nope") == "ignore"


def test_poll_and_prediction_winner_text():
    poll = _parse_notification(
        "channel.poll.end",
        {
            "title": "Boss fight",
            "status": "completed",
            "choices": [
                {"title": "Loot first", "votes": 38},
                {"title": "Boss first", "votes": 62},
            ],
        },
    )
    assert poll is not None
    assert poll.scene() == "Poll ended: 'Boss first' won with 62%."

    prediction = _parse_notification(
        "channel.prediction.end",
        {
            "title": "Win?",
            "status": "resolved",
            "winning_outcome_id": "yes",
            "outcomes": [
                {"id": "yes", "title": "Yes", "users": 10, "channel_points": 620},
                {"id": "no", "title": "No", "users": 4, "channel_points": 380},
            ],
        },
    )
    assert prediction is not None
    assert prediction.scene() == "Prediction ended: 'Yes' won with 62%."
    assert _parse_notification("channel.poll.end", {"title": "old", "status": "archived"}) is None
    assert _parse_notification("channel.prediction.end", {"title": "old", "status": "terminated"}) is None
    plain = StreamEvent(kind="poll_end", user="chat", extra={"title": "a poll"})
    assert plain.scene() == "The poll ended: a poll."


def test_first_message_is_a_typed_welcome(tmp_path, settings):
    async def run():
        assert message_is_first(SimpleNamespace(first=True)) is True
        assert message_is_first(SimpleNamespace(tags={"first-msg": "1"})) is True
        assert message_is_first(SimpleNamespace(tags={"first-msg": "0"})) is False

        settings.welcome_first_time = True
        orch = make_orch(tmp_path, settings)
        llm = CaptureLLM("welcome in")
        orch.llm = llm
        first = ChatLine(user="newbie", text="hello?", first_msg=True)
        assert chat_action(first, welcome_first_time=True) == "chat"
        await orch._decide_chat(first, "chat")
        assert orch.tts.spoken == []
        assert "first message ever in this channel" in json.dumps(llm.seen)

        llm.seen.clear()
        second = ChatLine(user="newbie", text="hello?", first_msg=False)
        assert chat_action(second, welcome_first_time=True) == "speak"
        await orch._decide_chat(second, "speak")
        assert "first message ever in this channel" not in json.dumps(llm.seen)
        assert orch.tts.spoken

        assert chat_action(ChatLine(user="newbie", text="hello?", first_msg=True)) == "speak"

    asyncio.run(run())


def test_recap_sections_and_file(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        orch.llm = CaptureLLM("A cozy raid night.")
        memory = orch.memory
        memory.remember("twitch_event", "Luna", "Luna just followed the channel.", via="event")
        memory.remember("twitch_event", "Neo", "Neo just subscribed.", via="event")
        memory.remember("twitch_event", "RaidBoss", "RaidBoss just raided in with 10 viewers.", via="event")
        memory.remember("viewer", "bob", "hi", via="chat")
        memory.remember("viewer", "bob", "again", via="chat")
        memory.remember("viewer", "sam", "yo", via="chat")
        memory.add_note(STREAMER_THREAD, "clutch boss", is_streamer=True)

        result = await orch.make_recap(vibe=True)
        text = result["markdown"]
        assert "## Follows" in text
        assert "Luna just followed the channel." in text
        assert "## Subs" in text
        assert "Neo just subscribed." in text
        assert "## Raids" in text
        assert "RaidBoss just raided" in text
        assert "## Top chatters" in text
        assert "bob — 2 messages" in text
        assert "## Noted moments" in text
        assert "clutch boss" in text
        assert "## Vibe" in text
        assert "A cozy raid night." in text
        path = tmp_path / "recaps" / result["path"]
        assert path.name == f"{datetime.now():%Y-%m-%d}.md"
        assert path.read_text(encoding="utf-8") == text

    asyncio.run(run())


def test_alert_and_recap_timeouts_keep_going(tmp_path, settings):
    class SlowThenFast:
        def __init__(self):
            self.calls = 0
            self.last_error = None

        async def complete(self, messages, temperature=0.7, cancel=None):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(60)
            return "second alert line"

        async def health(self):
            return {"ok": True, "has_model": True}

        async def aclose(self):
            return None

    class SleepLLM:
        async def complete(self, messages, temperature=0.7, cancel=None):
            await asyncio.sleep(60)
            return "a long vibe"

        async def health(self):
            return {"ok": True}

        async def aclose(self):
            return None

    async def run():
        settings.chat_timeout_seconds = 0.05
        settings.events_speak = False
        orch = make_orch(tmp_path, settings)
        orch.llm = SlowThenFast()
        first = StreamEvent(kind="follow", user="Ada")
        second = StreamEvent(kind="follow", user="Bea")
        orch._event_pending.append(first)
        orch._event_pending.append(second)
        await orch._drain_events()
        assert orch._last_skip["reason"] == "llm timeout"
        assert orch.hub.status.get("phase") == "idle"
        assert any("Ada" in event.text for event in orch.memory.events)
        assert any("second alert line" in line for line in orch.twitch.sent)
        assert orch.llm.calls >= 2

        orch.llm = SleepLLM()
        result = await orch.make_recap(vibe=True)
        assert "## Vibe" not in result["markdown"]
        assert "a long vibe" not in result["markdown"]

    asyncio.run(run())


def test_mod_pixel_controls_and_viewer_is_ignored(tmp_path, settings):
    async def run():
        settings.control_cooldown_seconds = 30
        orch = make_orch(tmp_path, settings)
        viewer = ChatLine(user="bob", text="!pixel chill", command="!pixel")
        orch._chat_pending.append(viewer)
        assert await orch._consider_chat_locked() is True
        assert orch.mood == "normal"
        assert orch._last_skip["reason"] == "not a mod"
        assert orch.twitch.sent == []
        assert orch.tts.spoken == []

        mod = ChatLine(user="moddy", text="!pixel chill", command="!pixel", is_mod=True)
        orch._chat_pending.append(mod)
        assert await orch._consider_chat_locked() is True
        assert orch.mood == "chill"
        assert orch.twitch.sent == ["Mood is chill."]
        assert orch.tts.spoken == []

        again = ChatLine(user="moddy", text="!pixel hype", command="!pixel", is_mod=True)
        orch._chat_pending.append(again)
        assert await orch._consider_chat_locked() is True
        assert orch.mood == "chill"
        assert orch._last_skip["reason"] == "control cooldown"

    asyncio.run(run())


def test_queue_drop_answer_and_replay(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        orch.llm = CaptureLLM("@sam hey")
        chat = ChatLine(user="sam", text="hey", mention=True)
        alert = StreamEvent(kind="raid", user="Neo", extra={"viewers": 4})
        orch._chat_pending.append(chat)
        orch._event_pending.append(alert)
        snap = orch.queue_snapshot()
        assert snap["chat"][0]["id"]
        assert snap["alerts"][0]["id"]
        assert await orch.drop_queued(snap["alerts"][0]["id"]) is True
        assert orch.queue_snapshot()["alerts"] == []

        orch._mark_reply("sam")
        blocked, why = orch._rate_ok(mention=True, user="sam")
        assert blocked is False
        assert why
        assert await orch.answer_queued(snap["chat"][0]["id"]) is True
        assert orch.twitch.sent
        assert list(orch._chat_pending) == []

        orch._last_caption = "last line"
        orch.twitch.sent.clear()
        orch.tts.spoken.clear()
        assert await orch.replay_last() is True
        assert orch.twitch.sent == []
        assert orch.tts.spoken

    asyncio.run(run())


def test_profile_switches_live_values_and_survives_restart(tmp_path, settings):
    async def run():
        settings.profiles = _profiles()
        orch = make_orch(tmp_path, settings)
        assert await orch.apply_profile("Focus game") == "Focus game"
        assert orch.mood == "chill"
        assert orch.settings.events_speak is False
        assert orch.settings.post_voice_replies_to_chat is False
        assert orch.settings.cooldown_seconds == 30
        assert orch.settings.max_replies_per_minute == 3
        _wake(orch)
        health = await snapshot(settings, orch)
        assert health["profile"] == "Focus game"
        assert health["runtime"]["events_speak"] is False
        assert health["runtime"]["mood"] == "chill"
        assert "Focus game" in health["profiles"]

        fresh = Settings(settings._data)
        fresh.profiles = _profiles()
        restarted = make_orch(tmp_path, fresh)
        assert restarted.profile_name == "Focus game"
        assert restarted.mood == "chill"
        assert restarted.settings.events_speak is False
        assert restarted.settings.max_replies_per_minute == 3
        assert restarted.settings.cooldown_seconds == 30

        assert await restarted.apply_profile("") == ""
        assert restarted.mood == "normal"
        assert restarted.settings.events_speak is True
        assert restarted.settings.cooldown_seconds == 12

    asyncio.run(run())


def test_phase4_routes(tmp_path, settings):
    settings.profiles = _profiles()
    orch = make_orch(tmp_path, settings)
    orch._chat_pending.append(ChatLine(user="sam", text="hey", mention=True))
    orch._last_caption = "replay me"
    app = create_app(prepared=(settings, orch.hub, orch))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"x-cohost-token": token, "origin": "http://127.0.0.1:8080"}
        queued = client.get("/api/queue").json()
        dropped = client.post("/api/queue/drop", json={"id": queued["chat"][0]["id"]}, headers=headers)
        assert dropped.status_code == 200
        assert dropped.json()["queue"]["chat"] == []
        orch._chat_pending.append(ChatLine(user="sam", text="hey", mention=True))
        orch._mark_reply("sam")
        queued = client.get("/api/queue").json()
        answered = client.post("/api/queue/answer", json={"id": queued["chat"][0]["id"]}, headers=headers)
        assert answered.status_code == 200
        assert orch.twitch.sent
        replayed = client.post("/api/queue/replay", json={}, headers=headers)
        assert replayed.status_code == 200
        assert orch.tts.spoken
        recap = client.post("/api/recap", json={"vibe": False}, headers=headers)
        assert recap.status_code == 200
        assert "## Top chatters" in recap.json()["markdown"]
        switched = client.post("/api/profile", json={"name": "Just chatting"}, headers=headers)
        assert switched.status_code == 200
        assert orch.mood == "hype"
        missing = client.post("/api/profile", json={"name": "nope"}, headers=headers)
        assert missing.status_code == 400


def test_shipped_config_and_overlay_hooks():
    from pathlib import Path

    loaded = load_settings()
    assert loaded.reward_action("hydrate") == "speak"
    assert loaded.welcome_first_time is True
    assert loaded.profiles["Focus game"]["mood"] == "chill"
    assert loaded.profiles["Focus game"]["events_speak"] is False
    assert loaded.profiles["Just chatting"]["max_replies_per_minute"] == 12
    root = Path(__file__).resolve().parents[1]
    script = (root / "src/web/static/overlay.js").read_text(encoding="utf-8")
    css = (root / "src/web/static/overlay.css").read_text(encoding="utf-8")
    assert "JSON.parse" in script
    assert "catch" in script
    assert 'get("alerts") === "0"' in script
    assert "fontsize" in script
    assert "dataset.kind" in script
    assert "prefers-reduced-motion" in css
    assert '[data-kind="raid"]' in css
