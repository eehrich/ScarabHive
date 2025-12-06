"""Tests for Sub-Agent Manager plugin hook duplicate injection prevention."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent_system.hooks.plugin_hook import HookContext, HookResult
from agent_system.llm.models import ChatMessage
from plugins.sub_agent_manager.hooks import SubAgentContextInjector
from plugins.sub_agent_manager.manager import SubAgentManager


@pytest.fixture
def mock_manager():
    """Create mock SubAgentManager."""
    manager = MagicMock(spec=SubAgentManager)
    
    # Mock list_sub_sessions to return test data
    async def mock_list_sub_sessions(parent_session_id, include_completed=False):
        return [
            {
                "instance_id": "sub_123",
                "agent_type": "research_agent",
                "status": "active",
                "message_count": 5,
                "task_summary": "Research Python best practices",
                "last_used": "2025-11-18T10:00:00Z",
                "tools_used": ["web_search", "memory"]
            },
            {
                "instance_id": "sub_456",
                "agent_type": "code_agent",
                "status": "active",
                "message_count": 3,
                "task_summary": "Implement feature X",
                "last_used": "2025-11-18T09:30:00Z",
                "tools_used": ["file_ops"]
            }
        ]
    
    manager.list_sub_sessions = AsyncMock(side_effect=mock_list_sub_sessions)
    return manager


@pytest.fixture
def injector(mock_manager):
    """Create SubAgentContextInjector with mock manager."""
    config = {
        "enabled": True,
        "max_sub_agents_shown": 10,
        "show_completed": False,
        "show_tool_state": True,
        "format": "markdown"
    }
    return SubAgentContextInjector(mock_manager, config)


@pytest.mark.asyncio
async def test_hook_injects_sub_agents_once(injector):
    """Test that sub-agent list is injected only once."""
    context = HookContext(
        hook_type="inject_sub_agent_context",
        request_id="test_req_001",
        session_id="test_session_123",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="What sub-agents are active?")
        ]
    )
    
    # First injection
    result1 = await injector.inject_sub_agent_context(context)
    
    assert result1.success is True
    assert result1.modified is True
    
    # Count system messages with sub-agent marker
    sub_agent_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "## Active Sub-Agents" in msg.content
    ]
    assert len(sub_agent_messages) == 1, "Should have exactly one sub-agent injection"
    
    # Second call (simulate multiple LLM calls)
    result2 = await injector.inject_sub_agent_context(context)
    
    assert result2.success is True
    assert result2.modified is True
    
    # Should still have exactly one sub-agent injection
    sub_agent_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "## Active Sub-Agents" in msg.content
    ]
    assert len(sub_agent_messages) == 1, "Should still have exactly one sub-agent injection after second call"


@pytest.mark.asyncio
async def test_hook_prevents_multiple_injections_across_calls(injector):
    """Test that multiple sequential hook calls don't accumulate sub-agent injections."""
    context = HookContext(
        hook_type="inject_sub_agent_context",
        request_id="test_req_002",
        session_id="test_session_multi",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant.")
        ]
    )
    
    # Simulate 5 consecutive LLM calls (like in a real multi-turn conversation)
    for i in range(5):
        context.messages.append(ChatMessage(role="user", content=f"Question {i}"))
        result = await injector.inject_sub_agent_context(context)
        assert result.success is True
        assert result.modified is True
        
        # Count all sub-agent injections after each call
        sub_agent_messages = [
            msg for msg in context.messages
            if msg.role == "system" and "## Active Sub-Agents" in msg.content
        ]
        
        assert len(sub_agent_messages) == 1, (
            f"After call {i+1}: Expected 1 sub-agent injection, found {len(sub_agent_messages)}"
        )


@pytest.mark.asyncio
async def test_hook_no_duplication_with_manual_injection(injector):
    """Test that hook removes manually injected old sub-agent lists."""
    # Manually inject old-style sub-agent list
    context = HookContext(
        hook_type="inject_sub_agent_context",
        request_id="test_req_003",
        session_id="test_session_manual",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="system", content="## Active Sub-Agents\n\nOld sub-agent list with outdated info"),
            ChatMessage(role="user", content="Status update?")
        ]
    )
    
    # Hook should remove old injection and add new one
    result = await injector.inject_sub_agent_context(context)
    assert result.success is True
    
    # Should have exactly one sub-agent injection
    sub_agent_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "## Active Sub-Agents" in msg.content
    ]
    assert len(sub_agent_messages) == 1, "Should replace old manual injection"
    assert "outdated info" not in sub_agent_messages[0].content, "Old content should be removed"
    assert "research_agent" in sub_agent_messages[0].content, "New content should be present"


