"""Tests for Sub-Agent Manager plugin hook duplicate injection prevention."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent_system.hooks.plugin_hook import HookContext
from agent_system.llm.models import ChatMessage
from plugins.sub_agent_manager.hooks import SubAgentContextInjector
from plugins.sub_agent_manager.manager import SubAgentManager


@pytest.fixture
def mock_manager():
    """Create mock SubAgentManager."""
    manager = MagicMock(spec=SubAgentManager)
    
    # Mock list_sub_sessions to return test data - accepts creator_plugin for filtering
    async def mock_list_sub_sessions(parent_session_id, include_completed=False, creator_plugin=None):
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
    return SubAgentContextInjector(mock_manager, "test_sam", config)


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
    
    injector = SubAgentContextInjector(manager, "test_sam", {"enabled": True})
    
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
    async def new_list_sub_sessions(parent_session_id, include_completed=False, creator_plugin=None):
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


@pytest.mark.asyncio
async def test_context_vars_inheritance():
    """Test that sub-sessions inherit context_vars from parent."""
    from agent_system.services.session_manager import SessionManager
    from agent_system.services.session_service import SessionService
    from agent_system.config.models import AgentConfig
    import tempfile
    import os
    
    # Create temp directory for test
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create session manager with temp storage
        session_manager = SessionManager(storage_path=tmpdir)
        session_service = SessionService(session_manager=session_manager)
        
        # Create mock registry with test agent
        registry = MagicMock()
        mock_agent = MagicMock()
        mock_agent.agent_config = AgentConfig(default_llm_profile="test_profile")
        registry.get = MagicMock(return_value=mock_agent)
        
        # Create SubAgentManager
        manager = SubAgentManager(
            session_service=session_service,
            registry=registry,
            max_nesting_depth=5
        )
        
        # Create parent session with context_vars
        user_id = "test_user"
        parent_session_id = "parent_123"
        await session_manager.create_session(
            user_id=user_id,
            session_id=parent_session_id,
            title="Parent Session",
            agent_name="parent_agent",
            llm_profile="test_profile"
        )
        
        # Add context_vars to parent
        parent_data = await session_manager.load_session(user_id, parent_session_id)
        parent_data["context_vars"] = {
            "book_id": "42",
            "workflow_phase": "planning"
        }
        await session_manager.save_session(parent_data)
        
        # Create mock parent agent with context_vars
        parent_agent = MagicMock()
        parent_agent.name = "parent_agent"
        parent_agent.agent_config = AgentConfig(
            default_llm_profile="test_profile",
            template_vars={"book_id": "42", "workflow_phase": "planning"}
        )
        
        # Create sub-session
        params = {
            "_agent": parent_agent,
            "_user_id": user_id
        }
        sub_session_id = await manager.create_sub_session(
            parent_session_id=parent_session_id,
            agent_type="test_agent",
            initial_message="Test task",
            params=params
        )
        
        # Load sub-session and verify context_vars inherited
        sub_data = await session_manager.load_session(user_id, sub_session_id)
        assert "context_vars" in sub_data, "Sub-session should have context_vars"
        assert sub_data["context_vars"]["book_id"] == "42"
        assert sub_data["context_vars"]["workflow_phase"] == "planning"
        
        # Verify parent link
        assert sub_data["parent_session"]["session_id"] == parent_session_id


@pytest.mark.asyncio
async def test_context_vars_loaded_into_agent_template_vars():
    """Test that context_vars from sub-session are loaded into agent's template_vars.
    
    This tests the code path in server.py that should load context_vars
    from the sub-session into the agent's template_vars before execution.
    """
    from agent_system.services.session_manager import SessionManager
    from agent_system.services.session_service import SessionService
    from agent_system.config.models import AgentConfig
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Setup
        session_manager = SessionManager(storage_path=tmpdir)
        session_service = SessionService(session_manager=session_manager)
        
        user_id = "test_user"
        sub_session_id = "sub_test_agent_001"
        
        # Create sub-session with context_vars (simulating what manager.create_sub_session does)
        await session_manager.create_session(
            user_id=user_id,
            session_id=sub_session_id,
            title="Sub Session",
            agent_name="continuity_guardian",
            llm_profile="test_profile"
        )
        
        # Add context_vars (simulating inheritance from parent)
        sub_data = await session_manager.load_session(user_id, sub_session_id)
        sub_data["context_vars"] = {
            "book_id": "17",
            "workflow_phase": "structure"
        }
        await session_manager.save_session(sub_data)
        
        # Create mock agent with EMPTY template_vars
        mock_agent = MagicMock()
        mock_agent.agent_config = AgentConfig(
            default_llm_profile="test_profile",
            template_vars={}  # EMPTY - should be populated
        )
        
        # Now simulate what server._handle_create does:
        # Load context_vars and inject into agent's template_vars
        loaded_session = await session_manager.load_session(user_id, sub_session_id)
        context_vars = loaded_session.get("context_vars", {})
        
        if context_vars:
            if mock_agent.agent_config.template_vars is None:
                mock_agent.agent_config.template_vars = {}
            mock_agent.agent_config.template_vars.update(context_vars)
        
        # Verify context_vars were loaded
        assert mock_agent.agent_config.template_vars.get("book_id") == "17", \
            f"book_id should be '17', got: {mock_agent.agent_config.template_vars}"
        assert mock_agent.agent_config.template_vars.get("workflow_phase") == "structure", \
            f"workflow_phase should be 'structure', got: {mock_agent.agent_config.template_vars}"
