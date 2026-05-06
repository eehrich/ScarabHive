"""Tests for SessionService including token estimation."""

import pytest
from agent_system.services.session_service import _estimate_message_tokens, _trim_to_safe_boundary


class TestTrimToSafeBoundary:
    """Tests for the orphan-tool_call trim used by periodic checkpoints."""

    def test_empty_list(self):
        assert _trim_to_safe_boundary([]) == []

    def test_only_user(self):
        msgs = [{"role": "user", "content": "hello"}]
        assert _trim_to_safe_boundary(msgs) == msgs

    def test_completed_tool_round_trip(self):
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
            {"role": "assistant", "content": "done"},
        ]
        assert _trim_to_safe_boundary(msgs) == msgs

    def test_pending_tool_call_dropped(self):
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "tool_calls": [{"id": "tc1"}]},
            # tool result missing — pipeline still running
        ]
        trimmed = _trim_to_safe_boundary(msgs)
        assert trimmed == [{"role": "user", "content": "go"}]

    def test_partial_parallel_tool_calls_dropped(self):
        # Assistant fires two tool_calls, only one is answered → entire assistant is unsafe
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "tool_calls": [{"id": "tc1"}, {"id": "tc2"}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
        ]
        trimmed = _trim_to_safe_boundary(msgs)
        assert trimmed == [{"role": "user", "content": "go"}]

    def test_keeps_history_drops_only_trailing_orphan(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "r1"},
            {"role": "assistant", "content": "step done"},
            {"role": "user", "content": "next"},
            {"role": "assistant", "tool_calls": [{"id": "tc2"}]},
            # tc2 still pending
        ]
        trimmed = _trim_to_safe_boundary(msgs)
        assert trimmed == msgs[:5]

    def test_user_message_alone_is_safe_boundary(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "r1"},
            {"role": "user", "content": "follow-up"},
        ]
        # Trailing user message is fine — it's a complete state
        assert _trim_to_safe_boundary(msgs) == msgs


class TestEstimateMessageTokens:
    """Tests for the _estimate_message_tokens helper function."""

    def test_simple_text_content(self):
        """Test token estimation for simple text content."""
        msg = {"role": "user", "content": "Hello, how are you?"}
        tokens = _estimate_message_tokens(msg)
        # ~20 chars / 4 = ~5 tokens
        assert 3 <= tokens <= 10

    def test_empty_content(self):
        """Test token estimation for empty content."""
        msg = {"role": "assistant", "content": ""}
        tokens = _estimate_message_tokens(msg)
        # Empty but at least 1 token
        assert tokens >= 1

    def test_none_content(self):
        """Test token estimation for None content."""
        msg = {"role": "assistant", "content": None}
        tokens = _estimate_message_tokens(msg)
        assert tokens >= 1

    def test_long_text_content(self):
        """Test token estimation for longer text."""
        # 400 chars should be ~100 tokens
        long_text = "a" * 400
        msg = {"role": "user", "content": long_text}
        tokens = _estimate_message_tokens(msg)
        assert 90 <= tokens <= 110

    def test_multimodal_content_with_text(self):
        """Test token estimation for multimodal content with text."""
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"}
            ]
        }
        tokens = _estimate_message_tokens(msg)
        # ~25 chars / 4 = ~6 tokens
        assert 4 <= tokens <= 12

    def test_multimodal_content_with_image(self):
        """Test token estimation for multimodal content with image."""
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
            ]
        }
        tokens = _estimate_message_tokens(msg)
        # ~15 chars text + 1000 for image = ~1003
        assert tokens >= 1000

    def test_multimodal_multiple_images(self):
        """Test token estimation for multiple images."""
        msg = {
            "role": "user",
            "content": [
                {"type": "image", "data": "..."},
                {"type": "image_url", "image_url": {"url": "..."}},
            ]
        }
        tokens = _estimate_message_tokens(msg)
        # 2 images = 2000 tokens
        assert tokens >= 2000

    def test_tool_calls_in_assistant_message(self):
        """Test token estimation includes tool calls."""
        msg = {
            "role": "assistant",
            "content": "Let me search for that.",
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": '{"query": "weather in Berlin"}'
                    }
                }
            ]
        }
        tokens = _estimate_message_tokens(msg)
        # Should include both content and tool calls
        # Content: ~25 chars = ~6 tokens
        # Tool call JSON: ~100 chars = ~25 tokens
        assert tokens >= 30

    def test_multiple_tool_calls(self):
        """Test token estimation with multiple tool calls."""
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "tool1", "arguments": '{"a": 1}'}
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "tool2", "arguments": '{"b": 2}'}
                }
            ]
        }
        tokens = _estimate_message_tokens(msg)
        # Two tool calls should add significant tokens
        assert tokens >= 40

    def test_reasoning_content(self):
        """Test token estimation includes reasoning content."""
        msg = {
            "role": "assistant",
            "content": "The answer is 42.",
            "reasoning_content": "Let me think about this step by step. First I need to consider..."
        }
        tokens = _estimate_message_tokens(msg)
        # Content (~17 chars = ~4 tokens) + reasoning (~65 chars = ~16 tokens)
        assert tokens >= 15

    def test_tool_result_message(self):
        """Test token estimation for tool result messages."""
        msg = {
            "role": "tool",
            "content": '{"result": "The weather in Berlin is sunny, 22°C"}',
            "tool_call_id": "call_abc123"
        }
        tokens = _estimate_message_tokens(msg)
        assert tokens >= 10

    def test_system_message(self):
        """Test token estimation for system messages."""
        system_prompt = "You are a helpful assistant. " * 50  # ~1500 chars
        msg = {"role": "system", "content": system_prompt}
        tokens = _estimate_message_tokens(msg)
        # ~1500 chars / 4 = ~375 tokens
        assert 350 <= tokens <= 400

    def test_content_as_list_with_strings(self):
        """Test multimodal content that's a list of strings (edge case)."""
        msg = {
            "role": "user",
            "content": ["Hello", "World", "How are you?"]
        }
        tokens = _estimate_message_tokens(msg)
        assert tokens >= 5

    def test_minimum_token_count(self):
        """Test that minimum token count is 1."""
        msg = {"role": "user", "content": ""}
        tokens = _estimate_message_tokens(msg)
        assert tokens >= 1

    def test_missing_content_key(self):
        """Test message without content key."""
        msg = {"role": "assistant", "tool_calls": []}
        tokens = _estimate_message_tokens(msg)
        assert tokens >= 1


