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
async def test_hook_writes_an_unchanged_list_only_once(injector):
    """The second call writes nothing -- that is what keeps the prefix cached."""
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
    
    sub_agent_messages = [
        msg for msg in context.messages
        if "## Active Sub-Agents" in (msg.content or "")
    ]
    assert len(sub_agent_messages) == 1
    assert context.messages[-1] is sub_agent_messages[0], "appended, not pushed to the head"

    # Second call (simulate multiple LLM calls)
    result2 = await injector.inject_sub_agent_context(context)

    assert result2.success is True
    assert result2.modified is False, "nothing changed, so nothing may be written"

    sub_agent_messages = [
        msg for msg in context.messages
        if "## Active Sub-Agents" in (msg.content or "")
    ]
    assert len(sub_agent_messages) == 1


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
        assert result.modified is (i == 0), "an unchanged list is written once"

        sub_agent_messages = [
            msg for msg in context.messages
            if "## Active Sub-Agents" in (msg.content or "")
        ]

        assert len(sub_agent_messages) == 1, (
            f"After call {i+1}: Expected 1 sub-agent block, found {len(sub_agent_messages)}"
        )


@pytest.mark.asyncio
async def test_hook_supersedes_an_older_unmarked_list(injector):
    """The old block keeps its place; the current list follows it."""
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
    
    result = await injector.inject_sub_agent_context(context)
    assert result.success is True

    sub_agent_messages = [
        msg for msg in context.messages
        if "## Active Sub-Agents" in (msg.content or "")
    ]
    assert len(sub_agent_messages) == 2, "the stale block stays, the new one is appended"
    assert "outdated info" in sub_agent_messages[0].content, "history is not rewritten"
    assert "research_agent" in sub_agent_messages[-1].content, "the last word is current"
    assert context.messages[-1] is sub_agent_messages[-1]


@pytest.mark.asyncio
async def test_hook_appends_instead_of_inserting_at_the_head(injector):
    """Position is the whole point: at the head it invalidates the cache."""
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
    
    assert "## Active Sub-Agents" in (context.messages[-1].content or ""),         "the block is the last turn"
    assert [m.content for m in context.messages[:3]] == [
        "Main system prompt.", "Hello", "Hi there!"],         "everything that was there before must stay byte-identical"


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
    injections = [m for m in context.messages if "## Active Sub-Agents" in (m.content or "")]
    assert len(injections) == 2, "the new state is appended behind the old one"

    updated_injection = injections[-1]
    assert "research_agent" in updated_injection.content
    assert "code_agent" not in updated_injection.content, "Removed sub-agent should not appear"
    assert "code_agent" in injections[0].content, "what was true then stays as it was"


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


async def _session_with_a_manager(tmp_path):
    """Session ``s-1`` of user ``ada``, and a manager instance ``sam`` that may spawn ``writer_agent``."""
    from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
    from agent_system.services.session_manager import SessionManager
    from agent_system.services.session_service import SessionService
    from agent_system.tools.base import ToolServerRegistry
    from plugins.sub_agent_manager.server import SubAgentManagerServer

    service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path)))
    await service.session_manager.create_session(user_id="ada", session_id="s-1", title="The coordinator",
                                                 agent_name="coordinator", llm_profile="normal")
    registry = ToolServerRegistry()
    agent = MagicMock()
    agent.name = "writer_agent"
    agent.agent_config = AgentConfig(llm_profile="normal")
    registry.register("writer_agent", agent)
    server = SubAgentManagerServer("sam", AgentSystemConfig(), ToolServerConfig(allowed_agents=["*"]))
    return service, server, server._get_manager(service, registry)


def _spawn(manager, parent: str, label: str, creator: str = "sam"):
    return manager.create_sub_session(parent_session_id=parent, agent_type="writer_agent",
                                      initial_message=f"Task of {label}", instance_label=label,
                                      params={"_creator_plugin": creator, "_user_id": "ada"})


@pytest.mark.asyncio
async def test_the_agent_map_nests_a_sub_agents_own_sub_agents_under_it(tmp_path):
    """The map walks the whole session: a sub-agent's sub-agents hang under it, oldest first, each naming the
    session it hangs under -- which is what the panel passes to read its transcript.

    Whoever spawned them: below the first level the manager instance is the sub-agent's own, so the map shows what
    any instance created, where the list of the same session shows only this one's.
    """
    from unittest.mock import patch

    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    service, server, manager = await _session_with_a_manager(tmp_path)
    factory = SubAgentManagerWebFactory(server)
    first = await _spawn(manager, "s-1", "first")
    below = await _spawn(manager, first, "below", creator="sam_other")
    second = await _spawn(manager, "s-1", "second", creator="sam_other")
    await manager.update_sub_session_metadata(parent_session_id=first, sub_session_id=below,
                                              current_activity="Running tool: web_search")

    with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service):
        answer = await factory.get_agent_map(MagicMock(), session_id="s-1")
        listed = await factory.get_sub_agents(MagicMock(), session_id="s-1")

    root = answer["root"]
    assert (root["instance_id"], root["title"], root["agent_type"]) == ("s-1", "The coordinator", "coordinator")
    assert [child["instance_id"] for child in root["children"]] == [first, second]  # oldest first
    assert [child["parent_session_id"] for child in root["children"]] == ["s-1", "s-1"]
    [nested] = root["children"][0]["children"]
    assert (nested["instance_id"], nested["parent_session_id"]) == (below, first)
    assert (nested["agent_type"], nested["current_activity"]) == ("writer_agent", "Running tool: web_search")
    assert root["children"][1]["children"] == [] and answer["truncated"] is False
    assert [instance["instance_id"] for instance in listed["instances"]] == [first]


