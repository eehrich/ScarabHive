"""Anthropic extended-thinking blocks must round-trip unchanged.

Contract: when tool results are returned, the assistant's thinking /
redacted_thinking blocks have to be sent back **complete and unmodified**.
A PARTIAL echo is rejected with 400 ("...blocks in the latest assistant message
cannot be modified"), which makes a half-fix worse than none — hence the
explicit tests for the two tempting filters (empty text, type filtering).
"""
import pytest

from agent_system.llm.anthropic_client import AnthropicAsyncClient
from agent_system.llm.models import ChatMessage


class _Block:
    """Stand-in for an SDK content block (has .type and .model_dump)."""

    def __init__(self, **data):
        self._data = data
        self.type = data.get("type")

    def model_dump(self, mode="json", exclude_none=True):
        return {k: v for k, v in self._data.items() if not (exclude_none and v is None)}


class _Message:
    def __init__(self, content):
        self.content = content


@pytest.fixture
def client():
    """Client instance without touching the SDK constructor."""
    c = AnthropicAsyncClient.__new__(AnthropicAsyncClient)
    c.model = "claude-opus-5"
    c.enable_prompt_caching = False
    c.prompt_cache_mode = None
    c.reasoning_details_mode = None
    return c


class TestSerializeThinkingBlocks:
    def test_captures_thinking_with_signature(self, client):
        msg = _Message([
            _Block(type="thinking", thinking="weil ...", signature="SIG-1"),
            _Block(type="text", text="Antwort"),
        ])
        blocks = client._serialize_thinking_blocks(msg)
        assert blocks == [{"type": "thinking", "thinking": "weil ...", "signature": "SIG-1"}]

    def test_keeps_empty_text_blocks(self, client):
        """display='omitted' is the DEFAULT on Opus 5 / Sonnet 5 / Fable 5: the
        text is empty but the signature carries the encrypted reasoning.
        Dropping these is the classic partial-echo 400."""
        msg = _Message([_Block(type="thinking", thinking="", signature="SIG-EMPTY")])
        assert client._serialize_thinking_blocks(msg) == [
            {"type": "thinking", "thinking": "", "signature": "SIG-EMPTY"}
        ]

    def test_keeps_redacted_thinking(self, client):
        msg = _Message([
            _Block(type="redacted_thinking", data="ENCRYPTED"),
            _Block(type="thinking", thinking="x", signature="S"),
        ])
        types = [b["type"] for b in client._serialize_thinking_blocks(msg)]
        assert types == ["redacted_thinking", "thinking"]  # order preserved

    def test_ignores_non_thinking_blocks(self, client):
        msg = _Message([
            _Block(type="text", text="hi"),
            _Block(type="tool_use", id="t1", name="f", input={}),
        ])
        assert client._serialize_thinking_blocks(msg) == []

    def test_empty_or_missing_content_is_safe(self, client):
        assert client._serialize_thinking_blocks(_Message(None)) == []
        assert client._serialize_thinking_blocks(_Message([])) == []


class TestReplayGuard:
    def _msg(self, model="claude-opus-5"):
        return ChatMessage(
            role="assistant", content="txt",
            thinking_blocks=[{"type": "thinking", "thinking": "t", "signature": "S"}],
            thinking_model=model,
        )

    def test_replays_for_same_model(self, client):
        assert len(client._replayable_thinking(self._msg())) == 1

    def test_skips_after_model_switch(self, client):
        """Fallback chains move messages between models. Another model ignores
        foreign signatures but still bills them as input."""
        assert client._replayable_thinking(self._msg("claude-sonnet-5")) == []

    def test_no_blocks_is_empty(self, client):
        assert client._replayable_thinking(ChatMessage(role="assistant", content="x")) == []

    def test_returns_a_copy_not_the_stored_list(self, client):
        msg = self._msg()
        client._replayable_thinking(msg)[0]["thinking"] = "MUTATED"
        assert msg.thinking_blocks[0]["thinking"] == "t"


class TestOutgoingOrder:
    def test_thinking_precedes_text_and_tool_use(self, client):
        msg = ChatMessage(
            role="assistant", content="Ich rufe ein Tool auf",
            tool_calls=[{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}],
            thinking_blocks=[{"type": "thinking", "thinking": "t", "signature": "S"}],
            thinking_model="claude-opus-5",
        )
        _system, converted = client._convert_messages([msg])
        types = [b["type"] for b in converted[0]["content"]]
        assert types == ["thinking", "text", "tool_use"]

    def test_assistant_without_tool_calls_keeps_blocks(self, client):
        msg = ChatMessage(
            role="assistant", content="Finale Antwort",
            thinking_blocks=[{"type": "thinking", "thinking": "t", "signature": "S"}],
            thinking_model="claude-opus-5",
        )
        _system, converted = client._convert_messages([msg])
        types = [b["type"] for b in converted[0]["content"]]
        assert types == ["thinking", "text"]

    def test_blocks_without_content_do_not_create_empty_message(self, client):
        msg = ChatMessage(
            role="assistant", content="",
            thinking_blocks=[{"type": "thinking", "thinking": "t", "signature": "S"}],
            thinking_model="claude-opus-5",
        )
        _system, converted = client._convert_messages([msg])
        assert converted[0]["content"] == ""

    def test_payload_unchanged_without_thinking_blocks(self, client):
        """Every legacy model and every message we have today must serialize
        exactly as before."""
        msg = ChatMessage(
            role="assistant", content="nur text",
            tool_calls=[{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}],
        )
        _system, converted = client._convert_messages([msg])
        types = [b["type"] for b in converted[0]["content"]]
        assert types == ["text", "tool_use"]

    def test_user_messages_are_untouched(self, client):
        _system, converted = client._convert_messages([
            ChatMessage(role="user", content="frage")
        ])
        assert converted[0] == {"role": "user", "content": "frage"}


class TestTransport:
    def test_chatmessage_declares_the_fields(self):
        """Undeclared keys are dropped by pydantic on ChatMessage(**dict) —
        session load and history replay would silently lose them."""
        blocks = [{"type": "thinking", "thinking": "t", "signature": "S"}]
        m = ChatMessage(role="assistant", content="x",
                        thinking_blocks=blocks, thinking_model="claude-opus-5")
        assert m.thinking_blocks == blocks and m.thinking_model == "claude-opus-5"

    def test_survives_session_roundtrip(self):
        m = ChatMessage(role="assistant", content="x",
                        thinking_blocks=[{"type": "thinking", "thinking": "t",
                                          "signature": "S"}],
                        thinking_model="claude-opus-5")
        restored = ChatMessage(**m.model_dump())
        assert restored.thinking_blocks == m.thinking_blocks
        assert restored.thinking_model == "claude-opus-5"
