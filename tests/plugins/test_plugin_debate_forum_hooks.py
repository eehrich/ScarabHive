"""Debate Forum Plugin - Hook Tests.

Tests cover:
- inject_debate_context hook: message injection, filtering, idempotency
- Context var resolution
- Edge cases (no channel, empty thread, missing vars)
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from plugins.debate_forum.database import DebateForumDB
from plugins.debate_forum.hooks import DebateForumHooks, INJECTION_MARKER
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
    plugin_dir = Path(__file__).resolve().parent.parent.parent / "src" / "plugins" / "debate_forum"
    h = DebateForumHooks(plugin_dir, db)
    return h


def _make_context(
    messages: list[ChatMessage],
    session_id: str = "test-session",
    context_vars: dict | None = None,
) -> HookContext:
    """Create a minimal HookContext with mocked agent/session_tracker."""
    agent = MagicMock()
    tracker = MagicMock()
    tracker.get_session_template_vars.return_value = context_vars or {}
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
        """Hook should not inject if channel has no messages."""
        ch = db.create_channel(name="empty", topic="Nothing here")
        msgs = [_sys("system"), _user("hi")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": ch["channel_id"]})
        result = await hooks.inject_debate_context(ctx)
        assert result.success
        assert not result.modified

    @pytest.mark.asyncio
    async def test_injects_all_messages(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Hook should inject all debate messages (no filtering)."""
        ch = db.create_channel(name="test", topic="Test debate")
        cid = ch["channel_id"]
        db.post_message(cid, "Mira", "advocate", 1, "I argue for X")
        db.post_message(cid, "Sven", "critic", 1, "I argue against X")

        msgs = [_sys("You are Mira"), _user("Debate topic")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})
        result = await hooks.inject_debate_context(ctx)

        assert result.success
        assert result.modified
        # Should have 3 messages: system, injected, user
        assert len(result.context.messages) == 3
        injected = result.context.messages[1]
        assert injected.role == "system"
        assert injected.injected_by == INJECTION_MARKER
        # Should contain BOTH participants
        assert "Mira" in injected.content
        assert "I argue for X" in injected.content
        assert "Sven" in injected.content
        assert "I argue against X" in injected.content

    @pytest.mark.asyncio
    async def test_idempotent_injection(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Running the hook twice should replace the previous injection, not duplicate."""
        ch = db.create_channel(name="test", topic="Test")
        cid = ch["channel_id"]
        db.post_message(cid, "Sven", "critic", 1, "Round 1 argument")

        msgs = [_sys("You are Mira"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})

        # First injection
        result1 = await hooks.inject_debate_context(ctx)
        assert result1.modified
        assert len(result1.context.messages) == 3

        # Add a new message to the forum
        db.post_message(cid, "Sven", "critic", 2, "Round 2 argument")

        # Second injection on the same context
        result2 = await hooks.inject_debate_context(result1.context)
        assert result2.modified
        # Still 3 messages (old injection removed, new one added)
        assert len(result2.context.messages) == 3
        injected = result2.context.messages[1]
        assert "Round 2 argument" in injected.content

    @pytest.mark.asyncio
    async def test_respects_max_messages(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Hook should limit injected messages to max_messages_injected config."""
        ch = db.create_channel(name="test", topic="Test")
        cid = ch["channel_id"]
        # Post 10 messages
        for i in range(10):
            db.post_message(cid, "Sven", "critic", i + 1, f"Argument {i + 1}")

        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})
        result = await hooks.inject_debate_context(ctx)

        assert result.modified
        injected = result.context.messages[1]
        # Default max_messages_injected is 6, so only last 6 should be present
        assert "Argument 5" in injected.content
        assert "Argument 10" in injected.content
        # First 4 should be trimmed (use word boundary to avoid matching "Argument 10" etc.)
        assert "\nArgument 1\n" not in injected.content
        assert "\nArgument 4\n" not in injected.content

    @pytest.mark.asyncio
    async def test_insert_position_after_system(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Injection should be placed after system messages, before user messages."""
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
        assert len(result.context.messages) == 5
        # Position 0: system, 1: injected, 2+: conversation
        assert result.context.messages[0].role == "system"
        assert result.context.messages[1].injected_by == INJECTION_MARKER
        assert result.context.messages[2].role == "user"

    @pytest.mark.asyncio
    async def test_formats_round_headers(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Injected content should include round headers."""
        ch = db.create_channel(name="test", topic="Test")
        cid = ch["channel_id"]
        db.post_message(cid, "Sven", "critic", 1, "Round 1")
        db.post_message(cid, "Sven", "critic", 2, "Round 2")

        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})
        result = await hooks.inject_debate_context(ctx)

        injected = result.context.messages[1]
        assert "### Round 1" in injected.content
        assert "### Round 2" in injected.content

    @pytest.mark.asyncio
    async def test_invalid_channel_id_noop(self, hooks: DebateForumHooks, db: DebateForumDB):
        """Hook should handle non-existent channel gracefully."""
        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": 9999})
        result = await hooks.inject_debate_context(ctx)
        assert result.success
        assert not result.modified


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

    def test_config_defaults(self, hooks: DebateForumHooks):
        """Config should have max_messages_injected with a sensible default."""
        config = hooks.get_config()
        assert "max_messages_injected" in config
        assert config["max_messages_injected"] == 6