# ---------------------------------------------------------------------------
# SessionService.save_session race-condition tests
# ---------------------------------------------------------------------------

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from agent_system.services.session_service import SessionService
from agent_system.services.session_manager import SessionManager, SessionNotFoundError


def _make_mock_agent(messages_dicts, runtime_template_vars=None):
    """Build a minimal mock agent whose session_tracker holds *messages_dicts*.

    runtime_template_vars: dict returned by get_session_template_vars(), simulating
    runtime template_vars set by plugins (e.g. task_switch.set_context).
    """
    agent = MagicMock()
    agent.agent_config.default_llm_profile = "normal"
    agent.agent_config.template_vars = {}
    tracker = MagicMock()
    # Convert dicts to mock ChatMessage objects
    mock_msgs = []
    for d in messages_dicts:
        m = MagicMock()
        m.role = d["role"]
        m.model_dump.return_value = d
        mock_msgs.append(m)
    tracker.get_session_messages.return_value = mock_msgs
    tracker.get_session_template_vars.return_value = runtime_template_vars or {}
    agent._session_tracker = tracker
    return agent


@pytest.fixture
def session_service_env(tmp_path):
    storage = tmp_path / "sessions"
    storage.mkdir()
    sm = SessionManager(storage_path=str(storage))
    svc = SessionService(session_manager=sm)
    return svc, sm


@pytest.mark.asyncio
async def test_save_session_race_creates_then_falls_back(session_service_env):
    """When _find_session_owner_async misses a session that already exists,
    save_session should catch the ValueError from create_session and fall
    back to load-update-save, preserving parent_session."""
    svc, sm = session_service_env

    # Pre-create a sub-agent session with parent_session (like SAM does)
    session = await sm.create_session(
        user_id="user1",
        session_id="sub_agent_race_001",
        title="Sub-agent",
        agent_name="v5b_moderator",
        llm_profile="normal",
    )
    session["parent_session"] = {"session_id": "parent_xyz", "created_at": "2025-01-01T00:00:00Z"}
    session["depth"] = 2
    await sm.save_session(session)

    # Clear cache so _find_session_owner_async has to hit filesystem
    sm.clear_cache()

    # Patch _find_session_owner_async to return None (simulating race)
    original_find = sm._find_session_owner_async

    async def mock_find_none(session_id):
        # First call returns None (race), subsequent calls work
        return None

    msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    agent = _make_mock_agent(msgs)

    with patch.object(sm, '_find_session_owner_async', side_effect=mock_find_none):
        # Restore for the retry inside save_session
        with patch.object(sm, '_find_session_owner_async', wraps=original_find):
            pass  # We need a different approach

    # Better: patch only the FIRST call
    call_count = 0
    async def mock_find_first_miss(session_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None  # First call: miss
        return await original_find(session_id)  # Retry: find it

    with patch.object(sm, '_find_session_owner_async', side_effect=mock_find_first_miss):
        result = await svc.save_session(
            agent=agent,
            user_id="user1",
            session_id="sub_agent_race_001",
            agent_name="v5b_moderator",
            llm_profile="normal",
            was_new_session=False,
        )

    assert result is True

    # Verify parent_session was preserved
    loaded = await sm.load_session("user1", "sub_agent_race_001")
    assert loaded["parent_session"]["session_id"] == "parent_xyz"
    assert loaded["depth"] == 2
    assert len(loaded["messages"]) == 2


@pytest.mark.asyncio
async def test_save_session_existing_preserves_parent(session_service_env):
    """Normal path: session exists, load-update-save preserves parent_session."""
    svc, sm = session_service_env

    session = await sm.create_session(
        user_id="user1",
        session_id="sub_normal_001",
        title="Sub-agent",
        agent_name="v5b_moderator",
        llm_profile="normal",
    )
    session["parent_session"] = {"session_id": "parent_abc"}
    session["depth"] = 3
    session["context_vars"] = {"book_id": "42"}
    await sm.save_session(session)

    msgs = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "done"}]
    agent = _make_mock_agent(msgs)

    result = await svc.save_session(
        agent=agent,
        user_id="user1",
        session_id="sub_normal_001",
        agent_name="v5b_moderator",
        llm_profile="normal",
        was_new_session=False,
    )
    assert result is True

    loaded = await sm.load_session("user1", "sub_normal_001")
    assert loaded["parent_session"]["session_id"] == "parent_abc"
    assert loaded["depth"] == 3
    assert loaded["context_vars"]["book_id"] == "42"
    assert len(loaded["messages"]) == 2


