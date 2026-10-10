import asyncio
import json

import httpx

from src.health import snapshot
from src.twitch.auth import CORE_TYPES, missing_scope, validate
from src.twitch.eventsub import EventSubClient


async def _quiet(*_args, **_kwargs):
    return None


def test_event_subscription_versions_match_current_reference(settings):
    settings.bits_enabled = True
    settings.channel_points_enabled = True
    settings.hype_train_enabled = True
    settings.polls_enabled = True
    settings.predictions_enabled = True
    pairs = dict(settings.event_subscriptions())
    assert pairs["channel.follow"] == "2"
    assert pairs["channel.subscribe"] == "1"
    assert pairs["channel.subscription.gift"] == "1"
    assert pairs["channel.subscription.message"] == "1"
    assert pairs["channel.raid"] == "1"
    assert pairs["channel.cheer"] == "1"
    assert pairs["channel.channel_points_custom_reward_redemption.add"] == "1"
    # Version 1 of hype train was withdrawn on 15 Jan 2026. Twitch's reference lists 2.
    assert pairs["channel.hype_train.begin"] == "2"
    assert pairs["channel.hype_train.progress"] == "2"
    assert pairs["channel.hype_train.end"] == "2"
    assert pairs["channel.poll.begin"] == "1"
    assert pairs["channel.poll.end"] == "1"
    assert pairs["channel.prediction.begin"] == "1"
    assert pairs["channel.prediction.end"] == "1"


def test_ids_are_resolved_before_the_socket_opens():
    async def run():
        order = []
        client = EventSubClient("oauth:tok", "cid", "chan", _quiet, _quiet)

        async def refresh_auth():
            order.append("auth")

        async def resolve_ids():
            order.append("ids")
            client.broadcaster_id = "broadcaster"
            client.moderator_id = "moderator"

        async def _run():
            order.append("socket")
            client.connected = True
            client._ready.set()

        client.refresh_auth = refresh_auth
        client.resolve_ids = resolve_ids
        client._run = _run
        await client.connect()
        assert order == ["auth", "ids", "socket"]

    asyncio.run(run())


class _RecordingClient:
    def __init__(self, *args, **kwargs):
        self.posts: list[str] = []
        self.inflight = 0
        self.peak = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, headers=None, json=None):  # noqa: A002
        async with self._lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.posts.append(json["type"])
        await asyncio.sleep(0.05)
        async with self._lock:
            self.inflight -= 1
        return httpx.Response(202, json={"data": []})


def _client(subscriptions, scopes):
    client = EventSubClient("oauth:tok", "cid", "chan", _quiet, _quiet, subscriptions=subscriptions)
    client.broadcaster_id = "b"
    client.moderator_id = "m"
    client.scopes = scopes
    return client


_SUBS = [
    ("channel.follow", "2"),
    ("channel.subscribe", "1"),
    ("channel.subscription.gift", "1"),
    ("channel.subscription.message", "1"),
    ("channel.raid", "1"),
    ("channel.cheer", "1"),
]


def test_subscribe_posts_core_first_with_a_concurrency_cap(monkeypatch):
    recorder = _RecordingClient()

    def factory(*args, **kwargs):
        return recorder

    monkeypatch.setattr("src.twitch.eventsub.httpx.AsyncClient", factory)

    async def run():
        client = _client(_SUBS, {"moderator:read:followers", "channel:read:subscriptions", "bits:read"})
        await client._subscribe_all("session")
        return client

    client = asyncio.run(run())
    assert recorder.posts.index("channel.cheer") == 5
    assert recorder.posts[:5]
    assert all(name in CORE_TYPES for name in recorder.posts[:5])
    assert recorder.peak <= 4
    assert client.feed_status["follow"] == "ok"
    assert client.feed_status["bits"] == "ok"


def test_missing_bits_scope_skips_cheer_and_keeps_core(monkeypatch):
    recorder = _RecordingClient()
    monkeypatch.setattr("src.twitch.eventsub.httpx.AsyncClient", lambda *a, **k: recorder)
    assert missing_scope({"moderator:read:followers", "channel:read:subscriptions"}, "channel.cheer") == "bits:read"

    async def run():
        client = _client(_SUBS, {"moderator:read:followers", "channel:read:subscriptions"})
        await client._subscribe_all("session")
        return client

    client = asyncio.run(run())
    assert "channel.cheer" not in recorder.posts
    assert "channel.follow" in recorder.posts
    assert client.feed_status["bits"] == "missing scope bits:read"
    assert client.feed_status["follow"] == "ok"


def test_validate_never_returns_the_token():
    secret = "oauth:super-secret-token-value"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "OAuth super-secret-token-value"
        assert secret not in str(request.url)
        return httpx.Response(
            200,
            json={
                "login": "pixel",
                "client_id": "cid",
                "scopes": ["moderator:read:followers"],
                "expires_in": 4000,
                "user_id": "42",
            },
        )

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            return await validate(secret, client=http)

    meta = asyncio.run(run())
    blob = json.dumps(meta)
    assert "super-secret-token-value" not in blob
    assert "oauth:" not in blob
    assert meta["login"] == "pixel"
    assert meta["scopes"] == ["moderator:read:followers"]
    assert meta["expires_in"] == 4000
    assert set(meta) == {"login", "client_id", "scopes", "expires_in", "user_id"}


