"""Tests for ``Agent._select_llm_messages``.

This helper picks which message list to send to the LLM at each step,
resolving the conflict between:

- ``modified_messages`` (returned by the pre_llm_call hook chain)
- ``compacted_messages`` (persistence marker on the session tracker)

The pre-fix code rebuilt ``[leading systems from pre-hook] + compacted``
whenever ``compacted_messages`` was set — which silently dropped any
hook-injected system messages because the pre-hook leading-system block
never saw them. That bug surfaced when v5b synopsis moderator looped 100×
on ``context_engineer.recall()`` because the debate_forum pinned
``role=system`` block never reached the LLM.

Helper contract:
- Hook chain modified the list (new identity) → use modified_messages
  directly (covers injection AND modern compaction).
- Else compacted_messages set → legacy rebuild + clear marker.
- Else: pre_hook_messages as-is.
"""

from __future__ import annotations

import pytest

from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.server import Agent


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _msgs(*specs):
    """Build ChatMessage list from (role, content) tuples."""
    return [ChatMessage(role=r, content=c) for r, c in specs]


@pytest.fixture
def base_messages():
    """A typical pre-hook message list: base system prompt + conversation."""
    return _msgs(
        ("system", "You are a helpful assistant."),
        ("user", "Hello"),
    )


# ---------------------------------------------------------------------------
# 1. Hook modified the list — modern path, modified_messages wins
# ---------------------------------------------------------------------------

class TestModernPath:
    """When hooks return a new list, that list goes to the LLM verbatim."""

    def test_pure_injection_hook_chain_passes_through(self, base_messages):
        """debate_forum-style: injects a role=system message via hook chain."""
        modified = _msgs(
            ("system", "You are a helpful assistant."),
            ("system", "## Debate Forum – Channel #3055\nPinned content..."),
            ("user", "Hello"),
        )
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=modified,
            compacted_messages=None,
        )
        assert selected is modified
        # No compacted marker was set → nothing to clear, but the contract
        # is "modern path always returns clear=True" (caller is idempotent).
        assert clear is True
        # Pinned system message survives → LLM sees it
        assert len(selected) == 3
        assert selected[1].role == "system"
        assert "Pinned content" in selected[1].content

    def test_b171_regression_pinned_role_system_survives(self, base_messages):
        """Regression test for B171/B172: v5b synopsis moderator loop.

        After commit a35b0021, debate_forum injects pinned content with
        ``role=system``. ``auto_sync_session_messages`` then sets
        ``compacted_messages`` as a side-effect (filtering system msgs out
        per persistence policy). Pre-fix server.py rebuilt the LLM-call
        list from ``[pre-hook leading-systems] + compacted_messages``,
        silently dropping the injected pinned-system. Moderator agents
        couldn't see the channel task and looped 100× on
        ``context_engineer.recall()`` searching for the missing context.
        """
        pinned = ChatMessage(
            role="system",
            content="## Debate Forum – Channel #3055\nStory-Idee: ...",
            injected_by="debate_forum",
        )
        modified = _msgs(
            ("system", "You are a helpful assistant."),
        ) + [pinned] + _msgs(
            ("user", "Channel-ID: 3055"),
        )
        # auto_sync sets compacted_messages with system-msgs filtered out
        compacted_after_filter = _msgs(
            ("user", "Channel-ID: 3055"),
        )

        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=modified,
            compacted_messages=compacted_after_filter,
        )
        # Modern path wins — pinned must survive
        assert selected is modified
        # AND the auto_sync-set compacted marker must be cleared, else
        # the post-tool reconstruction misreads it as "tool modified
        # session" and drops the assistant tool-call message → store_fact
        # loop regression.
        assert clear is True
        # Verify the pinned message is in the selected list
        pinned_msgs = [m for m in selected if "Debate Forum" in (m.content or "")]
        assert len(pinned_msgs) == 1
        assert pinned_msgs[0].role == "system"

    def test_multiple_hooks_inject_multiple_systems(self, base_messages):
        """Pinned + SAM context + restoration all reach the LLM."""
        modified = _msgs(
            ("system", "You are a helpful assistant."),
            ("system", "## Debate Forum – Channel #3055\nPinned..."),
            ("system", "## Active Sub-Agents\nautor_1: ..."),
            ("system", "# Context Engineer - Stored Information\n..."),
            ("user", "Hello"),
        )
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=modified,
            compacted_messages=_msgs(("user", "Hello")),
        )
        assert selected is modified
        assert clear is True
        # All injected systems present
        sys_msgs = [m for m in selected if m.role == "system"]
        assert len(sys_msgs) == 4

    def test_compaction_hook_modern_path(self, base_messages):
        """context_engineer compacts conversation via context.messages
        (not via set_compacted_messages-only). Modified path wins."""
        long_history = _msgs(
            ("system", "You are a helpful assistant."),
            ("user", "msg 1"), ("assistant", "reply 1"),
            ("user", "msg 2"), ("assistant", "reply 2"),
        )
        # Compacted: archived_ref + recent turn
        compacted_view = _msgs(
            ("system", "You are a helpful assistant."),
            ("system",
             '{"type": "archived_ref", "summary": "Earlier turns archived"}'),
            ("user", "msg 2"), ("assistant", "reply 2"),
        )
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=long_history,
            modified_messages=compacted_view,
            compacted_messages=compacted_view[1:],  # auto_sync version
        )
        assert selected is compacted_view
        assert clear is True
        # archived_ref preserved
        assert any('"archived_ref"' in (m.content or "") for m in selected)


