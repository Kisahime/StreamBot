from pathlib import Path

from src.llm.persona import build_chat_prompt, build_voice_prompt, load_persona
from src.memory import STREAMER_THREAD, ConversationMemory

FIXTURE = Path(__file__).parent / "fixtures" / "persona.yaml"


def test_style_rules_and_names_are_in_the_prompt():
    persona = load_persona(FIXTURE)
    voice = build_voice_prompt(persona, "Luna")
    chat = build_chat_prompt(persona, "Luna")
    assert "Do not @ the streamer" in voice
    assert "Do not @ the streamer" in chat
    assert "Luna" in voice and "Luna" in chat
    assert "Pixel" in voice


def test_viewer_and_streamer_headers_differ(tmp_path):
    mem = ConversationMemory(tmp_path / "memory.json")
    mem.remember("viewer", "bob", "hello")
    viewer = mem.prompt_block("viewer:bob", streamer_name="Luna")
    streamer = mem.prompt_block(STREAMER_THREAD, streamer_name="Luna", is_voice=True)
    assert "NOT the streamer (Luna)" in viewer
    assert "STREAMER" in streamer
    assert "Luna" in streamer
