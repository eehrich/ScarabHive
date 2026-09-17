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
    
    # Create temp directory for test
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create session manager with temp storage
        session_manager = SessionManager(storage_path=tmpdir)
        session_service = SessionService(session_manager=session_manager)
        
        # Create mock registry with test agent
        registry = MagicMock()
        mock_agent = MagicMock()
        mock_agent.agent_config = AgentConfig(llm_profile="test_profile")
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
            llm_profile="test_profile",
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
    """Test that context_vars from sub-session are loaded into agent's session_tracker template_vars.
    
    This tests the code path in server.py that should load context_vars
    from the sub-session into the agent's session-scoped template_vars before execution.
    Session-scoped vars are used instead of agent_config.template_vars for session isolation.
    """
    from agent_system.services.session_manager import SessionManager
    from agent_system.services.session_service import SessionService
    from agent_system.servers.agent.components.session_tracking import SessionTracker
    from agent_system.config.models import AgentConfig
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Setup
        session_manager = SessionManager(storage_path=tmpdir)
        _ = SessionService(session_manager=session_manager)  # For completeness
        
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
        
        # Create mock agent with a real session_tracker (session-isolated)
        mock_agent = MagicMock()
        mock_agent.agent_config = AgentConfig(
            llm_profile="test_profile",
            template_vars={}
        )
        mock_agent._session_tracker = SessionTracker()
        
        # Now simulate what server._handle_create does:
        # Load context_vars and inject into agent's session-scoped template_vars
        loaded_session = await session_manager.load_session(user_id, sub_session_id)
        context_vars = loaded_session.get("context_vars", {})
        
        if context_vars:
            # Session-scoped template vars (session-isolated, no global mutation)
            mock_agent._session_tracker.set_session_template_vars(sub_session_id, context_vars)
        
        # Verify context_vars were loaded into session_tracker (NOT agent_config)
        session_vars = mock_agent._session_tracker.get_session_template_vars(sub_session_id)
        assert session_vars.get("book_id") == "17", \
            f"book_id should be '17', got: {session_vars}"
        assert session_vars.get("workflow_phase") == "structure", \
            f"workflow_phase should be 'structure', got: {session_vars}"
        
        # Verify agent_config.template_vars is NOT modified (session isolation)
        assert mock_agent.agent_config.template_vars.get("book_id") is None, \
            "agent_config.template_vars should NOT be modified (race condition with singletons)"


@pytest.mark.asyncio
async def test_phase_filtering_shows_correct_agents():
    """Test that phase_filtering shows only agents allowed for the current phase."""
    from plugins.sub_agent_manager.hooks import SubAgentContextInjector
    
    # Create mock manager that returns various agent types
    mock_manager = MagicMock(spec=SubAgentManager)
    async def mock_list_sub_sessions(parent_session_id, include_completed=False, creator_plugin=None):
        return [
            {"instance_id": "sub_1", "agent_type": "story_designer", "status": "active"},
            {"instance_id": "sub_2", "agent_type": "character_designer", "status": "active"},
            {"instance_id": "sub_3", "agent_type": "structure_builder", "status": "active"},
        ]
    mock_manager.list_sub_sessions = AsyncMock(side_effect=mock_list_sub_sessions)
    
    # Hook config (without phase_filtering - that's now separate)
    hook_config = {
        "enabled": True,
        "max_sub_agents_shown": 50,
        "format": "markdown",
    }
    
    # Phase filtering config (now passed separately, mirrors server's top-level config)
    phase_filtering_config = {
        "enabled": True,
        "phase_variable": "workflow_phase",
        "phase_agents": {
            "planning": ["story_designer", "story_reviewer"],
            "characters": ["character_designer", "character_reviewer"],
            "structure": ["structure_builder", "continuity_guardian"],
        }
    }
    
    allowed_agents = ["story_designer", "story_reviewer", "character_designer", 
                      "character_reviewer", "structure_builder", "continuity_guardian"]
    
    injector = SubAgentContextInjector(mock_manager, "w_sam", hook_config, allowed_agents, phase_filtering_config)
    
    # Create mock agent with session_tracker that returns workflow_phase
    mock_agent = MagicMock()
    mock_session_tracker = MagicMock()
    mock_session_tracker.get_session_template_vars.return_value = {"workflow_phase": "planning"}
    mock_agent._session_tracker = mock_session_tracker
    
    context = HookContext(
        hook_type="inject_sub_agent_context",
        request_id="test_req",
        session_id="test_session",
        agent=mock_agent,
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Test")
        ]
    )
    
    result = await injector.inject_sub_agent_context(context)
    
    assert result.success is True
    assert result.modified is True
    
    # Find the injected message
    injected = [m for m in context.messages if "## Active Sub-Agents" in m.content]
    assert len(injected) == 1
    
    content = injected[0].content
    
    # Should show "Phase `planning` - Available agents: story_designer, story_reviewer"
    assert "Phase `planning`" in content
    assert "story_designer" in content
    assert "story_reviewer" in content
    
    # Should NOT show agents from other phases in the "Available agents" line
    assert "character_designer" not in content.split("Available agents:")[1].split("\n")[0]


