"""Tests for sequential thinking UX improvements (relative timestamps, quick actions, multi-session)."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from agent_system.hooks.plugin_hook import HookContext
from agent_system.hooks import HookType
from agent_system.llm.models import ChatMessage


@pytest.fixture
def plugin_server():
    """Create SequentialThinkingServer instance with test config."""
    from plugins.sequential_thinking.server import SequentialThinkingServer
    from agent_system.config.models import MCPConfig, AgentSystemConfig
    
    # System config
    system_config = AgentSystemConfig()
    
    # Mock config with UX improvement settings
    mcp_config = MCPConfig(
        type="sequential_thinking",
        enabled=True,
        max_history_size=100,
        session_ttl_seconds=3600,
        enable_branching=True,
        enable_revisions=True,
        max_summary_thoughts=10,
        max_thoughts_in_prompt=5,
        show_branch_info=True,
        format="markdown",
        show_relative_timestamps=True,
        show_quick_actions=True,
        max_sessions_in_prompt=2
    )
    
    return SequentialThinkingServer(
        name="sequential_thinking",
        system_config=system_config,
        mcp_config=mcp_config
    )


@pytest.mark.asyncio
async def test_relative_timestamp_formatting(plugin_server):
    """Test that relative timestamps are correctly formatted."""
    now = datetime.now()
    
    # Test seconds
    dt = now - timedelta(seconds=30)
    assert plugin_server._relative_time(dt) == "30s ago"
    
    # Test minutes
    dt = now - timedelta(minutes=5)
    assert plugin_server._relative_time(dt) == "5m ago"
    
    # Test hours
    dt = now - timedelta(hours=2)
    assert plugin_server._relative_time(dt) == "2h ago"
    
    # Test days
    dt = now - timedelta(days=3)
    assert plugin_server._relative_time(dt) == "3d ago"


@pytest.mark.asyncio
async def test_hook_with_relative_timestamps(plugin_server):
    """Test that hook includes relative timestamps when enabled."""
    agent_session_id = "test_agent_session"
    
    # Create a thinking session with some thoughts
    thinking_session_id = None
    
    # Add first thought
    params = {
        "thought": "Initial analysis",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": AsyncMock(),
        "_session_id": agent_session_id
    }
    result = await plugin_server.call("sequential_thinking", params)
    assert result["status"] == "success"
    thinking_session_id = result["session_id"]
    
    # Add second thought a bit later
    params.update({
        "thought": "Deeper investigation",
        "thought_number": 2,
        "session_id": thinking_session_id,
        "_session_id": agent_session_id
    })
    await plugin_server.call("sequential_thinking", params)
    
    # Call hook
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        messages=[ChatMessage(role="system", content="You are helpful")],
        session_id=agent_session_id,
        agent=MagicMock()
    )
    
    result = await plugin_server.on_pre_llm_call(context)
    
    assert result.success
    assert result.modified
    
    # Check that prompt includes relative timestamp indicators
    injected_msg = result.context.messages[1]
    assert "**Started**:" in injected_msg.content
    assert "ago" in injected_msg.content  # Should show "Xs ago" or similar


@pytest.mark.asyncio
async def test_hook_with_quick_actions(plugin_server):
    """Test that hook includes quick action hints when enabled."""
    agent_session_id = "test_agent_session"
    
    # Create session with thought
    params = {
        "thought": "Test thought",
        "thought_number": 1,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": AsyncMock(),
        "_session_id": agent_session_id
    }
    result = await plugin_server.call("sequential_thinking", params)
    thinking_session_id = result["session_id"]
    
    # Call hook
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        messages=[ChatMessage(role="system", content="You are helpful")],
        session_id=agent_session_id,
        agent=MagicMock()
    )
    
    result = await plugin_server.on_pre_llm_call(context)
    
    assert result.success
    assert result.modified
    
    # Check for parameter groups and examples section (replaces old "Quick actions")
    injected_msg = result.context.messages[1]
    assert "**Parameter Groups:**" in injected_msg.content
    assert "**Examples:**" in injected_msg.content
    assert f"sequential_thinking(session_id='{thinking_session_id}'" in injected_msg.content


@pytest.mark.asyncio
async def test_hook_without_quick_actions(plugin_server):
    """Test that quick actions are hidden when config disabled."""
    # Disable quick actions
    plugin_server.mcp_config.show_quick_actions = False
    
    agent_session_id = "test_agent_session"
    
    # Create session
    params = {
        "thought": "Test thought",
        "thought_number": 1,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": AsyncMock(),
        "_session_id": agent_session_id
    }
    await plugin_server.call("sequential_thinking", params)
    
    # Call hook
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        messages=[ChatMessage(role="system", content="You are helpful")],
        session_id=agent_session_id,
        agent=MagicMock()
    )
    
    result = await plugin_server.on_pre_llm_call(context)
    
    # Should NOT include quick actions
    injected_msg = result.context.messages[1]
    assert "**Quick actions:**" not in injected_msg.content
    assert "Continue reasoning with `sequential_thinking()`" in injected_msg.content


@pytest.mark.asyncio
async def test_hook_multiple_sessions_display(plugin_server):
    """Test that hook can display multiple sessions when max_sessions_in_prompt > 1."""
    agent_session_id = "test_agent_session"
    
    # Create first thinking session
    params1 = {
        "thought": "First session thought",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": AsyncMock(),
        "_session_id": agent_session_id
    }
    result1 = await plugin_server.call("sequential_thinking", params1)
    session1_id = result1["session_id"]
    
    # Create second thinking session - explicitly request new session with unique ID
    params2 = {
        "thought": "Second session thought",
        "thought_number": 1,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "session_id": "new_session_" + agent_session_id,  # Explicit new session ID
        "_status": AsyncMock(),
        "_session_id": agent_session_id
    }
    result2 = await plugin_server.call("sequential_thinking", params2)
    session2_id = result2["session_id"]
    
    # Configure to show 2 sessions
    plugin_server.mcp_config.max_sessions_in_prompt = 2
    
    # Call hook
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        messages=[ChatMessage(role="system", content="You are helpful")],
        session_id=agent_session_id,
        agent=MagicMock()
    )
    
    result = await plugin_server.on_pre_llm_call(context)
    
    assert result.success
    assert result.modified
    
    # Check that prompt includes both sessions
    injected_msg = result.context.messages[1]
    assert "## Active Sequential Thinking Sessions (2)" in injected_msg.content
    assert session1_id in injected_msg.content
    assert session2_id in injected_msg.content
    assert "### Session 1:" in injected_msg.content
    assert "### Session 2:" in injected_msg.content


@pytest.mark.asyncio
async def test_hook_limits_sessions_displayed(plugin_server):
    """Test that hook respects max_sessions_in_prompt limit."""
    agent_session_id = "test_agent_session"
    
    # Create 3 thinking sessions - each with explicit unique session_id
    session_ids = []
    for i in range(3):
        params = {
            "thought": f"Session {i+1} thought",
            "thought_number": 1,
            "total_thoughts": 3,
            "next_thought_needed": True,
            "session_id": f"session_{i}_" + agent_session_id,  # Explicit unique session ID
            "_status": AsyncMock(),
            "_session_id": agent_session_id
        }
        result = await plugin_server.call("sequential_thinking", params)
        session_ids.append(result["session_id"])
    
    # Configure to show only 1 session
    plugin_server.mcp_config.max_sessions_in_prompt = 1
    
    # Call hook
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        messages=[ChatMessage(role="system", content="You are helpful")],
        session_id=agent_session_id,
        agent=MagicMock()
    )
    
    result = await plugin_server.on_pre_llm_call(context)
    
    # Should show single session format (not multi-session)
    injected_msg = result.context.messages[1]
    assert "## Active Sequential Thinking Session\n" in injected_msg.content
    assert "## Active Sequential Thinking Sessions" not in injected_msg.content
    
    # Should show most recent session (session 3, the last one created)
    # The sessions are sorted by last_accessed, so the most recent is shown
    assert session_ids[2] in injected_msg.content or session_ids[1] in injected_msg.content


@pytest.mark.asyncio
async def test_hook_shows_branch_switch_action_with_branches(plugin_server):
    """Test that quick actions include branch switch hint when branches exist."""
    agent_session_id = "test_agent_session"
    
    # Create session with branching
    params = {
        "thought": "Main branch thought",
        "thought_number": 1,
        "total_thoughts": 5,
        "next_thought_needed": True,
        "_status": AsyncMock(),
        "_session_id": agent_session_id
    }
    result = await plugin_server.call("sequential_thinking", params)
    session_id = result["session_id"]
    
    # Create a branch
    params.update({
        "thought": "Alternative approach",
        "thought_number": 2,
        "branch_from_thought": 1,
        "branch_id": "alternative",
        "session_id": session_id,
        "_session_id": agent_session_id
    })
    await plugin_server.call("sequential_thinking", params)
    
    # Call hook
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        messages=[ChatMessage(role="system", content="You are helpful")],
        session_id=agent_session_id,
        agent=MagicMock()
    )
    
    result = await plugin_server.on_pre_llm_call(context)
    
    # Should include branch switch hint
    injected_msg = result.context.messages[1]
    assert "Switch branch:" in injected_msg.content or "branch_id=" in injected_msg.content


@pytest.mark.asyncio
async def test_relative_timestamps_can_be_disabled(plugin_server):
    """Test that relative timestamps can be disabled via config."""
    # Disable relative timestamps
    plugin_server.mcp_config.show_relative_timestamps = False
    
    agent_session_id = "test_agent_session"
    
    # Create session
    params = {
        "thought": "Test thought",
        "thought_number": 1,
        "total_thoughts": 3,
        "next_thought_needed": True,
        "_status": AsyncMock(),
        "_session_id": agent_session_id
    }
    await plugin_server.call("sequential_thinking", params)
    
    # Call hook
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test-req",
        messages=[ChatMessage(role="system", content="You are helpful")],
        session_id=agent_session_id,
        agent=MagicMock()
    )
    
    result = await plugin_server.on_pre_llm_call(context)
    
    # Should NOT include "Started:" or timestamp indicators
    injected_msg = result.context.messages[1]
    assert "Started:" not in injected_msg.content
    # Should still show progress
    assert "**Progress**:" in injected_msg.content
