"""Tests for Sub-Agent Manager plugin hook duplicate injection prevention."""
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock
from agent_system.hooks.plugin_hook import HookContext
from agent_system.llm.models import ChatMessage
from plugins.sub_agent_manager.hooks import SubAgentContextInjector
from plugins.sub_agent_manager.manager import SubAgentManager


# Whose sessions the panel is asked for: the tests store them under ada.
VIEWER = SimpleNamespace(username="ada")


async def stored_status(metadata):
    """What the injector is told a sub-agent is doing: here, the stored status as it stands."""
    return metadata.get("status", "unknown")


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
    return SubAgentContextInjector(mock_manager, "test_sam", config, status_of=stored_status)


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
        if "## Sub-Agents" in (msg.content or "")
    ]
    assert len(sub_agent_messages) == 1
    assert context.messages[-1] is sub_agent_messages[0], "appended, not pushed to the head"

    # Second call (simulate multiple LLM calls)
    result2 = await injector.inject_sub_agent_context(context)

    assert result2.success is True
    assert result2.modified is False, "nothing changed, so nothing may be written"

    sub_agent_messages = [
        msg for msg in context.messages
        if "## Sub-Agents" in (msg.content or "")
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
            if "## Sub-Agents" in (msg.content or "")
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
        if "Sub-Agents" in (msg.content or "")
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
    
    assert "## Sub-Agents" in (context.messages[-1].content or ""),         "the block is the last turn"
    assert [m.content for m in context.messages[:3]] == [
        "Main system prompt.", "Hello", "Hi there!"],         "everything that was there before must stay byte-identical"


@pytest.mark.asyncio
async def test_hook_no_injection_when_no_sub_agents():
    """Test that hook doesn't inject when no sub-agents exist."""
    # Create manager that returns empty list
    manager = MagicMock(spec=SubAgentManager)
    manager.list_sub_sessions = AsyncMock(return_value=[])
    
    injector = SubAgentContextInjector(manager, "test_sam", {"enabled": True}, status_of=stored_status)
    
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
        if "## Sub-Agents" in (msg.content or "")
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
    injection = [m for m in context.messages if "## Sub-Agents" in m.content][0]
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
    injections = [m for m in context.messages if "## Sub-Agents" in (m.content or "")]
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
    
    injector = SubAgentContextInjector(mock_manager, "w_sam", hook_config, allowed_agents, phase_filtering_config, status_of=stored_status)
    
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
    injected = [m for m in context.messages if "## Sub-Agents" in m.content]
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
    
    injector = SubAgentContextInjector(mock_manager, "w_sam", hook_config, allowed_agents, phase_filtering_config, status_of=stored_status)
    
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
    injected = [m for m in context.messages if "## Sub-Agents" in m.content]
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
    await manager.update_sub_agent_activity("parent", sub_id, "Thinking...")  # a run that never ended

    params = {"_session_id": "parent", "_session_service": service}
    [listed] = (await server._handle_list(params))["instances"]
    assert (listed["status"], listed["current_activity"]) == ("interrupted", None)
    stored = (await service.session_manager.load_session("ada", "parent", bypass_cache=True))["metadata"]["sub_agents"][sub_id]
    assert (stored["status"], stored["current_activity"]) == ("interrupted", None)


@pytest.mark.asyncio
async def test_a_run_that_ends_while_the_list_looks_keeps_its_ending(tmp_path):
    """The list is read before its loop. A run that ends in between -- ending written, slot let
    go -- still shows its activity on that copy and reads as a crash: healed on it, a clean
    ending was stored as "interrupted", "crashed". It keeps its ending and is shown by it."""
    from datetime import UTC, datetime

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
    await manager.update_sub_agent_activity("parent", sub_id, "Thinking...")  # the run, still going

    shown_on = server._shown_status

    async def the_run_ends_meanwhile(metadata, user_id):
        shown = await shown_on(metadata, user_id)
        if metadata.get("current_activity"):
            await manager.update_sub_session_metadata("parent", sub_id, status="active",
                                                      last_used=datetime.now(UTC).isoformat(),
                                                      current_activity=None, activity_updated_at=None)
        return shown

    server._shown_status = the_run_ends_meanwhile
    [listed] = (await server._handle_list({"_session_id": "parent", "_session_service": service}))["instances"]

    stored = (await service.session_manager.load_session("ada", "parent", bypass_cache=True))["metadata"]["sub_agents"][sub_id]
    assert (stored["status"], stored.get("error")) == ("active", None), stored
    assert listed["status"] == "idle", listed


async def _a_list_whose_heal_is_refused(tmp_path, meanwhile):
    """A sub-agent that reads as crashed on the list's copy, and ``meanwhile`` -- written after
    the list judged it -- refusing the heal. Returns the list's answer and the stored entry."""
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
    await manager.update_sub_agent_activity("parent", sub_id, "Thinking...")
    shown_on = server._shown_status

    async def written_meanwhile(metadata, user_id):
        shown = await shown_on(metadata, user_id)
        if metadata.get("current_activity"):
            await manager.update_sub_session_metadata("parent", sub_id, **meanwhile)
        return shown

    server._shown_status = written_meanwhile
    answer = await server._handle_list({"_session_id": "parent", "_session_service": service})
    stored = (await service.session_manager.load_session("ada", "parent", bypass_cache=True))["metadata"]["sub_agents"][sub_id]
    return answer, stored


@pytest.mark.asyncio
async def test_an_instance_archived_while_the_list_looks_is_not_listed(tmp_path):
    """Archived in the window (a delete, a create making room): the heal is refused, and the
    fresh copy says archived -- which a list without include_completed does not show."""
    answer, stored = await _a_list_whose_heal_is_refused(tmp_path, {"status": "archived"})
    assert stored["status"] == "archived"
    assert answer["instances"] == [], answer


@pytest.mark.asyncio
async def test_a_refused_heal_that_cannot_re_read_does_not_fail_the_list(tmp_path, monkeypatch):
    """The heal was quiet about a failure; the re-read after a refused one must be too."""
    from datetime import UTC, datetime

    from plugins.sub_agent_manager.manager import SubAgentManager

    reads = []
    list_sub_sessions = SubAgentManager.list_sub_sessions

    async def the_second_read_fails(self, *args, **kwargs):
        reads.append(1)
        if len(reads) > 1:
            raise OSError("the file is being replaced")
        return await list_sub_sessions(self, *args, **kwargs)

    monkeypatch.setattr(SubAgentManager, "list_sub_sessions", the_second_read_fails)
    answer, _ = await _a_list_whose_heal_is_refused(
        tmp_path, {"status": "active", "last_used": datetime.now(UTC).isoformat(),
                                "current_activity": None, "activity_updated_at": None})
    assert len(reads) == 2, "fixture: the heal was not refused"
    assert [i["status"] for i in answer["instances"]] == ["interrupted"], answer


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
        data = await factory.get_sub_agents(mock_request, session_id="test_session", current_user=VIEWER)
        phase = data["phase"]
        assert phase["variable"] == "workflow_phase"
        assert phase["current"] == "planning"
        assert phase["agents"] == ["story_designer"]
        assert phase["allowed_agents"] == ["story_designer", "character_designer", "scene_writer"]

        # a phase without agents of its own falls back to _default, and an empty _default to every allowed agent
        mock_session_manager.load_session.return_value = {"context_vars": {"workflow_phase": "review"}}
        phase = (await factory.get_sub_agents(mock_request, session_id="test_session", current_user=VIEWER))["phase"]
        assert phase["current"] == "review" and phase["agents"] == phase["allowed_agents"]

        # a non-empty _default is what such a phase gets
        server.phase_agents = {"planning": ["story_designer"], "_default": ["scene_writer"]}
        phase = (await factory.get_sub_agents(mock_request, session_id="test_session", current_user=VIEWER))["phase"]
        assert phase["current"] == "review" and phase["agents"] == ["scene_writer"]

        # without a phase set, _default does not apply: the tool allows every agent then
        mock_session_manager.load_session.return_value = {"context_vars": {}}
        phase = (await factory.get_sub_agents(mock_request, session_id="test_session", current_user=VIEWER))["phase"]
        assert phase["current"] is None and phase["agents"] == phase["allowed_agents"]
        assert server._get_phase_allowed_agents({}) is None

        server.phase_filtering_enabled = False
        assert (await factory.get_sub_agents(mock_request, session_id="test_session", current_user=VIEWER))["phase"] is None


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
    any instance created -- and the list of the same session its first level alike: the panel is one per instance,
    and a list of this one's own stood empty beside a full map on every session spawning through another.
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
        answer = await factory.get_agent_map(MagicMock(), session_id="s-1", current_user=VIEWER)
        listed = await factory.get_sub_agents(MagicMock(), session_id="s-1", current_user=VIEWER)

    root = answer["root"]
    assert (root["instance_id"], root["title"], root["agent_type"]) == ("s-1", "The coordinator", "coordinator")
    assert [child["instance_id"] for child in root["children"]] == [first, second]  # oldest first
    assert [child["parent_session_id"] for child in root["children"]] == ["s-1", "s-1"]
    [nested] = root["children"][0]["children"]
    assert (nested["instance_id"], nested["parent_session_id"]) == (below, first)
    assert (nested["agent_type"], nested["current_activity"]) == ("writer_agent", "Running tool: web_search")
    assert root["children"][1]["children"] == [] and answer["truncated"] is False
    assert sorted(instance["instance_id"] for instance in listed["instances"]) == sorted([first, second])


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
        answer = await factory.get_agent_map(MagicMock(), session_id="s-1", current_user=VIEWER)
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
            return await factory.get_agent_map(MagicMock(), session_id="s-1", current_user=VIEWER)

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
        answer = await factory.get_agent_map(MagicMock(), session_id="s-1", current_user=VIEWER)
        listed = await factory.get_sub_agents(MagicMock(), session_id="s-1", current_user=VIEWER)
    node = answer["root"]["children"][0]
    assert (node["instance_id"], node["children"]) == (parent, []) and answer["truncated"] is True
    assert parent in [entry["instance_id"] for entry in listed["instances"]], "the list dropped it"


@pytest.mark.asyncio
async def test_the_agent_map_of_a_session_that_is_not_stored_is_that_session_alone(tmp_path):
    """A session the panel is opened on before it is ever saved: the map is its one node, not an error."""
    from unittest.mock import patch

    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    service, server, _ = await _session_with_a_manager(tmp_path)
    with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service):
        answer = await SubAgentManagerWebFactory(server).get_agent_map(MagicMock(), session_id="s-unsaved", current_user=VIEWER)
    assert answer == {"root": {"instance_id": "s-unsaved", "title": None, "agent_type": None, "children": []},
                      "truncated": False}