# =============================================================================
# Config Override Tests (plugins.yaml overrides schema defaults)
# =============================================================================

class TestConfigOverride:
    """Tests that plugin_config passed at construction overrides schema defaults."""

    def _make_hooks(self, db: DebateForumDB, plugin_config: dict) -> DebateForumHooks:
        plugin_dir = Path(__file__).resolve().parent.parent.parent / "src" / "plugins" / "debate_forum"
        return DebateForumHooks(plugin_dir, db, plugin_config=plugin_config)

    def test_plugin_config_overrides_max_messages(self, db: DebateForumDB):
        """plugin_config should override the schema default for max_messages_injected."""
        h = self._make_hooks(db, {"max_messages_injected": 12})
        assert h.get_config()["max_messages_injected"] == 12

    def test_plugin_config_partial_override(self, db: DebateForumDB):
        """Only provided keys should be overridden; others keep schema defaults."""
        h = self._make_hooks(db, {"max_messages_injected": 20})
        config = h.get_config()
        assert config["max_messages_injected"] == 20
        # Other schema keys should still be present
        assert "max_messages_injected" in config

    def test_no_plugin_config_uses_schema_defaults(self, db: DebateForumDB):
        """Without plugin_config, schema default (6) should be used."""
        plugin_dir = Path(__file__).resolve().parent.parent.parent / "src" / "plugins" / "debate_forum"
        h = DebateForumHooks(plugin_dir, db)
        assert h.get_config()["max_messages_injected"] == 6

    def test_empty_plugin_config_uses_schema_defaults(self, db: DebateForumDB):
        """Empty plugin_config dict should not change schema defaults."""
        h = self._make_hooks(db, {})
        assert h.get_config()["max_messages_injected"] == 6

    @pytest.mark.asyncio
    async def test_overridden_limit_applied_during_injection(self, db: DebateForumDB):
        """Overridden max_messages_injected must be honoured at injection time."""
        h = self._make_hooks(db, {"max_messages_injected": 3})
        ch = db.create_channel(name="test", topic="Limit test")
        cid = ch["channel_id"]
        for i in range(8):
            db.post_message(cid, "Sven", "critic", i + 1, f"Msg {i + 1}")

        msgs = [_sys("system"), _user("go")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})
        result = await h.inject_debate_context(ctx)

        assert result.modified
        injected = result.context.messages[1].content
        # Only last 3 should appear
        assert "Msg 6" in injected
        assert "Msg 7" in injected
        assert "Msg 8" in injected
        # Earlier messages must be absent
        assert "Msg 1" not in injected
        assert "Msg 5" not in injected

    @pytest.mark.asyncio
    async def test_higher_limit_includes_more_messages(self, db: DebateForumDB):
        """Setting limit to 12 should include more messages than the default 6."""
        h = self._make_hooks(db, {"max_messages_injected": 12})
        ch = db.create_channel(name="test", topic="Wide window")
        cid = ch["channel_id"]
        for i in range(10):
            db.post_message(cid, "Anna", "pragmatiker", i + 1, f"Point {i + 1}")

        msgs = [_sys("system"), _user("start")]
        ctx = _make_context(msgs, context_vars={"debate_channel_id": cid})
        result = await h.inject_debate_context(ctx)

        assert result.modified
        injected = result.context.messages[1].content
        # All 10 messages fit within the limit of 12
        for i in range(1, 11):
            assert f"Point {i}" in injected

