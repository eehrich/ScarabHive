"""Tests for reasoning-artifact invalidation on history mutation.

Invariant (utils/reasoning_artifacts.py): reasoning_details are
integrity-protected artifacts over the exact history. Any mutation
(compaction, summarization, eviction) invalidates them -- the helper
strips all but the last assistant message and flags that one as
`_rd_orphaned`, so the LLM client (mode-aware) can complete the
chain reset.
"""
from __future__ import annotations

from agent_system.utils.reasoning_artifacts import (
    RD_ORPHANED_FLAG,
    invalidate_reasoning_artifacts,
    strip_all_reasoning_artifacts,
    strip_foreign_reasoning_artifacts,
    strip_reasoning_artifacts_containing,
    thinking_text,
    thinking_text_from_details,
)


def _msgs():
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}],
         "reasoning_details": [{"type": "reasoning.encrypted", "id": "rs_1", "data": "X"}]},
        {"role": "tool", "tool_call_id": "a", "content": "r1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}],
         "reasoning_details": [{"type": "reasoning.encrypted", "id": "rs_2", "data": "Y"}]},
        {"role": "tool", "tool_call_id": "b", "content": "r2"},
    ]


class TestThinkingTextHasExactlyOneHome:
    """The thinking text is stored once — and must survive the artifacts.

    The artifacts are dropped on model switch, chain healing and history
    mutation. That is precisely the fallback/compaction moment where the
    model's own reasoning is worth keeping, so the text is rescued onto the
    message before its carrier is thrown away. Without that rescue, moving the
    text into the artifact would trade a doubled session for a silent loss.
    """

    def test_reads_flat_blocks(self):
        """Separate blocks are separate SECTIONS — joined, not glued.

        Measured over 95.399 stored sessions: a block is a finished section
        with its own heading that ends on punctuation, and in all 702
        multi-block messages at least one ends without trailing whitespace.
        Gluing them produced "...continuity.**Evaluating story...**". The
        fixture therefore carries no trailing space that could hide the
        separator — that is exactly how the defect stayed invisible.
        """
        assert thinking_text_from_details([
            {"type": "reasoning.text", "text": "First I check the file."},
            {"type": "reasoning.encrypted", "data": "OPAQUE"},
            {"type": "reasoning.summary", "summary": "**Then** I weigh it."},
        ]) == "First I check the file.\n\n**Then** I weigh it."

    def test_reads_the_verbatim_replay_block(self):
        """The Responses route nests its items one level deeper."""
        assert thinking_text_from_details([{
            "type": "reasoning.responses_items",
            "items": [
                {"type": "reasoning",
                 "content": [{"type": "reasoning_text", "text": "raw thought"}]},
                {"type": "message", "content": [{"type": "output_text",
                                                 "text": "the answer"}]},
            ],
        }]) == "raw thought"
        # The answer text is not thinking and must not leak in.
        assert "the answer" not in thinking_text_from_details([{
            "type": "reasoning.responses_items",
            "items": [{"type": "message",
                       "content": [{"type": "output_text", "text": "the answer"}]}],
        }])

    def test_message_field_wins_and_details_fill_in(self):
        assert thinking_text({"reasoning_content": "on the message"}) == "on the message"
        assert thinking_text({"reasoning_details": [
            {"type": "reasoning.text", "text": "in the artifact"}]}) == "in the artifact"
        assert thinking_text({"role": "assistant"}) == ""

    def test_strip_all_rescues_the_text(self):
        msgs = [{"role": "assistant", "content": "a", "reasoning_details": [
            {"type": "reasoning.text", "text": "why I did it"}]}]
        strip_all_reasoning_artifacts(msgs)
        assert "reasoning_details" not in msgs[0]
        assert msgs[0]["reasoning_content"] == "why I did it"

    def test_compaction_does_not_write_the_thinking_back(self):
        """After a compaction the text is deliberately NOT rescued.

        invalidate runs because the history grew too large; writing the
        thinking back would undo part of the saving that was just made, per
        message and for good (median 5.034 characters, max 373.776). A durable
        archive for reasoning belongs beside the session, not inside it.
        """
        msgs = [
            {"role": "assistant", "content": "a", "reasoning_details": [
                {"type": "reasoning.text", "text": "older thought"}]},
            {"role": "assistant", "content": "b", "reasoning_details": [
                {"type": "reasoning.text", "text": "latest thought"}]},
        ]
        invalidate_reasoning_artifacts(msgs)
        assert "reasoning_details" not in msgs[0]         # carrier dropped
        assert "reasoning_content" not in msgs[0]         # and NOT written back
        assert thinking_text(msgs[1]) == "latest thought"  # carrier kept

    def test_strip_containing_rescues_the_text(self):
        msgs = [{"role": "assistant", "content": "a", "reasoning_details": [
            {"type": "reasoning.text", "id": "rs_1", "text": "doomed carrier"}]}]
        strip_reasoning_artifacts_containing(msgs, "rs_1")
        assert msgs[0]["reasoning_content"] == "doomed carrier"

    def test_rescue_never_overwrites_an_existing_text(self):
        msgs = [{"role": "assistant", "content": "a",
                 "reasoning_content": "the real one",
                 "reasoning_details": [{"type": "reasoning.text", "text": "other"}]}]
        strip_all_reasoning_artifacts(msgs)
        assert msgs[0]["reasoning_content"] == "the real one"

    def test_rescue_stays_silent_without_text(self):
        """An encrypted-only artifact has nothing to rescue."""
        msgs = [{"role": "assistant", "content": "a", "reasoning_details": [
            {"type": "reasoning.encrypted", "data": "OPAQUE"}]}]
        strip_all_reasoning_artifacts(msgs)
        assert "reasoning_content" not in msgs[0]


