import asyncio
import json

import pytest

from src.twitch.eventsub import KEEPALIVE_GRACE, EventSubClient


def _welcome(sid: str = "sid", keepalive: float = 10):
    return {
        "metadata": {"message_type": "session_welcome", "message_id": sid},
        "payload": {"session": {"id": sid, "keepalive_timeout_seconds": keepalive}},
    }


def _raid(message_id: str = "m1"):
    return {
        "metadata": {
            "message_type": "notification",
            "message_id": message_id,
            "subscription_type": "channel.raid",
        },
        "payload": {
            "event": {
                "from_broadcaster_user_login": "raider",
                "from_broadcaster_user_name": "Raider",
                "viewers": 4,
            }
        },
    }


class _Script:
    def __init__(self, client, items, events=None, name=""):
        self.client = client
        self.items = list(items)
        self.events = events
        self.name = name
        self.closed = False

    async def recv(self):
        if self.client._stop and not self.items:
            await asyncio.Future()
        if not self.items:
            self.client._stop = True
            return "{}"
        item = self.items.pop(0)
        if item == "HANG":
            await asyncio.Future()
        if isinstance(item, str):
            return item
        return json.dumps(item)

    async def close(self):
        self.closed = True
        if self.events is not None and self.name:
            self.events.append(f"close-{self.name}")


def _client(on_event, on_status):
    client = EventSubClient("oauth:tok", "cid", "chan", on_event, on_status)

    async def subscribe(_sid):
        return None

    client._subscribe_all = subscribe
    return client


def test_session_ignores_bad_json_and_duplicate_ids():
    seen: list[str] = []
    statuses: list[str] = []

    async def on_event(event):
        seen.append(event.user)

    async def on_status(state):
        statuses.append(state)

    async def run():
        client = _client(on_event, on_status)
        sock = _Script(client, [_welcome(), "not-json{", _raid("same"), _raid("same")])
        await client._read(sock, subscribe=True)

    asyncio.run(run())
    assert seen == ["Raider"]
    assert "connected" in statuses


def test_session_keepalive_timeout(monkeypatch):
    async def on_event(_event):
        return None

    async def on_status(_state):
        return None

    async def run():
        monkeypatch.setattr("src.twitch.eventsub.KEEPALIVE_GRACE", 0.05)
        client = _client(on_event, on_status)
        sock = _Script(client, [_welcome(keepalive=0.05), "HANG"])
        with pytest.raises(TimeoutError, match="keepalive timeout"):
            await client._read(sock, subscribe=True)

    asyncio.run(run())
    assert KEEPALIVE_GRACE == 3.0


def test_session_revocation_marks_the_feed():
    statuses: list[str] = []

    async def on_event(_event):
        return None

    async def on_status(state):
        statuses.append(state)

    async def run():
        client = _client(on_event, on_status)
        sock = _Script(
            client,
            [
                _welcome("rev"),
                {
                    "metadata": {"message_type": "revocation"},
                    "payload": {"subscription": {"type": "channel.raid", "status": "user_removed"}},
                },
            ],
        )
        await client._read(sock, subscribe=True)
        assert client.feed_status["raid"] == "revoked user_removed"

    asyncio.run(run())
    assert "revoked" in statuses


def test_reconnect_opens_the_new_socket_before_closing_the_old_one(monkeypatch):
    order: list[str] = []

    async def on_event(_event):
        return None

    async def on_status(_state):
        return None

    async def run():
        client = _client(on_event, on_status)

        async def connect(url, **_kwargs):
            if "rejoin" in url:
                order.append("open-new")
                sock = _Script(client, [_welcome("new")], order, "new")

                async def recv():
                    raw = await _Script.recv(sock)
                    client._stop = True
                    return raw

                sock.recv = recv
                return sock
            order.append("open-old")
            return _Script(
                client,
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

        monkeypatch.setattr("src.twitch.eventsub.websockets.connect", connect)
        await client._run()
        assert order.index("open-new") < order.index("close-old")

    asyncio.run(run())
