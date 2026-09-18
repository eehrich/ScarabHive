"""Tests for ``Agent._select_llm_messages``.

The helper picks which message list goes to the LLM after the pre_llm_call
hook chain has run. The whole contract is list IDENTITY: a hook that wants to
change what the model sees returns a NEW list. In-place mutation is not seen,
which is why tool_preload builds ``list(messages) + appended``.

There used to be a second input, the ``compacted_messages`` marker on the
session tracker, and a rebuild of ``[leading systems from pre-hook] +
compacted`` whenever it was set. That rebuild is built from the messages
BEFORE the hooks ran, so it dropped every system block a hook had injected --
how the v5b synopsis moderator once looped 100x on
``context_engineer.recall()`` without ever seeing the pinned channel task.
It was also unreachable: whoever sets that marker inside a hook returns a new
list in the same round, and the tool path clears it in its own step. Both the
branch and its marker argument are gone; the caller clears the marker
unconditionally.
"""

from __future__ import annotations

import pytest

from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.server import Agent


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


class TestHookReturnedANewList:
    """A new list is the hook chain's output and goes to the LLM verbatim."""

    def test_pure_injection_hook_chain_passes_through(self, base_messages):
        """debate_forum-style: injects a role=system message via hook chain."""
        modified = _msgs(
            ("system", "You are a helpful assistant."),
            ("system", "## Debate Forum - Channel #3055\nPinned content..."),
            ("user", "Hello"),
        )
        selected = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=modified,
        )
        assert selected is modified
        assert len(selected) == 3
        assert selected[1].role == "system"
        assert "Pinned content" in selected[1].content

    def test_b171_regression_pinned_role_system_survives(self, base_messages):
        """Regression for B171/B172: the v5b synopsis moderator loop.

        debate_forum injects its pinned content with ``role=system``. The old
        rebuild dropped it, because it was assembled from the leading systems
        of the PRE-hook list, which never saw the injection. Moderator agents
        could not see the channel task and looped 100x on
        ``context_engineer.recall()`` searching for the missing context.
        """
        pinned = ChatMessage(
            role="system",
            content="## Debate Forum - Channel #3055\nStory-Idee: ...",
            injected_by="debate_forum",
        )
        modified = _msgs(
            ("system", "You are a helpful assistant."),
        ) + [pinned] + _msgs(
            ("user", "Channel-ID: 3055"),
        )

        selected = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=modified,
        )
        assert selected is modified
        pinned_msgs = [m for m in selected if "Debate Forum" in (m.content or "")]
        assert len(pinned_msgs) == 1
        assert pinned_msgs[0].role == "system"

    def test_multiple_hooks_inject_multiple_systems(self, base_messages):
        """Pinned + SAM context + restoration all reach the LLM."""
        modified = _msgs(
            ("system", "You are a helpful assistant."),
            ("system", "## Debate Forum - Channel #3055\nPinned..."),
            ("system", "## Active Sub-Agents\nautor_1: ..."),
            ("system", "# Context Engineer - Stored Information\n..."),
            ("user", "Hello"),
        )
        selected = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=modified,
        )
        assert selected is modified
        assert len([m for m in selected if m.role == "system"]) == 4

    def test_compaction_hook_modern_path(self, base_messages):
        """context_engineer compacts by returning a new ``context.messages``."""
        long_history = _msgs(
            ("system", "You are a helpful assistant."),
            ("user", "msg 1"), ("assistant", "reply 1"),
            ("user", "msg 2"), ("assistant", "reply 2"),
        )
        compacted_view = _msgs(
            ("system", "You are a helpful assistant."),
            ("system",
             '{"type": "archived_ref", "summary": "Earlier turns archived"}'),
            ("user", "msg 2"), ("assistant", "reply 2"),
        )
        selected = Agent._select_llm_messages(
            pre_hook_messages=long_history,
            modified_messages=compacted_view,
        )
        assert selected is compacted_view
        assert any('"archived_ref"' in (m.content or "") for m in selected)


class TestHookLeftTheListAlone:
    """Same identity, or nothing returned: the pre-hook list goes out."""

    def test_same_identity_passes_the_original_through(self, base_messages):
        selected = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=base_messages,
        )
        assert selected is base_messages

    def test_modified_none_falls_back(self, base_messages):
        """Hook chain returned ``None`` (defensive: should not happen)."""
        selected = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=None,
        )
        assert selected is base_messages


class TestEdgeCases:
    def test_two_empty_lists_are_still_two_lists(self):
        """Identity, not content: an empty new list is a new list."""
        modified = []
        selected = Agent._select_llm_messages(
            pre_hook_messages=[],
            modified_messages=modified,
        )
        assert selected is modified

    def test_hook_returned_an_empty_list(self, base_messages):
        """A hook that returns [] gets its way -- the helper does not second
        guess it. (A hook doing this is broken; the helper is not the guard.)"""
        modified = []
        selected = Agent._select_llm_messages(
            pre_hook_messages=base_messages,
            modified_messages=modified,
        )
        assert selected is modified

    def test_dict_messages_pass_through(self):
        """Messages may be dicts, not just ChatMessage objects."""
        pre_hook = [
            {"role": "system", "content": "Base"},
            {"role": "user", "content": "Hello"},
        ]
        modified = pre_hook + [{"role": "system", "content": "injected"}]
        selected = Agent._select_llm_messages(
            pre_hook_messages=pre_hook,
            modified_messages=modified,
        )
        assert selected is modified
