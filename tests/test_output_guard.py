import asyncio

from src.safety import clean_chat_output
from src.twitch.chat import ChatLine
from tests.test_orchestrator_policy import FakeLLM, make_orch


def test_clean_chat_output_strips_commands_links_and_caps():
    assert clean_chat_output("!addcom x") == "addcom x"
    assert clean_chat_output("/ban bob") == "ban bob"
    assert clean_chat_output("\\timeout bob") == "timeout bob"
    assert "http" not in clean_chat_output("look https://example.com/a?q=1 now").lower()
    assert "www." not in clean_chat_output("see www.example.com/path please").lower()
    kept = clean_chat_output("look https://example.com/a now", allow_links=True)
    assert "https://example.com/a" in kept
    assert clean_chat_output("hi @bob and @sam", target_user="bob") == "hi @bob and"
    assert clean_chat_output("!!!") == ""
    assert clean_chat_output("https://only.example/path") == ""

    words = ["purplefin"] * 40
    long_line = " ".join(words)
    assert len(long_line) > 300
    capped = clean_chat_output(long_line, max_chars=200)
    assert len(capped) <= 200
    assert capped.split()[-1] == "purplefin"
    assert clean_chat_output(capped, max_chars=200) == capped


def test_dashboard_say_strips_a_leading_slash(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        await orch.say_line("/hello chat")
        assert orch.twitch.sent == ["hello chat"]
        await orch.say_line("https://example.com/only")
        assert orch.twitch.sent == ["hello chat"]
        assert orch.hub.status["skip"]["reason"] == "output guard"

    asyncio.run(run())


def test_chat_reply_does_not_post_a_leading_command(tmp_path, settings):
    async def run():
        orch = make_orch(tmp_path, settings)
        orch.llm = FakeLLM("!addcom x")
        await orch._decide_chat(ChatLine(user="bob", text="do it"), "chat")
        assert orch.twitch.sent
        posted = orch.twitch.sent[0]
        assert posted.startswith("@bob")
        assert "!" not in posted
        assert "addcom" in posted

    asyncio.run(run())
