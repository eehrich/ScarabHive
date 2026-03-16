"""Tests for SessionService including token estimation."""

import pytest
from agent_system.services.session_service import _estimate_message_tokens


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
from agent_system.services.session_manager import SessionManager


def _make_mock_agent(messages_dicts):
    """Build a minimal mock agent whose session_tracker holds *messages_dicts*."""
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