@pytest.mark.asyncio
async def test_the_panel_shows_its_viewer_her_own_sessions_only(tmp_path):
    """Every lookup of the panel goes by its viewer, as /sessions does. It asked the session directories whose a
    session id is, and answered anyone who named one: another user's sub-agents, their transcripts, an archive."""
    from unittest.mock import patch

    from fastapi import HTTPException

    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    service, server, manager = await _session_with_a_manager(tmp_path)
    sub = await _spawn(manager, "s-1", "own")
    await service.session_manager.create_session(user_id="mallory", session_id="m-1", title="Mine",
                                                 agent_name="coordinator", llm_profile="normal")
    mallorys = await manager.create_sub_session(parent_session_id="m-1", agent_type="writer_agent",
                                               initial_message="x", params={"_user_id": "mallory"})
    server.phase_filtering_enabled = True  # the session's phase is hers too
    await service.session_manager.replace_session_context_vars("ada", "s-1", {"workflow_phase": "planning"})
    factory = SubAgentManagerWebFactory(server)
    stranger = SimpleNamespace(username="mallory")

    async def refused(call) -> int:
        with pytest.raises(HTTPException) as answer:
            await call
        return answer.value.status_code

    with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service):
        listed = await factory.get_sub_agents(MagicMock(), session_id="s-1", current_user=stranger)
        mapped = await factory.get_agent_map(MagicMock(), session_id="s-1", current_user=stranger)
        read = await refused(factory.get_sub_agent(MagicMock(), agent_id=sub, session_id="s-1", offset=None,
                                                   limit=None, current_user=stranger))
        archived = await refused(factory.archive_sub_agent(MagicMock(), agent_id=sub, session_id="s-1",
                                                           current_user=stranger))
        # an id no session can have is not found either, not a server error
        malformed = await refused(factory.archive_sub_agent(MagicMock(), agent_id="a b", session_id="s-1",
                                                            current_user=VIEWER))
        # her own session, another user's sub-agent: not found, not "belongs to another user"
        foreign_read = await refused(factory.get_sub_agent(MagicMock(), agent_id=mallorys, session_id="s-1",
                                                           offset=None, limit=None, current_user=VIEWER))
        foreign_archive = await refused(factory.archive_sub_agent(MagicMock(), agent_id=mallorys, session_id="s-1",
                                                                  current_user=VIEWER))
        own = await factory.get_sub_agents(MagicMock(), session_id="s-1", current_user=VIEWER)  # the counter-proof

    assert listed["instances"] == [] and listed["phase"]["current"] is None, listed
    assert mapped["root"]["children"] == [] and mapped["root"]["title"] is None, mapped
    assert read == archived == malformed == foreign_read == foreign_archive == 404
    parent = await service.session_manager.load_session("ada", "s-1", bypass_cache=True)
    assert parent["metadata"]["sub_agents"][sub]["status"] != "archived", "archived by a stranger"
    assert [entry["instance_id"] for entry in own["instances"]] == [sub]
    assert own["phase"]["current"] == "planning"


