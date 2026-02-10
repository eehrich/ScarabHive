"""Tests for Sequential Thinking Plugin Hook Integration.

Tests the on_pre_llm_call hook that injects active sessions into system prompt.
"""

import pytest
from unittest.mock import AsyncMock

from agent_system.config.models import AgentSystemConfig, MCPConfig
from agent_system.hooks import HookContext, HookType
from agent_system.llm.models import ChatMessage
from plugins.sequential_thinking.server import SequentialThinkingServer


@pytest.fixture
def system_config():
    """Create minimal system config."""
    return AgentSystemConfig()


@pytest.fixture
def server(system_config):
    """Create SequentialThinkingServer instance with hook config."""
    mcp_config = MCPConfig(
        type="sequential_thinking",
        enabled=True,
        max_history_size=100,
        session_ttl_seconds=3600,
        enable_branching=True,
        enable_revisions=True,
        max_summary_thoughts=10,
        # Hook config
        max_thoughts_in_prompt=5,
        show_branch_info=True,
        format="markdown"
    )
    
    return SequentialThinkingServer(
        name="sequential_thinking",
        system_config=system_config,
        mcp_config=mcp_config
    )


@pytest.fixture
def mock_status():
    """Provide mock status object."""
    status = AsyncMock()
    status.start = AsyncMock()
    status.progress = AsyncMock()
    status.end = AsyncMock()
    status.error = AsyncMock()
    return status


@pytest.mark.asyncio
async def test_hook_no_messages(server):
    """Test hook with empty messages list."""
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        session_id="test-session",
        agent=None,
        messages=[],
    )
    
    result = await server.on_pre_llm_call(context)
    
    assert result.success is True
    assert result.modified is False
    assert len(context.messages) == 0


@pytest.mark.asyncio
async def test_hook_no_session_id(server):
    """Test hook without session_id in context."""
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        session_id=None,
        agent=None,
        messages=[ChatMessage(role="system", content="Test")],
    )
    
    result = await server.on_pre_llm_call(context)
    
    assert result.success is True
    assert result.modified is False


@pytest.mark.asyncio
async def test_hook_inject_reminder_no_active_session(server):
    """Test hook injects tool reminder when no active session exists."""
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        session_id="non-existent-session",
        agent=None,
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant"),
            ChatMessage(role="user", content="Hello")
        ],
    )
    
    result = await server.on_pre_llm_call(context)
    
    assert result.success is True
    assert result.modified is True
    assert len(context.messages) == 3  # Original 2 + 1 injection
    
    # Check injection content
    injected = context.messages[1]  # After first system message
    assert injected.role == "system"
    assert "Sequential Thinking Tool Available" in injected.content
    assert "sequential_thinking()" in injected.content


@pytest.mark.asyncio
async def test_hook_inject_active_session(server, mock_status):
    """Test hook injects active session thoughts into system prompt."""
    # Create a session with some thoughts
    agent_session_id = "test-agent-session"  # Agent conversation session ID
    
    params1 = {
        "thought": "First thought: analyze the problem",
        "next_thought_needed": True,
        "thought_number": 1,
        "total_thoughts": 3,
        "_status": mock_status,
        "_session_id": agent_session_id  # Link to agent session
    }
    result1 = await server.call("sequential_thinking", params1)
    thinking_session_id = result1["session_id"]  # Sequential thinking session ID
    
    params2 = {
        "thought": "Second thought: consider alternatives",
        "next_thought_needed": True,
        "thought_number": 2,
        "total_thoughts": 3,
        "session_id": thinking_session_id,
        "_status": mock_status,
        "_session_id": agent_session_id  # Same agent session
    }
    result2 = await server.call("sequential_thinking", params2)
    assert result2["status"] == "success"
    
    # Now test the hook with the AGENT session ID (not thinking session ID)
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        session_id=agent_session_id,  # Agent session ID for hook lookup
        agent=None,
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant"),
            ChatMessage(role="user", content="Continue thinking")
        ],
    )
    
    result = await server.on_pre_llm_call(context)
    
    assert result.success is True
    assert result.modified is True
    assert len(context.messages) == 3
    
    # Check injection content
    injected = context.messages[1]
    assert injected.role == "system"
    assert "Active Sequential Thinking Session" in injected.content
    assert "Thought #1" in injected.content
    assert "Thought #2" in injected.content
    assert "**Progress**: 2/3 thoughts" in injected.content