class TestInvalidateReasoningArtifacts:
    def test_strips_older_flags_latest(self):
        msgs = _msgs()
        n = invalidate_reasoning_artifacts(msgs)
        assert n == 2  # one stripped + one flagged
        assert "reasoning_details" not in msgs[1]          # older: stripped
        assert "reasoning_details" in msgs[3]              # last: kept
        assert msgs[3][RD_ORPHANED_FLAG] is True           # but flagged

    def test_noop_without_artifacts(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "answer"},
        ]
        assert invalidate_reasoning_artifacts(msgs) == 0
        assert RD_ORPHANED_FLAG not in msgs[1]

    def test_noop_without_assistant(self):
        msgs = [{"role": "user", "content": "hi"}]
        assert invalidate_reasoning_artifacts(msgs) == 0

    def test_single_assistant_only_flagged(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "",
             "reasoning_details": [{"type": "reasoning.encrypted", "data": "X"}]},
        ]
        n = invalidate_reasoning_artifacts(msgs)
        assert n == 1
        assert "reasoning_details" in msgs[1]
        assert msgs[1][RD_ORPHANED_FLAG] is True

    def test_idempotent(self):
        msgs = _msgs()
        invalidate_reasoning_artifacts(msgs)
        # Second call: older ones are already gone, last one already flagged
        n2 = invalidate_reasoning_artifacts(msgs)
        assert n2 == 1  # last one is flagged again (harmless), nothing else
        assert "reasoning_details" in msgs[3]

    def test_object_messages(self):
        class M:
            def __init__(self, role, rd=None):
                self.role = role
                if rd is not None:
                    self.reasoning_details = rd

        older = M("assistant", rd=[{"data": "X"}])
        latest = M("assistant", rd=[{"data": "Y"}])
        msgs = [M("user"), older, M("tool"), latest]
        n = invalidate_reasoning_artifacts(msgs)
        assert n == 2
        assert getattr(older, "reasoning_details", None) is None
        assert latest.reasoning_details == [{"data": "Y"}]
        assert getattr(latest, RD_ORPHANED_FLAG) is True

    def test_strip_containing_targets_only_named_item(self):
        """Surgical healing: only the message with the defective item loses
        its artifacts (OpenRouter bridge bug with parallel tool_calls --
        all other items of the chain keep verifying)."""
        msgs = _msgs()
        n = strip_reasoning_artifacts_containing(msgs, "rs_1")
        assert n == 1
        assert "reasoning_details" not in msgs[1]
        assert "reasoning_details" in msgs[3]
        # unbekannte id: no-op
        assert strip_reasoning_artifacts_containing(msgs, "rs_zzz") == 0

    def test_strip_containing_pydantic_objects(self):
        from agent_system.llm.models import ChatMessage

        bad = ChatMessage(role="assistant", content="",
                          reasoning_details=[{"type": "reasoning.encrypted", "id": "rs_bad", "data": "X"}])
        good = ChatMessage(role="assistant", content="",
                           reasoning_details=[{"type": "reasoning.encrypted", "id": "rs_ok", "data": "Y"}])
        n = strip_reasoning_artifacts_containing([bad, good], "rs_bad")
        assert n == 1
        assert bad.reasoning_details is None
        assert good.reasoning_details == [{"type": "reasoning.encrypted", "id": "rs_ok", "data": "Y"}]

    def test_strip_all(self):
        msgs = _msgs()
        msgs[3][RD_ORPHANED_FLAG] = True
        n = strip_all_reasoning_artifacts(msgs)
        assert n == 2
        assert all("reasoning_details" not in m for m in msgs if m.get("role") == "assistant")
        assert RD_ORPHANED_FLAG not in msgs[3]

    def test_pydantic_chatmessage_objects(self):
        """The summarizer works on real ChatMessage objects (pydantic):
        fields cannot be deleted (delattr forbidden) -- the helper must set
        them to None; rd_orphaned is a declared field and gets set."""
        from agent_system.llm.models import ChatMessage

        older = ChatMessage(role="assistant", content="",
                            reasoning_details=[{"type": "reasoning.encrypted", "data": "X"}])
        latest = ChatMessage(role="assistant", content="",
                             reasoning_details=[{"type": "reasoning.encrypted", "data": "Y"}])
        msgs = [ChatMessage(role="user", content="hi"), older,
                ChatMessage(role="tool", content="r", tool_call_id="a"), latest]
        n = invalidate_reasoning_artifacts(msgs)
        assert n == 2
        assert older.reasoning_details is None
        assert latest.reasoning_details == [{"type": "reasoning.encrypted", "data": "Y"}]
        assert latest.rd_orphaned is True
        # Serialization: the flag is included by model_dump(exclude_none=True),
        # so the LLM client still sees it after the session round trip
        dumped = latest.model_dump(exclude_none=True)
        assert dumped.get("rd_orphaned") is True
        assert older.model_dump(exclude_none=True).get("rd_orphaned") is None


class TestStripForeignReasoningArtifacts:
    def _tagged(self, *tags):
        msgs = _msgs()
        msgs[1]["reasoning_model"], msgs[3]["reasoning_model"] = tags
        return msgs

    def test_one_foreign_artifact_resets_all(self):
        msgs = self._tagged("big", "small")
        assert strip_foreign_reasoning_artifacts(msgs, "big") == 2
        assert all("reasoning_details" not in m for m in msgs)

    def test_own_and_untagged_artifacts_stay(self):
        msgs = self._tagged("big", None)
        assert strip_foreign_reasoning_artifacts(msgs, "big") == 0
        assert msgs[1]["reasoning_details"] and msgs[3]["reasoning_details"]

    def test_a_tag_whose_artifacts_are_gone_is_no_reason(self):
        """invalidate_reasoning_artifacts drops the details and leaves the tag."""
        msgs = self._tagged("small", "big")
        del msgs[1]["reasoning_details"]
        assert strip_foreign_reasoning_artifacts(msgs, "big") == 0
        assert msgs[3]["reasoning_details"]

    def test_a_client_without_a_model_name_strips_nothing(self):
        msgs = self._tagged("small", "small")
        assert strip_foreign_reasoning_artifacts(msgs, None) == 0