@pytest.mark.asyncio
async def test_an_entry_naming_another_users_sub_agent_is_shown_as_it_says(tmp_path, monkeypatch):
    """A session's metadata is its user's to write (PATCH /sessions). An entry naming another user's sub-agent had
    the panel look up that one's tokens and whether it runs, by the bare id. It is shown as its entry says, as one
    whose sub-session was deleted is: left out, it would have said the id exists elsewhere."""
    from unittest.mock import patch

    from plugins.sub_agent_manager import web_endpoints
    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    asked: list[str] = []

    async def context_of(ids):
        asked.extend(ids)
        return {}
    monkeypatch.setattr(web_endpoints, "context_of", context_of)

    service, server, manager = await _session_with_a_manager(tmp_path)
    theirs = await _spawn(manager, "s-1", "theirs")
    await service.session_manager.create_session(user_id="mallory", session_id="m-1", title="Mine",
                                                 agent_name="coordinator", llm_profile="normal")
    mine = await manager.create_sub_session(parent_session_id="m-1", agent_type="writer_agent",
                                            initial_message="x", params={"_user_id": "mallory"})
    forged = {"agent_type": "writer_agent", "status": "active", "created_at": "2026-09-25T00:00:00+00:00"}
    await service.session_manager.update_session_metadata("mallory", "m-1", {"sub_agents": {
        theirs: {"instance_id": theirs, **forged},
        "../s-1": {"instance_id": "../s-1", **forged}}})  # nor a path, whose file an answer would confirm
    server._running_agents.add(theirs)
    stranger = SimpleNamespace(username="mallory")
    factory = SubAgentManagerWebFactory(server)

    with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service):
        listed = await factory.get_sub_agents(MagicMock(), session_id="m-1", current_user=stranger)
        mapped = await factory.get_agent_map(MagicMock(), session_id="m-1", current_user=stranger)

    states = lambda entries: {entry["instance_id"]: entry["state"] for entry in entries}  # noqa: E731
    assert states(listed["instances"]) == states(mapped["root"]["children"]) == {
        mine: "idle", theirs: "idle", "../s-1": "idle"}, "running, as a lookup by the id says"
    assert set(asked) == {mine}, asked