@pytest.mark.asyncio
async def test_hook_with_branches(server, mock_status):
    """Test hook shows branch information."""
    agent_session_id = "test-agent-session"
    
    # Create main branch thoughts
    params1 = {
        "thought": "Main branch thought",
        "next_thought_needed": True,
        "thought_number": 1,
        "total_thoughts": 5,
        "_status": mock_status,
        "_session_id": agent_session_id
    }
    result1 = await server.call("sequential_thinking", params1)
    thinking_session_id = result1["session_id"]
    
    # Create alternative branch
    params2 = {
        "thought": "Alternative approach",
        "next_thought_needed": True,
        "thought_number": 2,
        "total_thoughts": 5,
        "session_id": thinking_session_id,
        "branch_from_thought": 1,
        "branch_id": "alternative",
        "_status": mock_status,
        "_session_id": agent_session_id
    }
    result2 = await server.call("sequential_thinking", params2)
    assert result2["status"] == "success"
    
    # Test hook with branched session
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        session_id=agent_session_id,
        agent=None,
        messages=[
            ChatMessage(role="system", content="System"),
            ChatMessage(role="user", content="User")
        ],
    )
    
    result = await server.on_pre_llm_call(context)
    
    assert result.success is True
    assert result.modified is True
    
    injected = context.messages[1]
    assert "Current branch" in injected.content
    assert "alternative" in injected.content
    assert "Available branches" in injected.content


@pytest.mark.asyncio
async def test_hook_removes_old_injection(server, mock_status):
    """Test hook removes old injection before adding new one."""
    agent_session_id = "test-agent-session"
    
    params = {
        "thought": "Test thought",
        "next_thought_needed": False,
        "thought_number": 1,
        "total_thoughts": 1,
        "_status": mock_status,
        "_session_id": agent_session_id
    }
    result = await server.call("sequential_thinking", params)
    # thinking_session_id = result["session_id"]  # Not needed for this test
    
    # Create context with old injection already present (tagged with injected_by)
    old_injection = ChatMessage(
        role="system",
        content="## Sequential Thinking Tool Available\n\nOld injection content...",
        injected_by="sequential_thinking"
    )
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        session_id=agent_session_id,
        agent=None,
        messages=[
            ChatMessage(role="system", content="System"),
            old_injection,  # Old injection should be removed
            ChatMessage(role="user", content="User")
        ],
    )
    
    original_count = len(context.messages)
    result = await server.on_pre_llm_call(context)
    
    assert result.success is True
    assert result.modified is True
    # Should still be same count (removed old, added new)
    assert len(context.messages) == original_count
    
    # Check that old injection was replaced with new one
    system_messages = [msg for msg in context.messages if msg.role == "system"]
    injections = [msg for msg in system_messages if "Sequential Thinking" in msg.content]
    assert len(injections) == 1  # Only one injection
    assert "Active Sequential Thinking Session" in injections[0].content  # New injection