def test_validate_redacts_the_token_from_errors():
    secret = "oauth:super-secret-token-value"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": f"rejected {secret}"})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            await validate(secret, client=http)

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        assert "super-secret-token-value" not in str(exc)
        assert "oauth:" not in str(exc)
    else:
        raise AssertionError("validate should fail on HTTP 401")


def test_health_hides_tokens_and_reports_feeds(tmp_path, settings):
    from tests.test_orchestrator_policy import make_orch

    class _Wake:
        def status(self):
            return {"enabled": False}

    class _EventSub:
        def __init__(self):
            self.feed_status = {"bits": "missing scope bits:read", "follow": "ok"}
            self.expires_in = 3600
            self.token = settings.twitch_token

    async def run():
        orch = make_orch(tmp_path, settings)
        orch.mic.wake = _Wake()
        orch.eventsub = _EventSub()
        orch.tts.mode = "persistent"
        return await snapshot(settings, orch)

    payload = asyncio.run(run())
    blob = json.dumps(payload)
    assert settings.twitch_token not in blob
    assert "oauth:" not in blob.lower()
    assert "fake-token-not-real" not in blob
    assert "fake-event-token" not in blob
    assert payload["twitch"]["feeds"]["bits"] == "missing scope bits:read"
    assert payload["twitch"]["feeds"]["follow"] == "ok"
    assert payload["twitch"]["feeds"]["raid"] == "off"
    assert payload["tts"]["mode"] == "persistent"
    assert any("under 24 hours" in hint for hint in payload["setup"])


def test_expiry_hint_uses_time_since_validation(tmp_path, settings):
    import time

    from tests.test_orchestrator_policy import make_orch

    class _Wake:
        def status(self):
            return {"enabled": False}

    class _EventSub:
        def __init__(self):
            self.feed_status = {}
            self.expires_in = 48 * 60 * 60
            self.validated_at = time.time() - 30 * 60 * 60
            self.scopes: set[str] = set()

    async def run(client):
        orch = make_orch(tmp_path, settings)
        orch.mic.wake = _Wake()
        orch.eventsub = client
        return await snapshot(settings, orch)

    client = _EventSub()
    payload = asyncio.run(run(client))
    assert any("under 24 hours" in hint for hint in payload["setup"])
    assert payload["twitch"]["actions"]["shoutout"] == "missing scope moderator:manage:shoutouts"
    assert payload["twitch"]["actions"]["marker"] == "missing scope channel:manage:broadcast"

    client.validated_at = time.time()
    client.scopes = {"moderator:manage:shoutouts", "channel:manage:broadcast"}
    fresh = asyncio.run(run(client))
    assert not any("under 24 hours" in hint for hint in fresh["setup"])
    assert fresh["twitch"]["actions"]["shoutout"] == "ok"
    assert fresh["twitch"]["actions"]["marker"] == "ok"


def test_revalidate_401_rejects_the_event_token(tmp_path, settings, monkeypatch):
    from src.twitch.eventsub import EventSubClient
    from tests.test_orchestrator_policy import make_orch

    async def rejected(token, client=None):
        raise RuntimeError("token validate HTTP 401: nope")

    async def run():
        orch = make_orch(tmp_path, settings)
        client = EventSubClient("oauth:tok", "cid", "chan", _quiet, orch._eventsub_status)
        client.connected = True
        orch.eventsub = client
        monkeypatch.setattr("src.twitch.auth.validate", rejected)
        await orch._revalidate_event_token()
        assert client.fatal is True
        assert orch.hub.status.get("eventsub") == "token rejected"

        calls: list[str] = []

        async def mark():
            calls.append("checked")
            orch._stopping = True

        orch._stopping = False
        orch._revalidate_event_token = mark
        orch._want_events = True
        orch._token_check_at = 0.0
        orch.eventsub = client

        async def asleep(_delay):
            return None

        monkeypatch.setattr("src.orchestrator.asyncio.sleep", asleep)
        await orch._twitch_watchdog()
        assert calls == ["checked"]

    asyncio.run(run())


def test_irc_login_mismatch_and_rejected_token(tmp_path, settings, monkeypatch):
    from tests.test_orchestrator_policy import make_orch

    async def mismatch(token, client=None):
        return {
            "login": "someoneelse",
            "scopes": ["chat:read", "chat:edit"],
            "expires_in": 1000,
            "user_id": "9",
            "client_id": "cid",
        }

    async def missing_edit(token, client=None):
        return {
            "login": "pixelbot",
            "scopes": ["chat:read"],
            "expires_in": 1000,
            "user_id": "9",
            "client_id": "cid",
        }

    async def rejected(token, client=None):
        raise RuntimeError("token validate HTTP 401: nope")

    async def connect_ok():
        return None

    async def run():
        orch = make_orch(tmp_path, settings)
        orch._irc_connect = connect_ok
        monkeypatch.setattr("src.twitch.auth.validate", mismatch)
        await orch.connect_irc()
        assert orch.irc_auth_note == "IRC token login is someoneelse, expected pixelbot"
        logs = []

        async def capture(message):
            logs.append(message)

        orch.hub.broadcast = capture
        await orch.connect_irc()
        assert any("someoneelse" in str(item) for item in logs)

        monkeypatch.setattr("src.twitch.auth.validate", missing_edit)
        await orch.connect_irc()
        assert orch.irc_auth_note == "IRC token is missing chat:edit"

        monkeypatch.setattr("src.twitch.auth.validate", rejected)
        await orch.connect_irc()
        assert orch._irc_fatal is True
        assert orch.hub.status.get("twitch") == "token rejected"

    asyncio.run(run())