@pytest.mark.asyncio
async def test_the_list_reads_the_viewers_copy_of_an_id_two_users_hold(tmp_path):
    """An id can sit in two users' directories: an archived one is free again, and a reinstated archive checks only
    its own directory. The list found the owner by scanning the directories and read whichever copy came first."""
    import json
    from unittest.mock import patch

    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    service, server, manager = await _session_with_a_manager(tmp_path)
    await service.session_manager.create_session(user_id="mallory", session_id="dup", title="Mine",
                                                 agent_name="coordinator", llm_profile="normal")
    mine = await manager.create_sub_session(parent_session_id="dup", agent_type="writer_agent",
                                            initial_message="x", params={"_user_id": "mallory"})
    other = json.loads((tmp_path / "mallory" / "dup.json").read_text(encoding="utf-8"))
    other["user_id"] = "aaa"  # a directory the scan meets before hers
    other["metadata"]["sub_agents"] = {"sub_of_aaa": {"instance_id": "sub_of_aaa", "agent_type": "writer_agent",
                                                      "status": "active", "created_at": "2026-09-25T00:00:00+00:00"}}
    (tmp_path / "aaa").mkdir()
    (tmp_path / "aaa" / "dup.json").write_text(json.dumps(other), encoding="utf-8")

    factory = SubAgentManagerWebFactory(server)
    mallory = SimpleNamespace(username="mallory")
    with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service):
        listed = await factory.get_sub_agents(MagicMock(), session_id="dup", current_user=mallory)
        # the transcript too: the lookup of whose it is read the other copy, and the cache kept it under the id
        service.session_manager._cache.clear()
        read = await factory.get_sub_agent(MagicMock(), agent_id=mine, session_id="dup", offset=None, limit=None,
                                           current_user=mallory)

    assert [entry["instance_id"] for entry in listed["instances"]] == [mine], listed
    assert read["instance_id"] == mine, read