@pytest.mark.asyncio
async def test_save_session_syncs_runtime_template_vars_into_context_vars(session_service_env):
    """Runtime template_vars set via session_tracker (e.g. by task_switch or
    pipeline phases) must be synced into persisted context_vars on save —
    otherwise the Session Info panel shows stale/empty values."""
    svc, sm = session_service_env

    session = await sm.create_session(
        user_id="user1",
        session_id="sync_001",
        title="Test",
        agent_name="chat_agent",
        llm_profile="normal",
    )
    # Start with NO context_vars
    await sm.save_session(session)

    # Simulate a plugin setting runtime template_vars during the run
    msgs = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "done"}]
    agent = _make_mock_agent(msgs, runtime_template_vars={"workflow_phase": "review", "book_id": "99"})

    result = await svc.save_session(
        agent=agent,
        user_id="user1",
        session_id="sync_001",
        agent_name="chat_agent",
        llm_profile="normal",
        was_new_session=False,
    )
    assert result is True

    loaded = await sm.load_session("user1", "sync_001")
    assert loaded["context_vars"]["workflow_phase"] == "review"
    assert loaded["context_vars"]["book_id"] == "99"


@pytest.mark.asyncio
async def test_save_session_runtime_vars_merge_overrides_existing(session_service_env):
    """When both persisted context_vars AND runtime template_vars exist, runtime
    values should override (since the tracker holds the most recent state) while
    persisted-only keys are preserved."""
    svc, sm = session_service_env

    session = await sm.create_session(
        user_id="user1",
        session_id="merge_001",
        title="Test",
        agent_name="v5b_story_designer",
        llm_profile="normal",
    )
    session["context_vars"] = {"phase": "synopsis", "book_id": "1", "persisted_only": "x"}
    await sm.save_session(session)

    msgs = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "ok"}]
    agent = _make_mock_agent(msgs, runtime_template_vars={"phase": "beats", "new_key": "y"})

    result = await svc.save_session(
        agent=agent,
        user_id="user1",
        session_id="merge_001",
        agent_name="v5b_story_designer",
        llm_profile="normal",
        was_new_session=False,
    )
    assert result is True

    loaded = await sm.load_session("user1", "merge_001")
    cv = loaded["context_vars"]
    assert cv["phase"] == "beats"          # runtime wins
    assert cv["book_id"] == "1"            # persisted preserved (not in runtime)
    assert cv["persisted_only"] == "x"     # persisted preserved
    assert cv["new_key"] == "y"            # runtime adds new key


# ---------------------------------------------------------------------------
# checkpoint_session tests (orphan-safe periodic save)
# ---------------------------------------------------------------------------


def _make_checkpoint_agent(messages_dicts, runtime_template_vars=None, metadata=None):
    """Mock agent for checkpoint tests. Tracker exposes raw dicts directly."""
    agent = MagicMock()
    agent.name = "test_agent"
    tracker = MagicMock()
    # checkpoint_session iterates and calls _msg_to_dict — returning dicts directly works
    tracker.get_session_messages.return_value = list(messages_dicts)
    tracker.get_session_template_vars.return_value = runtime_template_vars or {}
    tracker.get_session_metadata.return_value = metadata or {}
    agent._session_tracker = tracker
    return agent