@pytest.mark.asyncio
async def test_hook_insertion_position_after_system_prompt(injector):
    """Test that sub-agent injection is inserted after first system message."""
    context = HookContext(
        hook_type="inject_sub_agent_context",
        request_id="test_req_004",
        session_id="test_session_pos",
        messages=[
            ChatMessage(role="system", content="Main system prompt."),
            ChatMessage(role="user", content="Hello"),
            ChatMessage(role="assistant", content="Hi there!")
        ]
    )
    
    result = await injector.inject_sub_agent_context(context)
    assert result.success is True
    
    # Find sub-agent injection position
    sub_agent_index = None
    for i, msg in enumerate(context.messages):
        if msg.role == "system" and "## Active Sub-Agents" in msg.content:
            sub_agent_index = i
            break
    
    assert sub_agent_index is not None, "Sub-agent injection should be present"
    assert sub_agent_index == 1, f"Sub-agent injection should be at index 1 (after main system prompt), found at {sub_agent_index}"
    
    # Verify main system prompt is still first
    assert context.messages[0].content == "Main system prompt."


@pytest.mark.asyncio
async def test_hook_no_injection_when_no_sub_agents():
    """Test that hook doesn't inject when no sub-agents exist."""
    # Create manager that returns empty list
    manager = MagicMock(spec=SubAgentManager)
    manager.list_sub_sessions = AsyncMock(return_value=[])
    
    injector = SubAgentContextInjector(manager, {"enabled": True})
    
    context = HookContext(
        hook_type="inject_sub_agent_context",
        request_id="test_req_005",
        session_id="test_session_empty",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello")
        ]
    )
    
    result = await injector.inject_sub_agent_context(context)
    
    assert result.success is True
    assert result.modified is False, "Should not modify when no sub-agents"
    
    # Should have no sub-agent injections
    sub_agent_messages = [
        msg for msg in context.messages
        if msg.role == "system" and "## Active Sub-Agents" in msg.content
    ]
    assert len(sub_agent_messages) == 0, "Should have no injection when no sub-agents"


@pytest.mark.asyncio
async def test_hook_updates_when_sub_agents_change(mock_manager, injector):
    """Test that injection is updated when sub-agent list changes."""
    context = HookContext(
        hook_type="inject_sub_agent_context",
        request_id="test_req_006",
        session_id="test_session_change",
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="First question")
        ]
    )
    
    # First call - 2 sub-agents
    result1 = await injector.inject_sub_agent_context(context)
    assert result1.success is True
    
    # Verify 2 sub-agents are shown
    injection = [m for m in context.messages if "## Active Sub-Agents" in m.content][0]
    assert "research_agent" in injection.content
    assert "code_agent" in injection.content
    
    # Simulate sub-agent change - now only 1 active
    async def new_list_sub_sessions(parent_session_id, include_completed=False):
        return [
            {
                "instance_id": "sub_123",
                "agent_type": "research_agent",
                "status": "active",
                "message_count": 10,
                "task_summary": "Updated research task",
                "last_used": "2025-11-18T11:00:00Z",
                "tools_used": ["web_search"]
            }
        ]
    
    mock_manager.list_sub_sessions = AsyncMock(side_effect=new_list_sub_sessions)
    
    # Second call - should update
    context.messages.append(ChatMessage(role="user", content="Second question"))
    result2 = await injector.inject_sub_agent_context(context)
    assert result2.success is True
    
    # Should still have exactly one injection
    injections = [m for m in context.messages if "## Active Sub-Agents" in m.content]
    assert len(injections) == 1, "Should still have exactly one injection"
    
    # Verify updated content
    updated_injection = injections[0]
    assert "research_agent" in updated_injection.content
    assert "code_agent" not in updated_injection.content, "Removed sub-agent should not appear"