@pytest.mark.asyncio
async def test_the_panel_hands_a_sub_agent_to_the_instance_that_spawned_it(tmp_path, monkeypatch):
    """Shown by another instance's panel, a sub-agent is still answered by its own: its runs and its background job
    live in the instance that spawned it. Asked through this one, its run read idle, and an archive left its job
    unmarked -- the ending kept the result for a poll nobody makes, and rang the caller for it."""
    from unittest.mock import patch

    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from agent_system.plugins.tool_adapter import PluginToolAdapter, plugin_tool_registry
    from plugins.sub_agent_manager.plugin import PLUGIN_FACTORY
    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    service, server, manager = await _session_with_a_manager(tmp_path)
    other = PLUGIN_FACTORY("sam_other", AgentSystemConfig(), ToolServerConfig(allowed_agents=["*"]))
    monkeypatch.setitem(plugin_tool_registry.plugin_servers, "sam_other", PluginToolAdapter("sam_other", other))
    own = await _spawn(manager, "s-1", "own")
    theirs = await _spawn(manager, "s-1", "theirs", creator="sam_other")
    other.server._running_agents.add(theirs)  # a run of the other instance, in this process
    other.server._async_jobs[theirs] = {"status": "running"}
    factory = SubAgentManagerWebFactory(server)

    with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service):
        listed = (await factory.get_sub_agents(MagicMock(), session_id="s-1", current_user=VIEWER))["instances"]
        mapped = (await factory.get_agent_map(MagicMock(), session_id="s-1", current_user=VIEWER))["root"]["children"]
        info = await factory.get_sub_agent(MagicMock(), agent_id=theirs, session_id="s-1", offset=None, limit=None, current_user=VIEWER)
        archived = await factory.archive_sub_agent(MagicMock(), agent_id=theirs, session_id="s-1", current_user=VIEWER)

    states = lambda entries: {entry["instance_id"]: entry["state"] for entry in entries}  # noqa: E731
    assert states(listed) == states(mapped) == {own: "idle", theirs: "running"}
    assert info["status"] == "running"
    assert archived["status"] == "archived"
    assert other.server._async_jobs[theirs].get("_ended_by_caller") is True  # marked in the instance that holds the job


