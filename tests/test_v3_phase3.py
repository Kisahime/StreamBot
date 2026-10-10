import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.health import _twitch_feeds
from src.main import create_app
from src.memory import STREAMER_THREAD
from src.settings import Settings
from src.twitch.auth import missing_scope
from src.twitch.chat import (
    ChatLine,
    TwitchChat,
    format_reply_privmsg,
    message_id,
    safe_reply_parent,
)
from src.twitch.eventsub import StreamEvent, _parse_notification
from tests.test_orchestrator_policy import make_orch


class _Clock:
    moment = datetime(2026, 10, 10, 12, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.moment


class _Events:
    def __init__(self, scopes):
        self.scopes = scopes
        self.broadcaster_id = "99"
        self.moderator_id = "99"
        self.feed_status = {}


def test_hype_meter_payload_and_end_hold(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        seen = []
        original = orch.hub.broadcast

        async def wrap(event):
            seen.append(event)
            await original(event)

        orch.hub.broadcast = wrap
        await orch.on_stream_event(
            StreamEvent(
                kind="hype_progress",
                user="chat",
                extra={"level": 3, "progress": 40, "goal": 80, "total": 40, "expires_at": "2026-10-10T12:00:00Z"},
            )
        )
        progress = [item for item in seen if item.get("type") == "hype"][-1]["data"]
        assert progress["progress"] == 40
        assert progress["goal"] == 80
        assert progress["active"] is True
        assert progress["hold_seconds"] == settings.overlay_alert_hold
        assert orch.hub.status["hype"]["level"] == 3
        assert not any(event.kind == "hype_progress" for event in orch._event_pending)

        await orch.on_stream_event(StreamEvent(kind="hype_end", user="chat", extra={"level": 4, "progress": 80, "goal": 80}))
        ended = [item for item in seen if item.get("type") == "hype"][-1]["data"]
        assert ended["active"] is False
        assert ended["hold_seconds"] == settings.overlay_alert_hold

    asyncio.run(run())


def test_stream_sessions_write_separate_recaps(tmp_path, settings, monkeypatch):
    monkeypatch.setattr("src.core.event_flow.datetime", _Clock)

    async def run():
        orch = make_orch(tmp_path, settings)
        orch.memory.remember("twitch_event", "Old", "Old just followed the channel.", via="event")
        await orch.on_stream_event(StreamEvent(kind="stream_online", user="testchannel"))
        orch.memory.remember("twitch_event", "A", "A cheered 50 bits.", via="event")
        orch.memory.remember("twitch_event", "B", "B redeemed hydrate.", via="event")
        orch.memory.remember("twitch_event", "C", "A poll just started: snacks.", via="event")
        orch.mic.listen_enabled = True
        _Clock.moment = datetime(2026, 10, 10, 12, 0)
        await orch.on_stream_event(StreamEvent(kind="stream_offline", user="testchannel"))
        first = tmp_path / "recaps" / "2026-10-10-1200.md"
        assert first.exists()
        text = first.read_text(encoding="utf-8")
        assert "A cheered 50 bits." in text
        assert "B redeemed hydrate." in text
        assert "A poll just started: snacks." in text
        assert "Old just followed" not in text
        assert orch.mic.listen_enabled is False

        await orch.on_stream_event(StreamEvent(kind="stream_online", user="testchannel"))
        orch.memory.remember("twitch_event", "D", "D just followed the channel.", via="event")
        _Clock.moment = datetime(2026, 10, 10, 13, 5)
        await orch.on_stream_event(StreamEvent(kind="stream_offline", user="testchannel"))
        second = tmp_path / "recaps" / "2026-10-10-1305.md"
        assert second.exists()
        later = second.read_text(encoding="utf-8")
        assert "D just followed the channel." in later
        assert "A cheered 50 bits." not in later
        assert "Old just followed" not in later

    asyncio.run(run())


def test_ad_break_types_a_line_and_reports_missing_scope(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        event = _parse_notification(
            "channel.ad_break.begin",
            {"duration_seconds": 30},
            channel="testchannel",
        )
        assert event is not None
        assert event.kind == "ad_break"
        await orch.on_stream_event(event)
        assert len(orch.twitch.sent) == 1
        assert "ad break, back in 30 seconds" in orch.twitch.sent[0]
        assert orch.tts.spoken == []
        assert orch.twitch.replies == []

    asyncio.run(run())
    assert missing_scope(set(), "channel.ad_break.begin") == "channel:read:ads"
    settings.ad_breaks_enabled = True
    assert ("channel.ad_break.begin", "1") in settings.event_subscriptions()
    bare = Settings({"server": {"host": "127.0.0.1", "port": 8080}})
    assert ("stream.online", "1") in bare.event_subscriptions()
    assert ("stream.offline", "1") in bare.event_subscriptions()
    assert ("channel.ad_break.begin", "1") not in bare.event_subscriptions()
    probe = make_orch(tmp_path, settings)
    probe.eventsub = SimpleNamespace(feed_status={"ad_breaks": "missing scope channel:read:ads"})
    assert _twitch_feeds(probe)["ad_breaks"] == "missing scope channel:read:ads"


def test_viewer_replies_are_threaded_and_alerts_are_not(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        await orch._decide_chat(ChatLine(user="bob", text="nice play", msg_id="abc-123"), "chat")
        assert orch.twitch.replies == ["abc-123"]
        await orch._decide_chat(ChatLine(user="testchannel", text="hey", msg_id="streamer-id"), "chat")
        assert orch.twitch.replies == ["abc-123"]

        event = _parse_notification(
            "channel.shoutout.receive",
            {"from_broadcaster_user_name": "Nova"},
            channel="testchannel",
        )
        settings.events_speak = False
        await orch.on_stream_event(event)
        raid = _parse_notification(
            "channel.raid",
            {
                "from_broadcaster_user_login": "testchannel",
                "to_broadcaster_user_name": "Other",
            },
            channel="testchannel",
        )
        assert raid.kind == "raid_out"
        await orch.on_stream_event(raid)
        assert len(orch.twitch.sent) == 4
        assert "Nova" in orch.twitch.sent[-2]
        assert "Other" in orch.twitch.sent[-1]
        assert orch.twitch.replies == ["abc-123"]

    asyncio.run(run())
    assert safe_reply_parent("ab c!") == "abc"
    assert format_reply_privmsg("Test", "ab c!", "hi") == "@reply-parent-msg-id=abc PRIVMSG #Test :hi"
    assert message_id(SimpleNamespace(tags={"id": "tag-9"})) == "tag-9"


def test_raw_privmsg_falls_back_without_a_socket():
    async def run():
        chat = TwitchChat("oauth:fake", "pixelbot", "testchannel", _noop, _noop)

        class Channel:
            name = "testchannel"

            def __init__(self):
                self.sent = []

            async def send(self, text):
                self.sent.append(text)

        class Socket:
            def __init__(self):
                self.raw = []

            async def send(self, text):
                self.raw.append(text)

        channel = Channel()
        socket = Socket()

        class Bot:
            def get_channel(self, _name):
                return channel

        bot = Bot()
        bot._ws = socket
        chat._bot = bot
        await chat._send_now("@bob hey", "parent-1")
        assert socket.raw == ["@reply-parent-msg-id=parent-1 PRIVMSG #testchannel :@bob hey"]
        assert channel.sent == []

        bot._ws = None
        await chat._send_now("@bob hey", "parent-1")
        assert channel.sent == ["@bob hey"]

    asyncio.run(run())


def test_clip_note_scope_and_cooldown(tmp_path, settings, monkeypatch):
    calls = {"n": 0}

    async def fake_clip(token, client_id, broadcaster_id):
        calls["n"] += 1
        assert broadcaster_id == "99"
        return {"id": "abc", "edit_url": "https://clips.twitch.tv/abc/edit"}

    monkeypatch.setattr("src.orchestrator.create_clip", fake_clip)
    settings.control_cooldown_seconds = 60
    settings.clip_cooldown_seconds = 60

    async def run():
        orch = make_orch(tmp_path, settings)
        orch.eventsub = _Events({"clips:edit"})
        orch._last_control_at = time.monotonic()
        line = ChatLine(user="testchannel", text="!pixel clip", command="!pixel", is_broadcaster=True)
        await orch._perform_chat(line, ignore_rate=False)
        assert calls["n"] == 1
        notes = orch.memory.viewer(STREAMER_THREAD).notes
        assert "https://clips.twitch.tv/abc/edit" in notes
        await orch._perform_chat(line, ignore_rate=False)
        assert calls["n"] == 1
        assert orch._last_skip["reason"] == "clip cooldown"

        orch.eventsub.scopes = set()
        orch._last_clip_at = 0
        with pytest.raises(RuntimeError, match="missing scope clips:edit"):
            await orch.create_clip()

        app = create_app(prepared=(settings, orch.hub, orch))
        with TestClient(app, base_url="http://127.0.0.1") as client:
            token = client.get("/api/session").json()["token"]
            headers = {"x-cohost-token": token, "origin": "http://127.0.0.1:8080"}
            missing = client.post("/api/clip", json={}, headers=headers)
            assert missing.status_code == 400
            assert missing.json()["error"] == "missing scope clips:edit"
            orch.eventsub.scopes = {"clips:edit"}
            created = client.post("/api/clip", json={}, headers=headers)
            assert created.status_code == 200
            assert created.json()["id"] == "abc"

    asyncio.run(run())


def test_ask_queue_order_answer_and_stale(tmp_path, settings):
    settings.ask_queue = True
    settings.chat_max_age_seconds = 1

    async def run():
        orch = make_orch(tmp_path, settings)
        now = time.monotonic()
        for text in ("!ask one", "!ask two", "!ask three"):
            orch._chat_pending.append(ChatLine(user="bob", text=text, command="!ask", ts=now))
        await orch._drain_chat()
        assert [line.text for line in orch._questions] == ["!ask one", "!ask two", "!ask three"]
        assert orch.twitch.sent == []
        assert orch.tts.spoken == []

        stale = ChatLine(user="sam", text="hello there friend", ts=now - 30)
        lingering = ChatLine(user="sam", text="!ask later", command="!ask", ts=now - 30)
        orch._chat_pending.append(stale)
        orch._chat_pending.append(lingering)
        await orch._expire_stale_chat()
        assert stale not in list(orch._chat_pending)
        assert lingering in list(orch._chat_pending)
        assert [line.text for line in orch._questions] == ["!ask one", "!ask two", "!ask three"]

        first = orch._questions[0].id
        await orch.answer_queued(first)
        assert orch.tts.spoken
        assert "bob" in orch.twitch.sent[-1].lower()
        assert [line.text for line in orch._questions] == ["!ask two", "!ask three"]

    asyncio.run(run())


def test_editable_notes_and_pinned_viewers(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        app = create_app(prepared=(settings, orch.hub, orch))
        with TestClient(app, base_url="http://127.0.0.1") as client:
            token = client.get("/api/session").json()["token"]
            headers = {"x-cohost-token": token, "origin": "http://127.0.0.1:8080"}
            added = client.post(
                "/api/memory/note",
                json={"login": "bob", "text": "likes cats", "op": "add"},
                headers=headers,
            )
            assert added.status_code == 200
            assert "likes cats" in orch.memory.prompt_block("viewer:bob")
            removed = client.post(
                "/api/memory/note",
                json={"login": "bob", "text": "likes cats", "op": "remove"},
                headers=headers,
            )
            assert removed.status_code == 200
            assert "likes cats" not in orch.memory.prompt_block("viewer:bob")
            client.post(
                "/api/memory/note",
                json={"login": "bob", "text": "", "op": "add", "pinned": True},
                headers=headers,
            )
            orch.memory.viewers["bob"].last_ts = 1
            orch.memory.add_note("gone", "bye")
            orch.memory.viewers["gone"].last_ts = 1
            orch.memory.viewers["gone"].pinned = False
            orch.memory.prune(now=time.time())
            assert "bob" in orch.memory.viewers
            assert "gone" not in orch.memory.viewers

    asyncio.run(run())


def test_shoutout_feed_is_optional_and_speaks_one_line(tmp_path, settings):
    bare = Settings({"server": {"host": "127.0.0.1", "port": 8080}})
    assert ("channel.raid.out", "1") not in bare.event_subscriptions()
    settings.shoutouts_enabled = True
    pairs = dict(settings.event_subscriptions())
    assert pairs["channel.shoutout.receive"] == "1"
    assert pairs["channel.raid.out"] == "1"
    assert pairs["channel.raid"] == "1"

    async def run():
        orch = make_orch(tmp_path, settings)
        settings.events_speak = False
        received = _parse_notification(
            "channel.shoutout.receive",
            {"from_broadcaster_user_name": "Nova"},
            channel="testchannel",
        )
        outgoing = _parse_notification(
            "channel.raid",
            {"from_broadcaster_user_login": "testchannel", "to_broadcaster_user_name": "Other"},
            channel="testchannel",
        )
        await orch.on_stream_event(received)
        await orch.on_stream_event(outgoing)
        assert len(orch.twitch.sent) == 2
        assert "Nova" in orch.twitch.sent[0]
        assert "Other" in orch.twitch.sent[1]

    asyncio.run(run())


async def _noop(*_args, **_kwargs):
    return None
