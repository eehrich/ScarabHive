"""Tests for sub-agent context injection hook."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, UTC

from plugins.sub_agent_manager.hooks import SubAgentContextInjector
from agent_system.hooks.plugin_hook import HookContext, HookType
from agent_system.llm.models import ChatMessage


@pytest.fixture
def mock_manager():
    """Create mock SubAgentManager."""
    manager = MagicMock()
    manager.list_sub_sessions = AsyncMock()
    return manager


@pytest.fixture
def injector(mock_manager):
    """Create SubAgentContextInjector with default config."""
    config = {
        "max_sub_agents_shown": 10,
        "show_completed": False,
        "show_tool_state": True,
        "format": "markdown"
    }
    return SubAgentContextInjector(mock_manager, config)


@pytest.mark.asyncio
async def test_inject_context_no_sub_agents(injector, mock_manager):
    """Test that hook skips injection when no sub-agents exist."""
    # Mock empty sub-agent list
    mock_manager.list_sub_sessions.return_value = []
    
    # Create context
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req_123",
        session_id="test_session_456",
        agent=MagicMock(name="coordinator"),
        agent_name="coordinator",
        messages=[ChatMessage(role="user", content="Hello")],
        step=1
    )
    
    # Execute hook
    result = await injector.inject_sub_agent_context(context)
    
    # Verify no modification
    assert result.success is True
    assert result.modified is False
    assert len(context.messages) == 1


@pytest.mark.asyncio
async def test_inject_context_with_sub_agents(injector, mock_manager):
    """Test that hook injects context when sub-agents exist."""
    # Mock sub-agent data
    sub_agents = [
        {
            "instance_id": "parent_sub_web_research_001",
            "agent_type": "web_research_agent",
            "status": "active",
            "message_count": 12,
            "task_summary": "Research latest AI regulations",
            "last_used": datetime.now(UTC).isoformat(),
            "tools_used": ["duckduckgo_search", "web_scraper"]
        },
        {
            "instance_id": "parent_sub_financial_analyst_002",
            "agent_type": "financial_analyst_agent",
            "status": "completed",
            "message_count": 45,
            "task_summary": "Q4 financial analysis for TSLA",
            "last_used": datetime.now(UTC).isoformat(),
            "tools_used": ["yahoo_finance", "calculator"]
        }
    ]
    
    mock_manager.list_sub_sessions.return_value = sub_agents
    
    # Create context with initial messages
    messages = [
        ChatMessage(role="system", content="You are a coordinator"),
        ChatMessage(role="user", content="Hello")
    ]
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req_123",
        session_id="test_session_456",
        agent=MagicMock(name="coordinator"),
        agent_name="coordinator",
        messages=messages,
        step=1
    )
    
    # Execute hook
    result = await injector.inject_sub_agent_context(context)
    
    # Verify modification
    assert result.success is True
    assert result.modified is True
    assert result.metadata.get("sub_agents_count") == 2
    
    # Verify messages were modified
    assert len(context.messages) == 3  # system + injected + user
    
    # Verify injected message is system role
    injected_msg = context.messages[1]
    assert injected_msg.role == "system"
    assert "Active Sub-Agents" in injected_msg.content
    assert "web_research_agent" in injected_msg.content
    assert "financial_analyst_agent" in injected_msg.content
    assert "parent_sub_web_research_001" in injected_msg.content


@pytest.mark.asyncio
async def test_inject_context_limits_max_shown(mock_manager):
    """Test that hook respects max_sub_agents_shown configuration."""
    # Create injector with max_shown=2
    config = {
        "max_sub_agents_shown": 2,
        "show_completed": True,
        "format": "markdown"
    }
    injector = SubAgentContextInjector(mock_manager, config)
    
    # Mock 5 sub-agents
    sub_agents = [
        {
            "instance_id": f"parent_sub_agent_{i:03d}",
            "agent_type": "basic_agent",
            "status": "active",
            "message_count": 10,
            "task_summary": f"Task {i}",
            "last_used": datetime.now(UTC).isoformat()
        }
        for i in range(5)
    ]
    
    mock_manager.list_sub_sessions.return_value = sub_agents
    
    # Create context
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req",
        session_id="test_session",
        agent=MagicMock(),
        agent_name="coordinator",
        messages=[ChatMessage(role="user", content="Test")],
        step=1
    )
    
    # Execute hook
    result = await injector.inject_sub_agent_context(context)
    
    # Verify only 2 sub-agents shown
    assert result.success is True
    assert result.modified is True
    assert result.metadata.get("sub_agents_count") == 2
    
    injected_content = context.messages[0].content  # Inserted before user message
    assert "parent_sub_agent_000" in injected_content
    assert "parent_sub_agent_001" in injected_content
    # Should NOT contain agents 2, 3, 4
    assert "parent_sub_agent_002" not in injected_content


@pytest.mark.asyncio
async def test_inject_context_no_session_id(injector):
    """Test that hook skips when no session_id in context."""
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req",
        session_id=None,  # No session!
        agent=MagicMock(),
        agent_name="test",
        messages=[ChatMessage(role="user", content="Test")],
        step=1
    )
    
    result = await injector.inject_sub_agent_context(context)
    
    assert result.success is True
    assert result.modified is False


@pytest.mark.asyncio
async def test_inject_context_manager_error_handling(injector, mock_manager):
    """Test that hook handles manager errors gracefully."""
    # Mock manager to raise exception
    mock_manager.list_sub_sessions.side_effect = Exception("Database error")
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req",
        session_id="test_session",
        agent=MagicMock(),
        agent_name="test",
        messages=[ChatMessage(role="user", content="Test")],
        step=1
    )
    
    # Execute hook
    result = await injector.inject_sub_agent_context(context)
    
    # Should not fail, just skip injection
    assert result.success is True
    assert result.modified is False


@pytest.mark.asyncio
async def test_markdown_context_format(injector, mock_manager):
    """Test Markdown formatting of injected context."""
    sub_agents = [
        {
            "instance_id": "test_sub_001",
            "agent_type": "test_agent",
            "status": "active",
            "message_count": 5,
            "task_summary": "Test task",
            "last_used": "2025-01-15T10:00:00Z",
            "tools_used": ["tool1", "tool2"]
        }
    ]
    
    mock_manager.list_sub_sessions.return_value = sub_agents
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req",
        session_id="test_session",
        agent=MagicMock(),
        agent_name="test",
        messages=[ChatMessage(role="user", content="Test")],
        step=1
    )
    
    await injector.inject_sub_agent_context(context)
    
    injected_content = context.messages[0].content
    
    # Check Markdown table formatting (minimal: Type, Instance ID, Status)
    assert "## Active Sub-Agents" in injected_content
    assert "| Type | Instance ID | Status |" in injected_content
    assert "test_agent" in injected_content
    assert "`test_sub_001`" in injected_content
    assert "active" in injected_content
    assert "manage_sub_agent" in injected_content  # Continue example


@pytest.mark.asyncio
async def test_text_context_format(mock_manager):
    """Test plain text formatting of injected context."""
    config = {
        "format": "text",
        "show_completed": True
    }
    injector = SubAgentContextInjector(mock_manager, config)
    
    sub_agents = [
        {
            "instance_id": "test_sub_001",
            "agent_type": "test_agent",
            "status": "active",
            "message_count": 5,
            "task_summary": "Test task",
            "last_used": datetime.now(UTC).isoformat()
        }
    ]
    
    mock_manager.list_sub_sessions.return_value = sub_agents
    
    context = HookContext(
        hook_type=HookType.PRE_LLM_CALL,
        request_id="test_req",
        session_id="test_session",
        agent=MagicMock(),
        agent_name="test",
        messages=[ChatMessage(role="user", content="Test")],
        step=1
    )
    
    await injector.inject_sub_agent_context(context)
    
    injected_content = context.messages[0].content
    
    # Check text formatting (no Markdown)
    assert "ACTIVE SUB-AGENTS:" in injected_content
    assert "1. test_agent (test_sub_001)" in injected_content
    assert "Status: active | Messages: 5" in injected_content
    assert "Task: Test task" in injected_content
    assert "manage_sub_agent" in injected_content