@pytest.mark.asyncio
async def test_the_agent_map_reads_only_the_sessions_that_have_sub_agents(tmp_path):
    """A leaf is not opened: a stat on its sub-index says it has none, and the session file is the expensive read
    in a walk the panel repeats every ten seconds."""
    from unittest.mock import patch

    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    service, server, manager = await _session_with_a_manager(tmp_path)
    factory = SubAgentManagerWebFactory(server)
    parent = await _spawn(manager, "s-1", "parent")
    for label in ("leaf_a", "leaf_b"):
        await _spawn(manager, parent, label)

    sessions = service.session_manager
    opened: list[str] = []
    read = sessions.load_session

    async def counted(user_id, session_id, **kwargs):
        opened.append(session_id)
        return await read(user_id, session_id, **kwargs)

    sessions.load_session = counted
    with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service):
        answer = await factory.get_agent_map(MagicMock(), session_id="s-1")
    assert len(answer["root"]["children"][0]["children"]) == 2
    assert opened == ["s-1", parent]  # the two leaves are stated, not read, and the root is read once


@pytest.mark.asyncio
async def test_the_agent_map_says_when_something_below_is_not_shown(tmp_path):
    """Every bound the walk has leaves a node that looks exactly like a leaf: the sub-agents it may answer with,
    the sessions it may read, the depth it may go. Whichever one cuts, the answer says ``truncated`` -- and a tree
    that fits exactly does not, or the panel would put a banner over a complete map."""
    from unittest.mock import patch

    from plugins.sub_agent_manager import web_endpoints
    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    service, server, manager = await _session_with_a_manager(tmp_path)
    factory = SubAgentManagerWebFactory(server)
    first = await _spawn(manager, "s-1", "one")
    second = await _spawn(manager, "s-1", "two")
    below = await _spawn(manager, first, "below")

    async def mapped(**bounds):
        with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service), \
                patch.multiple(web_endpoints, **bounds):
            return await factory.get_agent_map(MagicMock(), session_id="s-1")

    whole = await mapped(MAP_NODES=3)  # the tree is three nodes and fits exactly
    assert whole["truncated"] is False
    assert [child["instance_id"] for child in whole["root"]["children"]] == [first, second]
    assert [child["instance_id"] for child in whole["root"]["children"][0]["children"]] == [below]

    nodes = await mapped(MAP_NODES=2)  # the last sibling does not fit
    assert nodes["truncated"] is True and [child["instance_id"] for child in nodes["root"]["children"]] == [first]

    reads = await mapped(MAP_READS=1)  # the root is read, nothing below it is
    assert reads["truncated"] is True and reads["root"]["children"][0]["children"] == []

    deep = await mapped(MAP_DEPTH=1)  # one level, and what hangs below it is not a leaf
    assert deep["truncated"] is True and deep["root"]["children"][0]["children"] == []


@pytest.mark.asyncio
async def test_the_agent_map_says_so_when_a_node_it_has_to_read_is_gone(tmp_path):
    """A sub-agent deleted from the store while its own sub-agents stay behind: the stat still says there is
    something under it and its session file no longer answers for it. It is drawn as the leaf it looks like --
    but not passed off as one."""
    from unittest.mock import patch

    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    service, server, manager = await _session_with_a_manager(tmp_path)
    factory = SubAgentManagerWebFactory(server)
    parent = await _spawn(manager, "s-1", "parent")
    await _spawn(manager, parent, "below")
    await _spawn(manager, "s-1", "other")  # deleting the only child of a node unlinks its sub-index: keep one
    await service.session_manager.delete_session("ada", parent, create_backup=False)

    with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service):
        answer = await factory.get_agent_map(MagicMock(), session_id="s-1")
    node = answer["root"]["children"][0]
    assert (node["instance_id"], node["children"]) == (parent, []) and answer["truncated"] is True


@pytest.mark.asyncio
async def test_the_agent_map_of_a_session_that_is_not_stored_is_that_session_alone(tmp_path):
    """A session the panel is opened on before it is ever saved: the map is its one node, not an error."""
    from unittest.mock import patch

    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    service, server, _ = await _session_with_a_manager(tmp_path)
    with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service):
        answer = await SubAgentManagerWebFactory(server).get_agent_map(MagicMock(), session_id="s-unsaved")
    assert answer == {"root": {"instance_id": "s-unsaved", "title": None, "agent_type": None, "children": []},
                      "truncated": False}