@pytest.mark.asyncio
async def test_a_creator_that_is_no_sub_agent_manager_leaves_the_sub_agent_to_this_one(tmp_path, monkeypatch):
    """An entry naming a plugin that is registered but no sub-agent manager (a renamed instance, a hand-edited file)
    is answered by the panel's own instance: handed to that plugin, the list and the map failed with a 500."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from agent_system.plugins.tool_adapter import PluginToolAdapter, plugin_tool_registry
    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    service, server, manager = await _session_with_a_manager(tmp_path)
    monkeypatch.setitem(plugin_tool_registry.plugin_servers, "not_a_sam",
                        PluginToolAdapter("not_a_sam", SimpleNamespace(server=SimpleNamespace())))
    stray = await _spawn(manager, "s-1", "stray", creator="not_a_sam")
    factory = SubAgentManagerWebFactory(server)

    with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service):
        listed = (await factory.get_sub_agents(MagicMock(), session_id="s-1", current_user=VIEWER))["instances"]
        mapped = (await factory.get_agent_map(MagicMock(), session_id="s-1", current_user=VIEWER))["root"]["children"]

    assert [(entry["instance_id"], entry["state"]) for entry in listed] == [(stray, "idle")]
    assert [(entry["instance_id"], entry["state"]) for entry in mapped] == [(stray, "idle")]


@pytest.mark.asyncio
async def test_the_list_and_the_map_show_the_messages_and_the_tokens_of_each_sub_agent(tmp_path, monkeypatch):
    """The same two figures in both views: the messages from the parent's sub-index -- the transcript as last saved,
    where the parent's own entry said 2 -- and the prompt tokens of the last call as the provider counted them, from
    context_usage_tracker, with the window. A sub-agent that made no call has none; one compacted since says so."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from agent_system.plugins.tool_adapter import PluginToolAdapter, plugin_tool_registry
    from plugins.context_usage_tracker.tracker import UsageTracker
    from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

    service, server, manager = await _session_with_a_manager(tmp_path)
    tracker = UsageTracker(storage_path=tmp_path / "usage" / "usage.json")
    monkeypatch.setitem(plugin_tool_registry.plugin_servers, "context_usage_tracker",
                        PluginToolAdapter("context_usage_tracker", SimpleNamespace(tracker=tracker)))
    worked = await _spawn(manager, "s-1", "worked")
    below = await _spawn(manager, worked, "below")
    quiet = await _spawn(manager, "s-1", "quiet")
    await manager.update_sub_session_metadata(parent_session_id="s-1", sub_session_id=worked, message_count=2)
    for session_id, count in ((worked, 7), (below, 3)):  # each counted in the sub-index of the one above it
        transcript = await service.session_manager.load_session("ada", session_id)
        transcript["messages"] = [{"role": "user", "content": f"Question {i}"} for i in range(count)]
        await service.session_manager.save_session(transcript)
    tracker.record_usage(agent_id="w", agent_name="writer_agent", session_id=worked, total_tokens=13000,
                         prompt_tokens=12000, completion_tokens=1000, context_window=200000)
    tracker.record_usage(agent_id="b", agent_name="writer_agent", session_id=below, total_tokens=5000,
                         prompt_tokens=4000, completion_tokens=1000, context_window=100000)
    tracker.invalidate_session(below)
    factory = SubAgentManagerWebFactory(server)

    with patch("plugins.sub_agent_manager.web_endpoints.get_session_service", return_value=service):
        listed = {entry["instance_id"]: entry
                  for entry in (await factory.get_sub_agents(MagicMock(), session_id="s-1", current_user=VIEWER))["instances"]}
        mapped = {entry["instance_id"]: entry
                  for entry in (await factory.get_agent_map(MagicMock(), session_id="s-1", current_user=VIEWER))["root"]["children"]}
    [nested] = mapped[worked]["children"]

    figures = lambda entry: tuple(entry.get(key) for key in (  # noqa: E731
        "message_count", "context_tokens", "context_window", "context_stale"))
    assert figures(listed[worked]) == figures(mapped[worked]) == (7, 12000, 200000, False)
    assert figures(nested) == (3, 4000, 100000, True)
    assert figures(listed[quiet]) == figures(mapped[quiet])
    assert figures(listed[quiet])[1:] == (None, None, None)

