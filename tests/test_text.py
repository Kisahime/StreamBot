from src.memory import (
    apply_catchphrase_budget,
    ensure_chat_mention,
    for_speech,
    is_generic_filler,
    sanitize_reply,
)
from src.sentences import take_sentence


def test_sanitize_strips_json_and_labels():
    raw = '```json\n{"reply": "hey chat"}\n```'
    assert sanitize_reply(raw) == "hey chat"
    assert sanitize_reply("action: speak\nwait what") == "wait what"


def test_generic_filler_uses_persona_phrases():
    assert is_generic_filler("that's a great question")
    assert is_generic_filler("yo great question friend", ["great question"])
    assert is_generic_filler("Hey lunaversevt! Any clips or moments that caught your eye today?")
    assert not is_generic_filler("pretty good, parked on the couch. you?")
    assert not is_generic_filler("that boss again")


def test_similar_replies_and_smalltalk_fallback():
    from src.memory import replies_too_similar, streamer_smalltalk_fallback

    assert replies_too_similar(
        "Hey luna! Any clips or moments that caught your eye today?",
        "Hey luna! Clip or moment or something caught your eye?",
    )
    assert streamer_smalltalk_fallback("how are you?") == "pretty good, parked on the couch. you?"
    assert streamer_smalltalk_fallback("what is your name?") == "Cohost."
    assert is_generic_filler("hey lunaversevt—got any good memes?")
    assert streamer_smalltalk_fallback("I just died again") == ""


def test_mention_and_speech_cleanup():
    assert ensure_chat_mention("hey", "maohime") == "@maohime hey"
    assert ensure_chat_mention("@maohime hey", "maohime") == "@maohime hey"
    assert ensure_chat_mention("hey chat", "maohime") == "hey chat"
    spoken = for_speech("@maohime check https://example.com LUL !foo")
    assert "@" not in spoken
    assert "http" not in spoken
    assert "LUL" not in spoken
    assert "!foo" not in spoken
    assert "maohime" in spoken
    assert "check" in spoken


def test_catchphrase_budget():
    used = {"i heard that, chat.": 1_000.0}
    out = apply_catchphrase_budget(
        "I heard that, chat. anyway",
        ["I heard that, chat."],
        used,
        window=600,
        now=1_100.0,
    )
    assert "heard that" not in out.lower()


def test_sentence_split_keeps_titles_and_ellipsis():
    sent, rest = take_sentence("Mr. Smith died again. Then he laughed.", flush=False)
    assert sent == "Mr. Smith died again."
    assert rest.startswith("Then")
    sent, rest = take_sentence("wait...", flush=True)
    assert sent == "wait..."
    assert rest == ""
