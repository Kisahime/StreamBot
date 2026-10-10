from src.memory import STREAMER_THREAD, ConversationMemory


def test_viewer_messages_are_not_double_counted(tmp_path):
    mem = ConversationMemory(tmp_path / "memory.json", viewer_limit=4, thread_turns=6)
    mem.remember("viewer", "Bob", "hi")
    assert mem.viewers["bob"].messages == 1
    mem.remember("viewer", "Bob", "i main mercy")
    assert mem.viewers["bob"].messages == 2
    assert any("mercy" in note.lower() for note in mem.viewers["bob"].notes)


def test_viewer_trim_drops_oldest(tmp_path):
    mem = ConversationMemory(tmp_path / "memory.json", viewer_limit=2, thread_turns=4)
    for name in ("ann", "bea", "cy"):
        mem.remember("viewer", name, "hello")
    extras = [key for key in mem.viewers if key != STREAMER_THREAD]
    assert len(extras) == 2
    assert "ann" not in mem.viewers


def test_prompt_block_does_not_repeat_the_thread(tmp_path):
    mem = ConversationMemory(tmp_path / "memory.json")
    mem.remember("streamer_voice", "Luna", "unique-mic-phrase", via="voice", thread=STREAMER_THREAD)
    block = mem.prompt_block(STREAMER_THREAD, streamer_name="Luna", is_voice=True)
    assert "unique-mic-phrase" not in block
    assert "STREAMER" in block
    msgs = mem.llm_messages(STREAMER_THREAD)
    assert any("unique-mic-phrase" in msg["content"] for msg in msgs)


def test_viewer_header_differs_from_streamer(tmp_path):
    mem = ConversationMemory(tmp_path / "memory.json")
    mem.remember("viewer", "bob", "first time here")
    viewer = mem.prompt_block("viewer:bob", streamer_name="Luna")
    streamer = mem.prompt_block(STREAMER_THREAD, streamer_name="Luna", is_voice=True)
    assert "NOT the streamer" in viewer
    assert "Luna" in viewer and "Luna" in streamer
    assert "STREAMER" in streamer
    assert "First time" in viewer


def test_corrupt_memory_is_kept_aside(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text("{not json", encoding="utf-8")
    mem = ConversationMemory(path)
    assert mem.viewers == {}
    assert (tmp_path / "memory.json.bad").exists()
