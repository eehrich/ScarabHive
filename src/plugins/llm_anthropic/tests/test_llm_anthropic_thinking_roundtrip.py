"""Anthropic extended-thinking blocks must round-trip unchanged.

Contract: when tool results are returned, the assistant's thinking /
redacted_thinking blocks have to be sent back **complete and unmodified**.
A PARTIAL echo is rejected with 400 ("...blocks in the latest assistant message
cannot be modified"), which makes a half-fix worse than none — hence the
explicit tests for the two tempting filters (empty text, type filtering).
"""
import pytest

from plugins.llm_anthropic.anthropic_client import AnthropicAsyncClient
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
    c.reasoning_details_mode = "keep_all"  # what __init__ resolves an unset entry to
    return c


class TestSerializeThinkingBlocks:
    def test_captures_thinking_with_signature(self, client):
        msg = _Message([
            _Block(type="thinking", thinking="weil ...", signature="SIG-1"),
            _Block(type="text", text="Antwort"),
        ])
        blocks = client._serialize_thinking_blocks(msg)
        assert blocks == [{"type": "thinking", "thinking": "weil ...", "signature": "SIG-1", "_after": 0}]

    def test_keeps_empty_text_blocks(self, client):
        """display='omitted' is the DEFAULT on Opus 5 / Sonnet 5 / Fable 5: the
        text is empty but the signature carries the encrypted reasoning.
        Dropping these is the classic partial-echo 400."""
        msg = _Message([_Block(type="thinking", thinking="", signature="SIG-EMPTY")])
        assert client._serialize_thinking_blocks(msg) == [
            {"type": "thinking", "thinking": "", "signature": "SIG-EMPTY", "_after": 0}
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

    def test_another_models_blocks_go_back_unchanged(self, client):
        """Anthropic drops what the model cannot read, unbilled, and a newer
        model reads an older one's; removing them cost that reasoning."""
        assert client._replayable_thinking(self._msg("claude-sonnet-5")) == [
            {"type": "thinking", "thinking": "t", "signature": "S"}]

    def test_an_unsigned_block_stays_home(self, client):
        """No signature arrived for it: sent, it is always a 400."""
        msg = ChatMessage(role="assistant", content="txt", thinking_model="claude-opus-5", thinking_blocks=[
            {"type": "thinking", "thinking": "a", "signature": ""},
            {"type": "redacted_thinking", "data": "R"}])
        assert client._replayable_thinking(msg) == [{"type": "redacted_thinking", "data": "R"}]

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

    @pytest.mark.parametrize("answer", [
        # a max_tokens stop right after a call ends on the next update
        [("thinking", "S1"), ("text", None), ("tool_use", "A"), ("thinking", "S2")],
        # a progress update before each tool_use it introduces
        [("thinking", "S1"), ("tool_use", "A"), ("thinking", "S2"), ("tool_use", "B")],
    ])
    def test_a_turn_goes_back_in_the_order_the_model_wrote_it(self, client, answer):
        """Captured by the real serializer, rebuilt by the real converter."""
        content = [_Block(type="thinking", thinking="", signature=v) if kind == "thinking"
                   else _Block(type="text", text="ok") if kind == "text"
                   else _Block(type="tool_use", id=v, name="f", input={}) for kind, v in answer]
        calls = [{"id": v, "type": "function", "function": {"name": "f", "arguments": "{}"}}
                 for kind, v in answer if kind == "tool_use"]
        msg = ChatMessage(role="assistant", content="ok" if ("text", None) in answer else "",
                          tool_calls=calls, thinking_model="claude-opus-5",
                          thinking_blocks=client._serialize_thinking_blocks(_Message(content)))

        _system, converted = client._convert_messages([msg])

        sent = [(b["type"], b.get("signature") or b.get("id")) for b in converted[0]["content"]]
        assert sent == answer
        assert not any("_after" in b for b in converted[0]["content"]), "a private key went out"

    def test_a_block_after_a_second_text_stays_before_its_call(self, client):
        """The turn's text is stored as one string: counting each text block put S2 behind B."""
        answer = [_Block(type="thinking", thinking="", signature="S1"), _Block(type="text", text="one"),
                  _Block(type="tool_use", id="A", name="f", input={}), _Block(type="text", text="two"),
                  _Block(type="thinking", thinking="", signature="S2"),
                  _Block(type="tool_use", id="B", name="f", input={})]
        calls = [{"id": v, "type": "function", "function": {"name": "f", "arguments": "{}"}} for v in "AB"]
        msg = ChatMessage(role="assistant", content="onetwo", tool_calls=calls, thinking_model="claude-opus-5",
                          thinking_blocks=client._serialize_thinking_blocks(_Message(answer)))

        _system, converted = client._convert_messages([msg])

        sent = [(b["type"], b.get("signature") or b.get("id")) for b in converted[0]["content"]]
        assert sent == [("thinking", "S1"), ("text", None), ("tool_use", "A"), ("thinking", "S2"), ("tool_use", "B")]

    def test_an_empty_text_block_takes_no_place(self, client):
        """The replay builds no block for empty text: counted, it put S2 behind B."""
        answer = [_Block(type="text", text=""), _Block(type="tool_use", id="A", name="f", input={}),
                  _Block(type="thinking", thinking="", signature="S2"),
                  _Block(type="tool_use", id="B", name="f", input={})]
        calls = [{"id": v, "type": "function", "function": {"name": "f", "arguments": "{}"}} for v in "AB"]
        msg = ChatMessage(role="assistant", content="", tool_calls=calls, thinking_model="claude-opus-5",
                          thinking_blocks=client._serialize_thinking_blocks(_Message(answer)))

        _system, converted = client._convert_messages([msg])

        sent = [(b["type"], b.get("signature") or b.get("id")) for b in converted[0]["content"]]
        assert sent == [("tool_use", "A"), ("thinking", "S2"), ("tool_use", "B")]

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


def _built(**kw):
    """A real constructor run — the declared keys are validated there."""
    from unittest.mock import MagicMock, patch
    with patch("anthropic.AsyncAnthropic", MagicMock()):
        return AnthropicAsyncClient(model=kw.pop("model", "claude-opus-5"),
                                    api_key="k", **kw)


class TestThinkingRequestShape:
    """Which thinking shape the model accepts is told by its entry -- not
    by its name. The wrong shape is an HTTP 400, not a silent loss."""

    def test_default_is_adaptive(self):
        """Every current Claude model refuses a budget with a 400."""
        assert _built(thinking_budget=4096)._build_thinking_param() == {"type": "adaptive"}

    def test_a_declared_budget_form_sends_the_budget(self):
        assert _built(thinking_request_shape="budget", thinking_budget=4096)._build_thinking_param() == {
            "type": "enabled", "budget_tokens": 4096}

    def test_budget_falls_back_to_the_house_default(self):
        assert _built(thinking_request_shape="budget")._build_thinking_param() == {
            "type": "enabled", "budget_tokens": 8192}

    def test_declared_adaptive_sends_no_budget(self):
        """budget_tokens would be the 400 here -- the field must not travel along,
        even if the entry carries a thinking_budget."""
        assert _built(thinking_request_shape="adaptive",
                      thinking_budget=16384)._build_thinking_param() == {"type": "adaptive"}

    def test_the_name_no_longer_decides(self):
        """Counter-check to the deleted name table: the same name, both
        shapes -- the declared key alone decides."""
        assert _built(model="claude-opus-5", thinking_request_shape="budget")._build_thinking_param()["type"] == "enabled"
        assert _built(model="claude-haiku-4-5-20251001",
                      thinking_request_shape="adaptive")._build_thinking_param() == {
            "type": "adaptive"}

    def test_an_undeclared_shape_fails_at_construction(self):
        with pytest.raises(ValueError, match="thinking_request_shape"):
            _built(thinking_request_shape="enabled")


class TestReasoningDetailsModeGovernsTheThinkingBlocks:
    """This client carries its reasoning in ``thinking_blocks`` instead of
    ``reasoning_details`` -- the declared mode still governs it."""

    @staticmethod
    def _history():
        def turn(text):
            return ChatMessage(
                role="assistant", content=text,
                thinking_blocks=[{"type": "thinking", "thinking": text, "signature": "S"}],
                thinking_model="claude-opus-5")
        return [ChatMessage(role="user", content="frage"), turn("erst"),
                ChatMessage(role="user", content="weiter"), turn("dann")]

    def _replayed(self, mode):
        client = _built(reasoning_details_mode=mode) if mode else _built()
        _system, converted = client._convert_messages(self._history())
        return [b["thinking"] for m in converted if isinstance(m["content"], list)
                for b in m["content"] if b.get("type") == "thinking"]

    def test_default_replays_all_of_them(self):
        assert self._replayed(None) == ["erst", "dann"]

    def test_keep_last_replays_only_the_open_turn(self):
        assert self._replayed("keep_last") == ["dann"]

    def test_strip_replays_none(self):
        assert self._replayed("strip") == []

    def test_the_text_of_a_stripped_turn_survives(self):
        """Only the blocks drop out, not the model's answer."""
        client = _built(reasoning_details_mode="strip")
        _system, converted = client._convert_messages(self._history())
        assert [m["content"] for m in converted if m["role"] == "assistant"] == ["erst", "dann"]

    def test_an_undeclared_mode_fails_at_construction(self):
        with pytest.raises(ValueError, match="reasoning_details_mode"):
            _built(reasoning_details_mode="keep_first")


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


class TestHistoryMutationDropsTheBoundBlocks:
    """A thinking block is bound to the conversation that produced it: an edit
    before it (compaction, summary) makes it invalid -- a 400 where Anthropic
    enforces the check. The mutation helper strips the older ones, and this
    client completes the reset on the orphaned latest turn: dropped, also when
    that turn still waits for its tools -- Claude with preserved thinking accepts a tool turn
    without its thinking, but not with a block from before the edit."""

    @staticmethod
    def _turn(text, tool_call_id=None):
        return ChatMessage(
            role="assistant", content=text,
            tool_calls=([{"id": tool_call_id, "type": "function",
                          "function": {"name": "f", "arguments": "{}"}}]
                        if tool_call_id else None),
            thinking_blocks=[{"type": "thinking", "thinking": text, "signature": "S"}],
            thinking_model="claude-opus-5")

    @staticmethod
    def _replayed(client, messages):
        _system, converted = client._convert_messages(messages)
        return [b["thinking"] for m in converted if isinstance(m["content"], list)
                for b in m["content"] if b.get("type") == "thinking"]

    def _history(self, open_tool_turn):
        last = (self._turn("dann", "t2") if open_tool_turn else self._turn("dann"))
        history = [ChatMessage(role="user", content="frage (compacted)"),
                   self._turn("erst", "t1"),
                   ChatMessage(role="tool", tool_call_id="t1", content="r1"),
                   last]
        if open_tool_turn:
            history.append(ChatMessage(role="tool", tool_call_id="t2", content="r2"))
        return history

    def test_every_block_behind_the_edit_is_gone_the_open_tool_turn_included(self, client):
        from agent_system.utils.reasoning_artifacts import invalidate_reasoning_artifacts
        history = self._history(open_tool_turn=True)
        assert self._replayed(client, history) == ["erst", "dann"], "fixture replays nothing"
        assert invalidate_reasoning_artifacts(history) == 2
        assert self._replayed(client, history) == []
        _system, converted = client._convert_messages(history)
        assert [b["type"] for b in converted[3]["content"]] == ["text", "tool_use"], \
            "the open turn keeps its text and tool call, only the thinking goes"

    def test_an_orphaned_finished_turn_loses_its_blocks_too(self, client):
        from agent_system.utils.reasoning_artifacts import invalidate_reasoning_artifacts
        history = self._history(open_tool_turn=False)
        invalidate_reasoning_artifacts(history)
        assert self._replayed(client, history) == []

    def test_turns_before_the_mutation_keep_their_blocks(self, client):
        """start: what came before the changed spot is unchanged history."""
        from agent_system.utils.reasoning_artifacts import invalidate_reasoning_artifacts
        history = [ChatMessage(role="user", content="frage"), self._turn("erst"),
                   ChatMessage(role="user", content="weiter (compacted)"), self._turn("mitte"),
                   ChatMessage(role="user", content="noch"), self._turn("dann", "t3"),
                   ChatMessage(role="tool", tool_call_id="t3", content="r3")]
        invalidate_reasoning_artifacts(history, start=2)
        assert self._replayed(client, history) == ["erst"]

    def test_the_orphan_rule_holds_in_keep_last_too(self):
        """keep_last only narrows which turns may replay; an orphaned one may not."""
        from agent_system.utils.reasoning_artifacts import invalidate_reasoning_artifacts
        keep_last = _built(reasoning_details_mode="keep_last")
        history = self._history(open_tool_turn=False)
        assert self._replayed(keep_last, history) == ["dann"], "fixture replays nothing"
        invalidate_reasoning_artifacts(history)
        assert self._replayed(keep_last, history) == []
