from src.identity import StreamerIdentity
from src.policy import chat_action, mention_waits
from src.twitch.chat import ChatLine, is_spammy
from src.twitch.eventsub import StreamEvent, _looks_like_auth, _parse_notification


def test_spam_and_engaging():
    assert is_spammy("!foo")
    assert is_spammy("https://example.com")
    assert not is_spammy("hey pixel")
    assert ChatLine(user="a", text="how are you?").is_engaging()
    assert not ChatLine(user="a", text="nice play").is_engaging()


def test_mention_boost_shortens_only_when_enabled():
    assert mention_waits(True, True, 12, 20) == (4.0, 8.0)
    assert mention_waits(True, False, 12, 20) == (12, 20)
    assert mention_waits(False, True, 12, 20) == (12, 20)


def test_chat_action_policy():
    assert chat_action(ChatLine(user="a", text="nice play")) == "chat"
    assert chat_action(ChatLine(user="a", text="hey", mention=True)) == "speak"
    assert chat_action(ChatLine(user="a", text="what was that?")) == "speak"
    assert chat_action(ChatLine(user="a", text="!so bob", command="!so")) == "command"
    assert chat_action(ChatLine(user="a", text="!pixel hello", command="!pixel")) == "speak"


def test_event_scenes_and_outgoing_raid():
    raid = StreamEvent(kind="raid", user="Nova", extra={"viewers": 12})
    assert "12" in raid.scene()
    assert "Nova" in raid.scene()
    assert "followed" in StreamEvent(kind="follow", user="A", users=["B", "C"]).scene()
    assert "100" in StreamEvent(kind="cheer", user="A", extra={"bits": 100}).scene()
    assert "gifted 5" in StreamEvent(kind="gift", user="A", extra={"total": 5}).scene()
    outgoing = _parse_notification(
        "channel.raid",
        {
            "from_broadcaster_user_login": "mychan",
            "from_broadcaster_user_name": "Me",
            "to_broadcaster_user_name": "Other",
            "to_broadcaster_user_login": "other",
            "viewers": 3,
        },
        channel="mychan",
    )
    assert outgoing is not None
    assert outgoing.kind == "raid_out"
    assert outgoing.user == "Other"
    assert "Other" in outgoing.scene()
    incoming = _parse_notification(
        "channel.raid",
        {"from_broadcaster_user_login": "nova", "from_broadcaster_user_name": "Nova", "viewers": 9},
        channel="mychan",
    )
    assert incoming is not None
    assert incoming.kind == "raid"


def test_timeout_is_not_treated_as_a_bad_token():
    assert not _looks_like_auth("EventSub did not become ready in 25s. Check Client-ID and event token scopes.")
    assert _looks_like_auth("subscribe HTTP 401: invalid oauth token")


def test_identity_login():
    ident = StreamerIdentity(display_name="Luna", twitch_login="luna_tv")
    assert ident.is_login("Luna_TV")
    assert ident.is_login("@luna_tv")
    assert not ident.is_login("someone")
    assert ident.spoken_name() == "Luna"