@pytest.mark.asyncio
async def test_phase_filtering_disabled_shows_all_allowed():
    """Test that with phase_filtering disabled, all allowed_agents are shown."""
    from plugins.sub_agent_manager.hooks import SubAgentContextInjector
    
    mock_manager = MagicMock(spec=SubAgentManager)
    async def mock_list_sub_sessions(parent_session_id, include_completed=False, creator_plugin=None):
        return [{"instance_id": "sub_1", "agent_type": "story_designer", "status": "active"}]
    mock_manager.list_sub_sessions = AsyncMock(side_effect=mock_list_sub_sessions)
    
    # Hook config
    hook_config = {
        "enabled": True,
        "max_sub_agents_shown": 50,
        "format": "markdown",
    }
    
    # Phase filtering disabled (passed separately)
    phase_filtering_config = {
        "enabled": False
    }
    
    allowed_agents = ["story_designer", "story_reviewer", "character_designer"]
    
    injector = SubAgentContextInjector(mock_manager, "w_sam", hook_config, allowed_agents, phase_filtering_config)
    
    context = HookContext(
        hook_type="inject_sub_agent_context",
        request_id="test_req",
        session_id="test_session",
        messages=[
            ChatMessage(role="system", content="Test"),
            ChatMessage(role="user", content="Test")
        ]
    )
    
    result = await injector.inject_sub_agent_context(context)
    
    assert result.success is True
    injected = [m for m in context.messages if "## Active Sub-Agents" in m.content]
    assert len(injected) == 1
    
    content = injected[0].content
    
    # Should show all allowed_agents since phase_filtering is disabled
    assert "Available agents:" in content
    assert "story_designer" in content


@pytest.mark.asyncio
async def test_server_phase_filtering_blocks_wrong_phase_agent():
    """Test that server blocks agent creation if not allowed in current phase."""
    from plugins.sub_agent_manager.server import SubAgentManagerServer
    from agent_system.config import ToolServerConfig, AgentSystemConfig
    
    # Create mock configs
    server_config = MagicMock(spec=ToolServerConfig)
    server_config.max_sub_agents_per_session = 10
    server_config.max_nesting_depth = 3
    server_config.max_message_history = 100
    server_config.max_sub_agents_per_type = 3
    server_config.default_wait_timeout = 3600
    server_config.allowed_agents = ["story_designer", "character_designer", "scene_writer"]
    server_config.blocked_agents = []
    server_config.hook_config = {}
    # Phase filtering config at top-level
    server_config.phase_filtering = {
        "enabled": True,
        "phase_variable": "workflow_phase",
        "phase_agents": {
            "planning": ["story_designer"],
            "content": ["scene_writer"],
        }
    }
    
    system_config = MagicMock(spec=AgentSystemConfig)
    
    server = SubAgentManagerServer("test_sam", system_config, server_config)
    
    # Verify phase filtering config was loaded
    assert server.phase_filtering_enabled is True
    assert server.phase_variable == "workflow_phase"
    assert "planning" in server.phase_agents
    
    # Test _get_phase_allowed_agents with mock params
    mock_agent = MagicMock()
    mock_session_tracker = MagicMock()
    mock_session_tracker.get_session_template_vars.return_value = {"workflow_phase": "planning"}
    mock_agent._session_tracker = mock_session_tracker
    
    params = {
        "_agent": mock_agent,
        "_session_id": "test_session"
    }
    
    # Should return planning agents
    phase_allowed = server._get_phase_allowed_agents(params)
    assert phase_allowed == ["story_designer"]
    
    # story_designer should be allowed
    assert server._is_agent_allowed_for_phase("story_designer", phase_allowed) is True
    
    # scene_writer should NOT be allowed (it's in content phase, not planning)
    assert server._is_agent_allowed_for_phase("scene_writer", phase_allowed) is False
    
    # character_designer is in allowed_agents but not in any phase - blocked
    assert server._is_agent_allowed_for_phase("character_designer", phase_allowed) is False