# ---------------------------------------------------------------------------
# 2. Legacy path — set_compacted_messages without modifying context.messages
# ---------------------------------------------------------------------------

class TestLegacyPath:
    """Old hook pattern: only ``set_compacted_messages`` is set, no
    ``context.messages`` change. Reconstruct + clear marker."""

    def test_legacy_compaction_rebuilds_from_pre_hook_systems(self, base_messages):
        """When hooks didn't modify the list, fall back to legacy rebuild."""
        compacted = _msgs(
            ("user", "compacted conversation msg"),
        )
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=base_messages,  # SAME identity → no modification
            compacted_messages=compacted,
        )
        # Reconstructed: [system from pre_hook] + compacted
        assert clear is True
        assert len(selected) == 2
        assert selected[0].role == "system"
        assert selected[0].content == "You are a helpful assistant."
        assert selected[1].content == "compacted conversation msg"

    def test_legacy_preserves_all_leading_systems(self):
        """Multiple leading system messages (agent prompt + tools_msg +
        session hooks) all survive the rebuild."""
        pre_hook = _msgs(
            ("system", "Base agent prompt"),
            ("system", "Tool definitions"),
            ("system", "Session hook context"),
            ("user", "Hello"),
            ("assistant", "Hi"),
        )
        compacted = _msgs(("user", "new conversation"))
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=pre_hook,
            modified_messages=pre_hook,
            compacted_messages=compacted,
        )
        assert clear is True
        assert len(selected) == 4  # 3 systems + 1 compacted user
        assert selected[0].content == "Base agent prompt"
        assert selected[1].content == "Tool definitions"
        assert selected[2].content == "Session hook context"
        assert selected[3].content == "new conversation"

    def test_legacy_empty_compacted_still_rebuilds(self, base_messages):
        compacted = []
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=base_messages,
            compacted_messages=compacted,
        )
        # System messages preserved, no conversation
        assert clear is True
        assert len(selected) == 1
        assert selected[0].role == "system"


# ---------------------------------------------------------------------------
# 3. No-op path — nothing was modified
# ---------------------------------------------------------------------------

class TestNoOpPath:
    """No hook modified anything, no compaction marker. Pass through."""

    def test_no_modification_no_compaction(self, base_messages):
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=base_messages,  # same identity
            compacted_messages=None,
        )
        assert selected is base_messages
        assert clear is False

    def test_modified_none_no_compaction(self, base_messages):
        """Hook chain returned ``None`` for modified_messages
        (defensive: shouldn't happen in practice, but no crash)."""
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=None,
            compacted_messages=None,
        )
        assert selected is base_messages
        assert clear is False