@pytest.mark.asyncio
async def test_hook_max_thoughts_limit(server, mock_status):
    """Test hook respects max_thoughts_in_prompt config."""
    agent_session_id = "test-agent-session"
    
    # Create 10 thoughts
    thinking_session_id = None
    for i in range(1, 11):
        params = {
            "thought": f"Thought number {i}",
            "next_thought_needed": i < 10,
            "thought_number": i,
            "total_thoughts": 10,
            "session_id": thinking_session_id,
            "_status": mock_status,
            "_session_id": agent_session_id
        }
        result = await server.call("sequential_thinking", params)
        if thinking_session_id is None:
            thinking_session_id = result["session_id"]
    
    # Hook should only show last 5 thoughts (max_thoughts_in_prompt=5)
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        session_id=agent_session_id,
        agent=None,
        messages=[
            ChatMessage(role="system", content="System"),
            ChatMessage(role="user", content="User")
        ],
    )
    
    result = await server.on_pre_llm_call(context)
    
    assert result.success is True
    assert result.modified is True
    
    injected = context.messages[1]
    # Should show thoughts 6-10 (last 5)
    assert "Thought #6" in injected.content
    assert "Thought #10" in injected.content
    # Should NOT show earlier thoughts in the list (but may be in example text)
    thought_list = injected.content.split("**Recent thoughts:**")[1]
    assert "Thought #1:" not in thought_list
    assert "Thought #5:" not in thought_list


@pytest.mark.asyncio
async def test_hook_truncates_long_thoughts(server, mock_status):
    """Test hook truncates thoughts longer than 150 chars."""
    agent_session_id = "test-agent-session"
    long_thought = "A" * 200  # 200 chars
    
    params = {
        "thought": long_thought,
        "next_thought_needed": False,
        "thought_number": 1,
        "total_thoughts": 1,
        "_status": mock_status,
        "_session_id": agent_session_id
    }
    result = await server.call("sequential_thinking", params)
    # thinking_session_id = result["session_id"]  # Not needed
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        session_id=agent_session_id,
        agent=None,
        messages=[
            ChatMessage(role="system", content="System"),
            ChatMessage(role="user", content="User")
        ],
    )
    
    result = await server.on_pre_llm_call(context)
    
    assert result.success is True
    assert result.modified is True
    
    injected = context.messages[1]
    # Should contain truncated version with "..."
    assert "..." in injected.content
    # Full thought should NOT be in injection
    assert long_thought not in injected.content


@pytest.mark.asyncio
async def test_hook_with_revision(server, mock_status):
    """Test hook shows revision indicator."""
    agent_session_id = "test-agent-session"
    
    # Original thought
    params1 = {
        "thought": "Original thought",
        "next_thought_needed": True,
        "thought_number": 1,
        "total_thoughts": 3,
        "_status": mock_status,
        "_session_id": agent_session_id
    }
    result1 = await server.call("sequential_thinking", params1)
    thinking_session_id = result1["session_id"]
    
    # Revision of first thought
    params2 = {
        "thought": "Revised version of first thought",
        "next_thought_needed": False,
        "thought_number": 2,
        "total_thoughts": 3,
        "session_id": thinking_session_id,
        "is_revision": True,
        "revises_thought": 1,
        "_status": mock_status,
        "_session_id": agent_session_id
    }
    result2 = await server.call("sequential_thinking", params2)
    assert result2["status"] == "success"
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        session_id=agent_session_id,
        agent=None,
        messages=[
            ChatMessage(role="system", content="System"),
            ChatMessage(role="user", content="User")
        ],
    )
    
    result = await server.on_pre_llm_call(context)
    
    assert result.success is True
    assert result.modified is True
    
    injected = context.messages[1]
    # Should show revision indicator
    assert "(revises #1)" in injected.content


@pytest.mark.asyncio
async def test_hook_error_handling(server):
    """Test hook handles errors gracefully."""
    # Create a context that might cause an error
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        session_id="test-session",
        agent=None,
        messages=[
            ChatMessage(role="system", content="System"),
        ],
    )
    
    # Even if something goes wrong, hook should return success=True, modified=False
    # This ensures LLM call doesn't fail due to hook issues
    result = await server.on_pre_llm_call(context)
    
    assert result.success is True
    # Should either succeed with injection or fail gracefully
    assert isinstance(result.modified, bool)