@pytest.mark.asyncio
async def test_the_tool_list_marks_a_sub_agent_without_a_run_interrupted_and_clears_its_activity(tmp_path):
    """A sub-agent still reporting an activity with no run in this process: the tool's list answers it interrupted
    without the stale activity, and stores it so -- a manager made for the call knows no earlier activity to compare."""
    from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
    from agent_system.tools.base import ToolServerRegistry
    from agent_system.services.session_manager import SessionManager
    from agent_system.services.session_service import SessionService
    from plugins.sub_agent_manager.server import SubAgentManagerServer

    service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path)))
    server = SubAgentManagerServer("sam", AgentSystemConfig(), ToolServerConfig(allowed_agents=["*"]))
    registry = ToolServerRegistry()
    agent = MagicMock()
    agent.name = "writer_agent"
    agent.agent_config = AgentConfig(llm_profile="normal")
    registry.register("writer_agent", agent)
    await service.session_manager.create_session(user_id="ada", session_id="parent", agent_name="coordinator", llm_profile="normal")
    manager = server._get_manager(service, registry)
    sub_id = await manager.create_sub_session(parent_session_id="parent", agent_type="writer_agent", initial_message="task",
                                              params={"_creator_plugin": "sam"})
    await manager.update_sub_session_metadata(parent_session_id="parent", sub_session_id=sub_id, current_activity="Thinking...")

    params = {"_session_id": "parent", "_session_service": service}
    [listed] = (await server._handle_list(params))["instances"]
    assert (listed["status"], listed["current_activity"]) == ("interrupted", None)
    stored = (await service.session_manager.load_session("ada", "parent", bypass_cache=True))["metadata"]["sub_agents"][sub_id]
    assert (stored["status"], stored["current_activity"]) == ("interrupted", None)


@pytest.mark.asyncio
async def test_the_sub_agent_list_carries_the_phase_of_the_session():
    """The panel's list names the session's phase and the agents it lets the tool spawn, by the tool's own rule."""
    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory
    from plugins.sub_agent_manager.server import SubAgentManagerServer
    from agent_system.config import ToolServerConfig, AgentSystemConfig
    from fastapi import Request
    from unittest.mock import AsyncMock, patch
    
    # Create mock configs with phase filtering
    server_config = MagicMock(spec=ToolServerConfig)
    server_config.max_sub_agents_per_session = 10
    server_config.max_nesting_depth = 3
    server_config.max_message_history = 100
    server_config.max_sub_agents_per_type = 3
    server_config.default_wait_timeout = 3600
    server_config.allowed_agents = ["story_designer", "character_designer", "scene_writer"]
    server_config.blocked_agents = []
    server_config.hook_config = {}
    server_config.phase_filtering = {
        "enabled": True,
        "phase_variable": "workflow_phase",
        "phase_agents": {
            "planning": ["story_designer"],
            "content": ["scene_writer"],
            "_default": []
        }
    }
    
    system_config = MagicMock(spec=AgentSystemConfig)
    
    server = SubAgentManagerServer("test_sam", system_config, server_config)
    factory = SubAgentManagerWebFactory(server)
    
    # Mock session manager (used by web endpoint)
    mock_session_manager = MagicMock()
    mock_session_manager._find_session_owner_async = AsyncMock(return_value="test_user")
    mock_session_manager.load_session = AsyncMock(return_value={
        "context_vars": {"workflow_phase": "planning", "book_id": "17"}
    })
    
    # Mock session service with session_manager
    mock_session_service = MagicMock()
    mock_session_service.session_manager = mock_session_manager
    
    mock_request = MagicMock(spec=Request)

    with patch('plugins.sub_agent_manager.web_endpoints.get_session_service', return_value=mock_session_service):
        data = await factory.get_sub_agents(mock_request, session_id="test_session")
        phase = data["phase"]
        assert phase["variable"] == "workflow_phase"
        assert phase["current"] == "planning"
        assert phase["agents"] == ["story_designer"]
        assert phase["allowed_agents"] == ["story_designer", "character_designer", "scene_writer"]

        # a phase without agents of its own falls back to _default, and an empty _default to every allowed agent
        mock_session_manager.load_session.return_value = {"context_vars": {"workflow_phase": "review"}}
        phase = (await factory.get_sub_agents(mock_request, session_id="test_session"))["phase"]
        assert phase["current"] == "review" and phase["agents"] == phase["allowed_agents"]

        # a non-empty _default is what such a phase gets
        server.phase_agents = {"planning": ["story_designer"], "_default": ["scene_writer"]}
        phase = (await factory.get_sub_agents(mock_request, session_id="test_session"))["phase"]
        assert phase["current"] == "review" and phase["agents"] == ["scene_writer"]

        # without a phase set, _default does not apply: the tool allows every agent then
        mock_session_manager.load_session.return_value = {"context_vars": {}}
        phase = (await factory.get_sub_agents(mock_request, session_id="test_session"))["phase"]
        assert phase["current"] is None and phase["agents"] == phase["allowed_agents"]
        assert server._get_phase_allowed_agents({}) is None

        server.phase_filtering_enabled = False
        assert (await factory.get_sub_agents(mock_request, session_id="test_session"))["phase"] is None
