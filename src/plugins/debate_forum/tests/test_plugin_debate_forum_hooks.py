"""Debate Forum Plugin - Hook Tests.

Tests cover:
- inject_debate_context hook: two-tier injection (pinned→user ephemeral, posts→user permanent)
- Diff-based injection: only new messages since last call
- Context var resolution
- Edge cases (no channel, empty thread, missing vars)
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from plugins.debate_forum.database import DebateForumDB
from plugins.debate_forum.hooks import DebateForumHooks, INJECTION_MARKER, INJECTION_MARKER_POSTS
from agent_system.llm.models import ChatMessage
from agent_system.hooks import HookContext


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def db(tmp_path: Path) -> DebateForumDB:
    """Fresh SQLite database for each test."""
    db_path = tmp_path / "test_forum.db"
    return DebateForumDB(db_path, wal_mode=False)


@pytest.fixture
def hooks(tmp_path: Path, db: DebateForumDB) -> DebateForumHooks:
    """DebateForumHooks using actual plugin directory for schema loading."""
    plugin_dir = Path(__file__).resolve().parent.parent.parent.parent.parent / "src" / "plugins" / "debate_forum"
    h = DebateForumHooks(plugin_dir, db)
    return h


def _make_context(
    messages: list[ChatMessage],
    session_id: str = "test-session",
    context_vars: dict | None = None,
) -> HookContext:
    """Create a minimal HookContext with mocked agent/session_tracker.

    The mock tracker supports get/set_session_template_vars so the hook
    can track ``debate_last_injected_msg_id`` across calls.
    """
    agent = MagicMock()
    _vars = dict(context_vars or {})

    tracker = MagicMock()
    tracker.get_session_template_vars.side_effect = lambda sid: _vars
    tracker.set_session_template_vars.side_effect = lambda sid, v: _vars.update(v)
    agent._session_tracker = tracker

    return HookContext(
        hook_type="pre_llm_call",
        request_id="req-1",
        session_id=session_id,
        agent=agent,
        agent_name="test_agent",
        messages=messages,
    )


def _sys(content: str) -> ChatMessage:
    return ChatMessage(role="system", content=content)


def _user(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content)


def _assistant(content: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=content)


# =============================================================================
# Hook Tests
# =============================================================================

class TestInjectDebateContext:
    """Tests for the inject_debate_context pre_llm_call hook."""

    @pytest.mark.asyncio
    async def test_no_context_vars_noop(self, hooks: DebateForumHooks):
        """Hook should not modify messages if no debate_channel_id is set."""
        msgs = [_sys("You are a helpful assistant"), _user("Hello")]
        ctx = _make_context(msgs, context_vars={})
        result = await hooks.inject_debate_context(ctx)
        assert result.success
        assert not result.modified
        assert len(result.context.messages) == 2

    @pytest.mark.asyncio
    async def test_no_messages_noop(self, hooks: DebateForumHooks):
        """Hook should not fail on empty messages."""
        ctx = _make_context([], context_vars={"debate_channel_id": 1})
        result = await hooks.inject_debate_context(ctx)
        assert result.success
        assert not result.modified

    @pytest.mark.asyncio
    async def test_no_session_id_noop(self, hooks: DebateForumHooks):
        """Hook should not fail without session_id."""
        msgs = [_sys("system"), _user("hi")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": 1})
        ctx.session_id = ""
        result = await hooks.inject_debate_context(ctx)
        assert result.success
        assert not result.modified

    @pytest.mark.asyncio
    async def test_empty_channel_noop(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Hook should not inject if channel has no messages and no topic."""
        ch = db.create_channel(name="empty", topic="")
        msgs = [_sys("system"), _user("hi")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": ch["channel_id"]})
        result = await hooks.inject_debate_context(ctx)
        assert result.success
        assert not result.modified

    @pytest.mark.asyncio
    async def test_channel_with_topic_injects_system(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Channel with topic but no messages should inject metadata as system message."""
        ch = db.create_channel(name="test", topic="Test debate")
        msgs = [_sys("system"), _user("hi")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": ch["channel_id"]})
        result = await hooks.inject_debate_context(ctx)

        assert result.success
        assert result.modified
        assert len(result.context.messages) == 3
        injected = result.context.messages[1]
        assert injected.role == "system"
        assert injected.injected_by == INJECTION_MARKER
        assert "Test debate" in injected.content

    @pytest.mark.asyncio
    async def test_unpinned_messages_injected_as_user(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Unpinned forum posts should be injected as role=user (permanent),
        pinned-context/metadata as role=system (compaction-safe)."""
        ch = db.create_channel(name="test", topic="Test debate")
        cid = ch["channel_id"]
        db.post_message(cid, "Mira", "advocate", 1, "I argue for X")
        db.post_message(cid, "Sven", "critic", 1, "I argue against X")

        msgs = [_sys("You are Mira"), _user("Debate topic")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})
        result = await hooks.inject_debate_context(ctx)

        assert result.success
        assert result.modified
        # system, system(metadata), user(posts), user(original)
        assert len(result.context.messages) == 4

        # System injection (metadata / pinned)
        sys_injected = result.context.messages[1]
        assert sys_injected.role == "system"
        assert sys_injected.injected_by == INJECTION_MARKER
        assert "Test debate" in sys_injected.content

        # User injection (posts)
        user_injected = result.context.messages[2]
        assert user_injected.role == "user"
        assert user_injected.injected_by == INJECTION_MARKER_POSTS
        assert "Mira" in user_injected.content
        assert "I argue for X" in user_injected.content
        assert "Sven" in user_injected.content
        assert "I argue against X" in user_injected.content

    @pytest.mark.asyncio
    async def test_diff_injection_only_new_messages(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Running the hook twice should only inject NEW messages on the second call."""
        ch = db.create_channel(name="test", topic="Test")
        cid = ch["channel_id"]
        db.post_message(cid, "Sven", "critic", 1, "Round 1 argument")

        msgs = [_sys("You are Mira"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})

        # First injection: all messages
        result1 = await hooks.inject_debate_context(ctx)
        assert result1.modified
        user_msgs = [m for m in result1.context.messages if m.injected_by == INJECTION_MARKER_POSTS]
        assert len(user_msgs) == 1
        assert "Round 1 argument" in user_msgs[0].content

        # Add a new message to the forum
        db.post_message(cid, "Sven", "critic", 2, "Round 2 argument")

        # Second injection on the same context: only round 2 should be new
        result2 = await hooks.inject_debate_context(result1.context)
        assert result2.modified
        user_msgs = [m for m in result2.context.messages if m.injected_by == INJECTION_MARKER_POSTS]
        assert len(user_msgs) == 2  # Both batches present
        # Latest batch should contain only round 2
        latest_batch = user_msgs[-1]
        assert "Round 2 argument" in latest_batch.content
        assert "Round 1 argument" not in latest_batch.content

    @pytest.mark.asyncio
    async def test_no_sliding_window_all_messages_injected(self, hooks: DebateForumHooks, db: DebateForumDB):
        """All unpinned messages should be injected on first call (no max limit)."""
        ch = db.create_channel(name="test", topic="Test")
        cid = ch["channel_id"]
        for i in range(15):
            db.post_message(cid, "Sven", "critic", i + 1, f"Argument {i + 1}")

        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})
        result = await hooks.inject_debate_context(ctx)

        assert result.modified
        user_msgs = [m for m in result.context.messages if m.injected_by == INJECTION_MARKER_POSTS]
        assert len(user_msgs) == 1
        content = user_msgs[0].content
        # ALL 15 messages should be present (no sliding window)
        for i in range(1, 16):
            assert f"Argument {i}" in content

    @pytest.mark.asyncio
    async def test_user_injection_before_last_user_message(self, hooks: DebateForumHooks, db: DebateForumDB):
        """User injection should appear before the last user message."""
        ch = db.create_channel(name="test", topic="Test")
        cid = ch["channel_id"]
        db.post_message(cid, "Sven", "critic", 1, "My argument")

        msgs = [
            _sys("System prompt"),
            _user("Message 1"),
            _assistant("Response 1"),
            _user("Message 2"),
        ]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})
        result = await hooks.inject_debate_context(ctx)

        assert result.modified
        messages = result.context.messages
        # system, user(metadata), user1, assistant1, user(injected), user2
        assert len(messages) == 6
        assert messages[0].role == "system"
        assert messages[1].injected_by == INJECTION_MARKER  # metadata
        assert messages[2].content == "Message 1"
        assert messages[3].content == "Response 1"
        assert messages[4].injected_by == INJECTION_MARKER_POSTS  # posts
        assert messages[4].role == "user"
        assert messages[5].content == "Message 2"

    @pytest.mark.asyncio
    async def test_formats_round_headers(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Injected user content should include round headers."""
        ch = db.create_channel(name="test", topic="Test")
        cid = ch["channel_id"]
        db.post_message(cid, "Sven", "critic", 1, "Round 1")
        db.post_message(cid, "Sven", "critic", 2, "Round 2")

        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})
        result = await hooks.inject_debate_context(ctx)

        user_msgs = [m for m in result.context.messages if m.injected_by == INJECTION_MARKER_POSTS]
        assert len(user_msgs) == 1
        content = user_msgs[0].content
        assert "### Runde 1" in content
        assert "### Runde 2" in content

    @pytest.mark.asyncio
    async def test_invalid_channel_id_noop(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Hook should handle non-existent channel gracefully."""
        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": 9999})
        result = await hooks.inject_debate_context(ctx)
        assert result.success
        assert not result.modified

    @pytest.mark.asyncio
    async def test_pinned_as_system_unpinned_as_user(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Pinned messages → system injection (compaction-safe); unpinned → user injection (permanent)."""
        ch = db.create_channel(name="test", topic="Mixed test")
        cid = ch["channel_id"]
        r1 = db.post_message(cid, "Mod", "moderator", 0, "Pinned summary")
        db.pin_message(r1["message_id"])
        db.post_message(cid, "Sven", "critic", 1, "Normal argument")

        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})
        result = await hooks.inject_debate_context(ctx)

        assert result.modified
        sys_injected = [m for m in result.context.messages
                        if getattr(m, "injected_by", None) == INJECTION_MARKER]
        user_injected = [m for m in result.context.messages
                         if getattr(m, "injected_by", None) == INJECTION_MARKER_POSTS]

        assert len(sys_injected) == 1
        assert len(user_injected) == 1
        assert "Pinned summary" in sys_injected[0].content
        assert sys_injected[0].role == "system"
        assert "Normal argument" in user_injected[0].content
        assert user_injected[0].role == "user"
        # Pinned should NOT appear in user injection
        assert "Pinned summary" not in user_injected[0].content

    @pytest.mark.asyncio
    async def test_tracks_last_injected_id(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Hook should update debate_last_injected_msg_id in session vars."""
        ch = db.create_channel(name="test", topic="Track test")
        cid = ch["channel_id"]
        r1 = db.post_message(cid, "Sven", "critic", 1, "First")
        r2 = db.post_message(cid, "Mira", "advocate", 1, "Second")

        context_vars = {"debate_channel_id": cid}
        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars=context_vars)
        await hooks.inject_debate_context(ctx)

        # The tracker should have been called with the max message id
        tracker = ctx.agent._session_tracker
        set_calls = tracker.set_session_template_vars.call_args_list
        assert len(set_calls) == 1
        stored_id = set_calls[0][0][1]["debate_last_injected_msg_id"]
        assert stored_id == r2["message_id"]

    @pytest.mark.asyncio
    async def test_system_injection_is_idempotent(self, hooks: DebateForumHooks, db: DebateForumDB):
        """System injection (pinned/metadata) should be replaced, not duplicated."""
        ch = db.create_channel(name="test", topic="Idempotent test")
        cid = ch["channel_id"]
        r1 = db.post_message(cid, "Mod", "moderator", 0, "Pinned info")
        db.pin_message(r1["message_id"])

        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})

        # First call
        await hooks.inject_debate_context(ctx)
        sys_count_1 = sum(1 for m in ctx.messages if getattr(m, "injected_by", None) == INJECTION_MARKER)
        assert sys_count_1 == 1

        # Second call: should replace, not duplicate
        await hooks.inject_debate_context(ctx)
        sys_count_2 = sum(1 for m in ctx.messages if getattr(m, "injected_by", None) == INJECTION_MARKER)
        assert sys_count_2 == 1

    @pytest.mark.asyncio
    async def test_no_new_messages_skips_user_injection(self, hooks: DebateForumHooks, db: DebateForumDB):
        """If all messages are already injected, no user injection should happen."""
        ch = db.create_channel(name="test", topic="Test")
        cid = ch["channel_id"]
        db.post_message(cid, "Sven", "critic", 1, "Old argument")

        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})

        # First call injects everything
        await hooks.inject_debate_context(ctx)
        user_count_1 = sum(1 for m in ctx.messages if getattr(m, "injected_by", None) == INJECTION_MARKER_POSTS)
        assert user_count_1 == 1

        # Second call: no new messages → no additional user injection
        result = await hooks.inject_debate_context(ctx)
        user_count_2 = sum(1 for m in ctx.messages if getattr(m, "injected_by", None) == INJECTION_MARKER_POSTS)
        assert user_count_2 == 1  # still just 1

    @pytest.mark.asyncio
    async def test_inherited_counter_from_parent_session_replays_history(
        self, hooks: DebateForumHooks, db: DebateForumDB
    ):
        """A sub-agent inheriting ``debate_last_injected_msg_id`` from its
        parent must NOT skip history posted before its spawn.

        Regression: sub_agent_manager copies parent context_vars into the
        new sub-session at spawn time. Before the fix, the diff-based
        injection hook treated the inherited counter as its own, so a
        fresh sub-agent saw only messages posted AFTER the parent's last
        read — symptom observed: Falk (Provocateur) sub-agent in channel
        3088 saw only Autor-C's post because the moderator's counter was
        already past Autor-A and Autor-B's msg_ids.
        """
        ch = db.create_channel(name="test", topic="Test")
        cid = ch["channel_id"]
        # Three posts: simulate Autor-A, Autor-B, Autor-C
        db.post_message(cid, "Autor-A", "writer", 1, "Synopsis A content")
        db.post_message(cid, "Autor-B", "writer", 1, "Synopsis B content")
        db.post_message(cid, "Autor-C", "writer", 1, "Synopsis C content")

        # Get the actual msg_ids posted into this channel
        all_posts = db.get_messages(cid, limit=0)
        autor_b_id = all_posts[1]["id"]

        # Simulate sub-agent spawn: it inherits the parent's counter
        # (parent had read up to Autor-B) via context_vars inheritance.
        msgs = [_sys("You are Falk"), _user("Konvergenz-Check über A/B/C")]
        ctx = _make_context(
            msgs,
            session_id="falk-session",
            context_vars={
                "debate_channel_id": cid,
                "debate_last_injected_msg_id": autor_b_id,
                "debate_counter_owner": "moderator-session",  # parent's id
            },
        )

        result = await hooks.inject_debate_context(ctx)

        assert result.modified
        user_msgs = [
            m for m in result.context.messages
            if m.injected_by == INJECTION_MARKER_POSTS
        ]
        # All three synopses must be present — the inherited counter
        # from "moderator-session" must NOT cause Falk to skip A and B.
        assert len(user_msgs) == 1
        content = user_msgs[0].content
        assert "Synopsis A content" in content
        assert "Synopsis B content" in content
        assert "Synopsis C content" in content

    @pytest.mark.asyncio
    async def test_own_counter_diff_injection_still_works(
        self, hooks: DebateForumHooks, db: DebateForumDB
    ):
        """The ownership check must not break legitimate diff-injection
        within the SAME session — counter set in call 1 is honored in call 2."""
        ch = db.create_channel(name="test", topic="Test")
        cid = ch["channel_id"]
        db.post_message(cid, "Sven", "critic", 1, "Old content")

        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})

        # Call 1: hook claims ownership and injects
        await hooks.inject_debate_context(ctx)

        # Add another message to the channel
        db.post_message(cid, "Sven", "critic", 2, "New content")

        # Call 2: same session — should ONLY inject the new message
        await hooks.inject_debate_context(ctx)
        user_msgs = [
            m for m in ctx.messages
            if m.injected_by == INJECTION_MARKER_POSTS
        ]
        assert len(user_msgs) == 2
        latest = user_msgs[-1].content
        assert "New content" in latest
        assert "Old content" not in latest


class TestDebateForumHooksSchema:
    """Tests for hook schema loading and configuration."""

    def test_hooks_loaded(self, hooks: DebateForumHooks):
        """Schema should define the inject_debate_context hook."""
        hook_defs = hooks.get_hooks()
        assert len(hook_defs) >= 1
        names = [h["name"] for h in hook_defs]
        assert "inject_debate_context" in names

    def test_hook_type(self, hooks: DebateForumHooks):
        """Hook should be of type pre_llm_call."""
        hook_defs = hooks.get_hooks()
        ctx_hook = next(h for h in hook_defs if h["name"] == "inject_debate_context")
        assert ctx_hook["type"] == "pre_llm_call"

    def test_hook_disabled_by_default(self, hooks: DebateForumHooks):
        """Hook should be disabled by default (enabled per-agent)."""
        hook_defs = hooks.get_hooks()
        ctx_hook = next(h for h in hook_defs if h["name"] == "inject_debate_context")
        assert ctx_hook["enabled"] is False


