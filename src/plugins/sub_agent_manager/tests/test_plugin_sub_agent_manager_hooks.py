"""Tests for sub-agent context injection hook."""
import pytest

from agent_system.llm.message_roles import DEVELOPER
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, UTC

from plugins.sub_agent_manager.hooks import SubAgentContextInjector
from agent_system.hooks.plugin_hook import HookContext, HookType
from agent_system.llm.models import ChatMessage


async def stored_status(metadata):
    """What the injector is told a sub-agent is doing: here, the stored status as it stands."""
    return metadata.get("status", "unknown")


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
        "format": "markdown"
    }
    return SubAgentContextInjector(mock_manager, "test_sam", config, status_of=stored_status)


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
    injected_msg = context.messages[-1]
    assert injected_msg.role == DEVELOPER
    assert "## Sub-Agents" in injected_msg.content
    assert "web_research_agent" in injected_msg.content
    assert "financial_analyst_agent" in injected_msg.content
    assert "parent_sub_web_research_001" in injected_msg.content


@pytest.mark.asyncio
async def test_inject_context_limits_max_shown(mock_manager):
    """Test that hook respects max_sub_agents_shown configuration.
    
    The hook sorts sub-agents by created_at (newest first) and shows
    only the N newest ones where N = max_sub_agents_shown.
    """
    # Create injector with max_shown=2
    config = {
        "max_sub_agents_shown": 2,
        "show_completed": True,
        "format": "markdown"
    }
    injector = SubAgentContextInjector(mock_manager, "test_sam", config, status_of=stored_status)
    
    # Mock 5 sub-agents with distinct creation times (index 4 is newest), listed
    # shuffled and with last_used running the other way: neither the input order
    # nor last_used can pass for the creation order.
    base_time = datetime.now(UTC)
    sub_agents = [
        {
            "instance_id": f"parent_sub_agent_{i:03d}",
            "agent_type": "basic_agent",
            "status": "active",
            "message_count": 10,
            "task_summary": f"Task {i}",
            "created_at": (base_time + timedelta(seconds=i)).isoformat(),
            "last_used": (base_time - timedelta(seconds=i)).isoformat(),
        }
        for i in (2, 4, 0, 3, 1)
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
    
    injected_content = context.messages[-1].content  # Inserted before user message
    # Should contain the 2 most recent (indices 4 and 3)
    assert "parent_sub_agent_004" in injected_content
    assert "parent_sub_agent_003" in injected_content
    # Should NOT contain older agents (0, 1, 2)
    assert "parent_sub_agent_000" not in injected_content
    assert "parent_sub_agent_001" not in injected_content
    assert "parent_sub_agent_002" not in injected_content


@pytest.mark.asyncio
async def test_the_cut_keeps_the_open_sub_agents_before_newer_failed_ones(mock_manager):
    """Failed and cancelled rows are listed now, and by creation time alone they sort in
    ahead of older open ones: a coordinator that replaced two failed workers had the two
    workers it can still use cut off behind them."""
    injector = SubAgentContextInjector(mock_manager, "test_sam", {"max_sub_agents_shown": 4},
                                       status_of=stored_status)
    base_time = datetime.now(UTC)
    rows = [("worker_a", "active"), ("worker_b", "active"), ("worker_c", "failed"),
            ("worker_d", "cancelled"), ("worker_e", "active"), ("worker_f", "interrupted")]
    mock_manager.list_sub_sessions.return_value = [
        {"instance_id": instance_id, "agent_type": "basic_agent", "status": status,
         "task_summary": f"Task {n}", "created_at": (base_time + timedelta(seconds=n)).isoformat()}
        for n, (instance_id, status) in enumerate(rows)
    ]
    context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="test_req",
                          session_id="test_session", agent=MagicMock(), agent_name="coordinator",
                          messages=[ChatMessage(role="user", content="Test")], step=1)

    await injector.inject_sub_agent_context(context)

    block = context.messages[-1].content
    assert [instance_id for instance_id, _ in rows if instance_id in block] == [
        "worker_a", "worker_b", "worker_e", "worker_f"]


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
    
    injected_content = context.messages[-1].content
    
    # Check Markdown table formatting
    assert "## Sub-Agents" in injected_content
    assert "| Type | Instance ID | Status | Task |" in injected_content
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
    injector = SubAgentContextInjector(mock_manager, "test_sam", config, status_of=stored_status)
    
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
    
    injected_content = context.messages[-1].content
    
    # Check text formatting (no Markdown)
    assert injected_content.startswith("SUB-AGENTS:")
    assert "1. test_agent (test_sub_001)" in injected_content
    assert "Status: active" in injected_content
    assert "Messages" not in injected_content  # grows on every continue
    assert "Task: Test task" in injected_content
    assert "manage_sub_agent" in injected_content


@pytest.mark.asyncio
async def test_the_block_names_the_phase_the_agent_config_sets_as_create_does(mock_manager):
    """No phase in the session's vars yet: create judges by the agent's configured default
    (server._get_current_phase), so the block must list that phase's agents, not every allowed one."""
    mock_manager.list_sub_sessions.return_value = [
        {"instance_id": "sub_planner_0001", "agent_type": "planner", "status": "active", "task_summary": "Plan"}]
    injector = SubAgentContextInjector(
        mock_manager, "test_sam", {"format": "markdown"}, ["planner", "builder"],
        {"enabled": True, "phase_variable": "workflow_phase", "phase_agents": {"planning": ["planner"]}},
        status_of=stored_status)
    agent = MagicMock()
    agent._session_tracker.get_session_template_vars.return_value = {}
    agent.agent_config.template_vars = {"workflow_phase": "planning"}
    context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="s", agent=agent,
                          agent_name="coordinator", messages=[ChatMessage(role="user", content="Go")], step=1)

    await injector.inject_sub_agent_context(context)

    block = context.messages[-1].content
    assert "**Phase `planning` - Available agents:** planner" in block
    assert "builder" not in block