@pytest.mark.asyncio
async def test_checkpoint_session_existing_writes_safe_messages(session_service_env):
    """Checkpoint into an existing session file: trims pending tool_call, persists rest."""
    svc, sm = session_service_env

    await sm.create_session(
        user_id="user1",
        session_id="ckpt_existing",
        title="Test",
        agent_name="test_agent",
        llm_profile="normal",
    )

    msgs = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "tool_calls": [{"id": "tc_pipeline"}]},
        # tc_pipeline still running — orphan
    ]
    agent = _make_checkpoint_agent(msgs, runtime_template_vars={"phase": "running"})

    ok = await svc.checkpoint_session(agent, "user1", "ckpt_existing")
    assert ok is True

    loaded = await sm.load_session("user1", "ckpt_existing")
    # Only the user message survives the trim
    assert len(loaded["messages"]) == 1
    assert loaded["messages"][0]["role"] == "user"
    # Runtime context_vars are persisted alongside
    assert loaded["context_vars"]["phase"] == "running"


@pytest.mark.asyncio
async def test_checkpoint_session_creates_new_when_missing(session_service_env):
    """If session is not yet on disk, checkpoint creates it (only if there
    are safe messages — bare context_vars-only checkpoints are skipped)."""
    svc, sm = session_service_env

    msgs = [
        {"role": "user", "content": "kickoff"},
        {"role": "assistant", "tool_calls": [{"id": "tc1"}]},
    ]
    agent = _make_checkpoint_agent(
        msgs,
        runtime_template_vars={"book_id": "42"},
        metadata={"user_id": "user1", "agent_name": "linear_book", "llm_profile": "normal"},
    )

    ok = await svc.checkpoint_session(agent, "user1", "ckpt_new")
    assert ok is True

    loaded = await sm.load_session("user1", "ckpt_new")
    assert len(loaded["messages"]) == 1  # orphan trimmed
    assert loaded["messages"][0]["content"] == "kickoff"
    assert loaded["context_vars"]["book_id"] == "42"


@pytest.mark.asyncio
async def test_checkpoint_session_skips_when_nothing_safe(session_service_env):
    """No safe messages and no runtime vars → silent no-op, no file written."""
    svc, sm = session_service_env

    agent = _make_checkpoint_agent([], runtime_template_vars={})

    ok = await svc.checkpoint_session(agent, "user1", "ckpt_empty")
    assert ok is False

    # Session file must not have been created
    with pytest.raises(SessionNotFoundError):
        await sm.load_session("user1", "ckpt_empty")


@pytest.mark.asyncio
async def test_start_stop_checkpoint_loop_runs_periodically(session_service_env):
    """Background loop runs at the configured interval, stops cleanly on cancel."""
    svc, sm = session_service_env
    svc.checkpoint_interval_seconds = 1  # fast for the test

    await sm.create_session(
        user_id="user1",
        session_id="loop_001",
        title="Loop test",
        agent_name="test_agent",
        llm_profile="normal",
    )

    msgs = [{"role": "user", "content": "ping"}]
    agent = _make_checkpoint_agent(msgs)

    svc.start_checkpoint_loop(agent, "user1", "loop_001")
    assert "loop_001" in svc._checkpoint_tasks
    # Wait long enough for the loop to fire at least once
    await asyncio.sleep(1.3)

    loaded = await sm.load_session("user1", "loop_001")
    assert len(loaded["messages"]) == 1

    await svc.stop_checkpoint_loop("loop_001")
    assert "loop_001" not in svc._checkpoint_tasks


@pytest.mark.asyncio
async def test_start_checkpoint_loop_idempotent(session_service_env):
    """Calling start twice for the same session does not spawn a second task."""
    svc, sm = session_service_env
    svc.checkpoint_interval_seconds = 60  # don't actually fire during test

    agent = _make_checkpoint_agent([{"role": "user", "content": "x"}])

    svc.start_checkpoint_loop(agent, "user1", "idem_001")
    first_task = svc._checkpoint_tasks["idem_001"]
    svc.start_checkpoint_loop(agent, "user1", "idem_001")
    second_task = svc._checkpoint_tasks["idem_001"]

    assert first_task is second_task

    await svc.stop_checkpoint_loop("idem_001")


@pytest.mark.asyncio
async def test_disabled_when_interval_zero(session_service_env):
    """checkpoint_interval_seconds=0 disables the loop entirely."""
    svc, sm = session_service_env
    svc.checkpoint_interval_seconds = 0

    agent = _make_checkpoint_agent([{"role": "user", "content": "x"}])
    svc.start_checkpoint_loop(agent, "user1", "off_001")
    assert "off_001" not in svc._checkpoint_tasks