# ---------------------------------------------------------------------------
# 4. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Misc safety checks."""

    def test_empty_pre_hook_messages(self):
        # Two empty list literals have different identities → counts as
        # "hook returned a new list" → modern path → clear=True.
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=[],
            modified_messages=[],
            compacted_messages=None,
        )
        assert selected == []
        assert clear is True

    def test_modified_messages_is_new_empty_list(self, base_messages):
        """Hook chain returned [] (different identity). Still modern path."""
        modified = []
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=modified,
            compacted_messages=None,
        )
        # Hook returned empty list — that's what gets passed through
        assert selected is modified
        # Modern path always clears (caller is idempotent if marker absent)
        assert clear is True

    def test_legacy_skips_after_first_non_system(self):
        """Reconstruction stops at first non-system message in pre-hook list.
        System messages AFTER a non-system are NOT preserved (would be
        invalid LLM message order anyway)."""
        pre_hook = _msgs(
            ("system", "Base"),
            ("user", "Hello"),
            ("system", "Mid-conversation system (should not happen)"),
            ("assistant", "Hi"),
        )
        compacted = _msgs(("user", "compacted"))
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=pre_hook,
            modified_messages=pre_hook,
            compacted_messages=compacted,
        )
        # Only the leading "Base" system is kept; mid-system is dropped
        assert clear is True
        assert len(selected) == 2
        assert selected[0].content == "Base"
        assert selected[1].content == "compacted"

    def test_dict_messages_compatible(self):
        """Pre-hook messages can be dicts (not just ChatMessage objects)."""
        pre_hook = [
            {"role": "system", "content": "Base"},
            {"role": "user", "content": "Hello"},
        ]
        compacted = [{"role": "user", "content": "compacted"}]
        selected, clear = Agent._select_llm_messages(
            pre_hook_messages=pre_hook,
            modified_messages=pre_hook,
            compacted_messages=compacted,
        )
        assert clear is True
        assert len(selected) == 2
        assert selected[0]["role"] == "system"
        assert selected[1]["content"] == "compacted"


# ---------------------------------------------------------------------------
# 5. Multi-turn semantic: ephemeral injections re-injected fresh each turn
# ---------------------------------------------------------------------------

class TestEphemeralInjectionSemantics:
    """The architectural intent: system prompt is rebuilt fresh each turn,
    NOT persisted. Hook injections (pinned, SAM, restoration) are ephemeral
    by design. Persistence (next-turn pre-hook input) only stores the
    conversation, not the system messages."""

    def test_modern_path_clears_compacted_marker(self, base_messages):
        """The compacted_messages marker MUST be cleared on the modern path.

        Regression: leaving the auto_sync-set marker in place caused the
        v5b synopsis moderator ``store_fact`` loop. After the LLM call,
        ``messages.append(assistant_msg)`` appends the tool-call assistant
        message. The post-tool-execution code path then checks the
        session tracker for ``compacted_messages``; if it's still set, it
        interprets that as "a tool modified the conversation mid-request,
        rebuild messages" and replaces messages with
        ``[system] + compacted_messages + tool_messages`` — DROPPING the
        just-appended assistant tool-call message. Across step-loop
        iterations the LLM never sees its own prior tool calls/results
        and loops calling the same tool with identical args.

        The auto_sync-set marker is redundant once modified_messages is
        used as the LLM input — the hook output already reflects the
        same content. Persistence is handled separately by post-LLM
        ``set_session_messages`` calls in the step-loop."""
        modified = base_messages + _msgs(("system", "injected"))
        compacted = _msgs(("user", "from auto_sync"))
        _, clear = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=modified,
            compacted_messages=compacted,
        )
        assert clear is True  # marker MUST be cleared to avoid tool-loop regression

    def test_legacy_path_clears_compacted_marker(self, base_messages):
        """Legacy path's compacted is one-shot — both current-turn list AND
        persistence are the same content. Marker must be cleared so the
        next iteration doesn't double-use it."""
        compacted = _msgs(("user", "legacy compacted"))
        _, clear = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=base_messages,  # same identity → legacy
            compacted_messages=compacted,
        )
        assert clear is True
