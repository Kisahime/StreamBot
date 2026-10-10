import asyncio
from types import SimpleNamespace

import httpx

from src.twitch.chat import ChatLine, author_flags
from src.twitch.helix import HelixError, shoutout
from tests.test_orchestrator_policy import make_orch


def test_author_flags_from_attrs_and_badges():
    assert author_flags(None) == (False, False)
    assert author_flags(SimpleNamespace(is_mod=True, is_broadcaster=False)) == (True, False)
    assert author_flags(SimpleNamespace(is_mod=False, is_broadcaster=True)) == (False, True)
    badges = author_flags(SimpleNamespace(badges={"broadcaster": "1", "moderator": "1"}))
    assert badges == (True, True)


def test_viewer_so_is_dropped_and_mod_so_is_cooled_down(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        calls: list[str | None] = []

        async def fake_shoutout(login=None):
            calls.append(login)

        orch.shoutout = fake_shoutout
        viewer = ChatLine(user="bob", text="!so sam", command="!so")
        orch._chat_pending.append(viewer)
        assert await orch._consider_chat_locked() is True
        assert calls == []
        assert orch._last_skip["reason"] == "not a mod"

        mod = ChatLine(user="moddy", text="!so sam", command="!so", is_mod=True)
        orch._chat_pending.append(mod)
        assert await orch._consider_chat_locked() is True
        assert calls == ["sam"]

        orch._last_chat_reply = 0
        orch._user_reply_at.clear()
        orch._reply_times.clear()
        again = ChatLine(user="caster", text="!so sam", command="!so", is_broadcaster=True)
        orch._chat_pending.append(again)
        assert await orch._consider_chat_locked() is True
        assert calls == ["sam"]
        assert orch._last_skip["reason"] == "shoutout cooldown"

    asyncio.run(run())


def test_dashboard_shoutout_does_not_need_a_mod_badge(tmp_path, settings, monkeypatch):
    async def run():
        orch = make_orch(tmp_path, settings)
        orch.eventsub = SimpleNamespace(broadcaster_id="11", moderator_id="22")
        seen: list[dict] = []

        async def fake_user_id(token, client_id, login):
            return "99"

        async def fake_shoutout(token, client_id, **kwargs):
            seen.append(kwargs)

        monkeypatch.setattr("src.orchestrator.user_id", fake_user_id)
        monkeypatch.setattr("src.orchestrator.shoutout", fake_shoutout)
        await orch.shoutout("sam")
        assert seen == [
            {
                "from_broadcaster_id": "11",
                "to_broadcaster_id": "99",
                "moderator_id": "22",
            }
        ]
        assert any(item.startswith("sam, welcome in.") or item == "sam, welcome in." for item in orch.twitch.sent)

    asyncio.run(run())


def test_helix_429_is_a_clear_rate_limit_error():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="slow down")

        transport = httpx.MockTransport(handler)

        class Client(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        # shoutout builds its own client; patch the constructor it calls.
        import src.twitch.helix as helix

        original = helix.httpx.AsyncClient
        helix.httpx.AsyncClient = Client
        try:
            try:
                await shoutout(
                    "oauth:fake-token-not-real",
                    "client",
                    from_broadcaster_id="1",
                    to_broadcaster_id="2",
                    moderator_id="3",
                )
            except HelixError as exc:
                assert exc.status == 429
                assert "HTTP 429" in str(exc)
                assert "rate limited" in str(exc).lower()
            else:
                raise AssertionError("expected HelixError")
        finally:
            helix.httpx.AsyncClient = original

    asyncio.run(run())
