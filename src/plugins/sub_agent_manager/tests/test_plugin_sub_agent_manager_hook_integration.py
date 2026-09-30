"""Integration tests for hook in SubAgentManagerServer."""
import pytest

from agent_system.llm.message_roles import DEVELOPER
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC

from plugins.sub_agent_manager.server import SubAgentManagerServer
from agent_system.hooks.plugin_hook import HookContext, HookType
from agent_system.llm.models import ChatMessage
from agent_system.config.models import ToolServerConfig


@pytest.fixture
def system_config():
    """Mock system configuration."""
    return MagicMock()


@pytest.fixture
def server_config():
    """Mock tool server configuration with hook settings."""
    return ToolServerConfig(
        enabled=True,
        storage_type="json",
        session_storage_dir="data/sessions",
        sub_agent_configs_dir="data/sub_agents",
        hook_config={
            "inject_sub_agent_context": {
                "enabled": True,
                "max_sub_agents_shown": 5,
                "show_completed": False,
                "format": "markdown"
            }
        }
    )


@pytest.mark.asyncio
async def test_server_implements_hook_interface(system_config, server_config):
    """Test that server correctly implements PluginHook interface."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, server_config)

    # Verify hook method exists
    assert hasattr(server, "on_pre_llm_call")
    assert callable(server.on_pre_llm_call)


@pytest.mark.asyncio
async def test_on_pre_llm_call_lazy_loading(system_config, server_config):
    """Test that hook injector is lazy-loaded on first call."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, server_config)

    # Mock manager
    with patch.object(server, '_get_manager') as mock_get_manager:
        mock_manager = MagicMock()
        mock_manager.list_sub_sessions = AsyncMock(return_value=[])
        mock_get_manager.return_value = mock_manager

        # Create context
        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="test_req",
            session_id="test_session",
            agent=MagicMock(),
            agent_name="test",
            messages=[ChatMessage(role="user", content="Test")],
            step=1
        )

        # Call hook
        result = await server.on_pre_llm_call(context)

        # Verify result
        assert result.success is True


@pytest.mark.asyncio
async def test_on_pre_llm_call_injects_context(system_config, server_config):
    """Test that hook properly injects sub-agent context."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, server_config)

    # Mock sub-agent data
    sub_agents = [
        {
            "instance_id": "test_sub_001",
            "agent_type": "web_research_agent",
            "status": "active",
            "message_count": 10,
            "task_summary": "Research topic",
            "last_used": datetime.now(UTC).isoformat(),
            "tools_used": ["web_search"]
        }
    ]

    with patch.object(server, '_get_manager') as mock_get_manager:
        mock_manager = MagicMock()
        mock_manager.list_sub_sessions = AsyncMock(return_value=sub_agents)
        mock_get_manager.return_value = mock_manager

        # Create context with messages
        messages = [
            ChatMessage(role="system", content="You are a coordinator"),
            ChatMessage(role="user", content="Continue research")
        ]

        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="test_req",
            session_id="test_session",
            agent=MagicMock(),
            agent_name="coordinator",
            messages=messages,
            step=1
        )

        # Call hook
        result = await server.on_pre_llm_call(context)

        # Verify injection
        assert result.success is True
        assert result.modified is True
        assert len(context.messages) == 3  # system + injected + user

        # Verify injected content
        injected = context.messages[-1]
        assert injected.role == DEVELOPER
        assert "## Sub-Agents" in injected.content
        assert "| web_research_agent | `test_sub_001` | idle | Research topic |" in injected.content


def _worker(instance_id, status="active", activity=None, created="2026-09-21T06:00:00Z"):
    return {"instance_id": instance_id, "agent_type": "worker", "status": status,
            "created_at": created, "task_summary": "count the words", "current_activity": activity}


def _coordinator_turn():
    return HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="req", session_id="parent1",
                       agent=MagicMock(), agent_name="coordinator",
                       messages=[ChatMessage(role="user", content="go")], step=1)


@pytest.mark.asyncio
async def test_the_list_says_running_while_a_run_is_under_way_and_idle_once_it_is_over(
        system_config, server_config):
    """Stored, both read "active" -- a clean ending stores it too -- so the list read the same
    before and after a worker finished, and a coordinator could not tell from it that the
    worker was done. What tells them apart is the run."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, server_config)
    manager = MagicMock()
    manager.list_sub_sessions = AsyncMock(return_value=[
        _worker("sub_worker"),
        _worker("sub_failed", status="failed", created="2026-09-21T05:00:00Z"),
        _worker("sub_archived", status="archived", created="2026-09-21T04:00:00Z")])
    context = _coordinator_turn()

    with patch.object(server, "_get_manager", return_value=manager):
        server._running_agents.add("sub_worker")
        await server.on_pre_llm_call(context)
        running = context.messages[-1].content
        server._running_agents.discard("sub_worker")
        await server.on_pre_llm_call(context)
        idle = context.messages[-1].content

    assert "| worker | `sub_worker` | running | count the words |" in running
    assert "| worker | `sub_worker` | idle | count the words |" in idle
    assert "| worker | `sub_failed` | failed |" in idle, "a run that failed is listed, not dropped"
    assert "sub_archived" not in idle, "archived ones only with show_completed"
    assert len(context.messages) == 3, "one block while it ran, one once it was over"


@pytest.mark.asyncio
async def test_a_run_in_another_process_is_asked_about_under_the_sessions_user(
        system_config, server_config, monkeypatch):
    """A woken coordinator runs in a process of its own, and its other workers in the one that
    started them: there only the lock beside the worker's session answers, and it lives in the
    directory of the user the session belongs to."""
    from plugins.sub_agent_manager import server as sam_server
    presence = MagicMock()
    presence.status = MagicMock(return_value="running")
    monkeypatch.setattr(sam_server, "presence_for", lambda config: presence)
    server = SubAgentManagerServer("sub_agent_manager", system_config, server_config)
    manager = MagicMock()
    manager._extract_user_id = MagicMock(return_value="ada")
    manager.list_sub_sessions = AsyncMock(return_value=[_worker("sub_elsewhere", activity="🔧 Running tool: read")])
    context = _coordinator_turn()

    with patch.object(server, "_get_manager", return_value=manager):
        await server.on_pre_llm_call(context)

    assert "| worker | `sub_elsewhere` | running |" in context.messages[-1].content
    assert presence.status.call_args.args == ("sub_elsewhere", "ada")
    assert manager._extract_user_id.call_args.args == ("parent1",)


@pytest.mark.asyncio
async def test_on_pre_llm_call_error_handling(system_config, server_config):
    """Test that hook handles errors gracefully."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, server_config)

    with patch.object(server, '_get_manager') as mock_get_manager:
        # Mock manager to raise exception
        mock_manager = MagicMock()
        mock_manager.list_sub_sessions = AsyncMock(side_effect=Exception("Test error"))
        mock_get_manager.return_value = mock_manager

        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="test_req",
            session_id="test_session",
            agent=MagicMock(),
            agent_name="test",
            messages=[ChatMessage(role="user", content="Test")],
            step=1
        )

        # Call hook
        result = await server.on_pre_llm_call(context)

        # Should not fail, just skip
        assert result.success is True
        assert result.modified is False


@pytest.mark.asyncio
async def test_on_pre_llm_call_disabled_hook(system_config):
    """Test behavior when hook is disabled in config."""
    # Config with hook disabled
    server_config = ToolServerConfig(
        enabled=True,
        storage_type="json",
        hook_config={
            "inject_sub_agent_context": {
                "enabled": False  # Disabled!
            }
        }
    )

    server = SubAgentManagerServer("sub_agent_manager", system_config, server_config)

    with patch.object(server, '_get_manager') as mock_get_manager:
        mock_manager = MagicMock()
        mock_manager.list_sub_sessions = AsyncMock(return_value=[{"instance_id": "test"}])
        mock_get_manager.return_value = mock_manager

        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="test_req",
            session_id="test_session",
            agent=MagicMock(),
            agent_name="test",
            messages=[ChatMessage(role="user", content="Test")],
            step=1
        )

        # Call hook
        result = await server.on_pre_llm_call(context)

        # Should skip when disabled
        assert result.success is True
        assert result.modified is False


@pytest.mark.asyncio
async def test_on_pre_llm_call_reuses_injector(system_config, server_config):
    """Test that hook can be called multiple times."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, server_config)

    with patch.object(server, '_get_manager') as mock_get_manager:
        mock_manager = MagicMock()
        mock_manager.list_sub_sessions = AsyncMock(return_value=[])
        mock_get_manager.return_value = mock_manager

        context = HookContext(
            hook_type=HookType.PRE_LLM_CALL,
            request_id="test_req",
            session_id="test_session",
            agent=MagicMock(),
            agent_name="test",
            messages=[ChatMessage(role="user", content="Test")],
            step=1
        )

        # First call
        result1 = await server.on_pre_llm_call(context)

        # Second call
        result2 = await server.on_pre_llm_call(context)

        # Both should succeed
        assert result1.success is True
        assert result2.success is True


@pytest.mark.asyncio
async def test_the_agents_override_wins_over_the_server_entry(system_config, server_config):
    """``hooks.overrides.<instance>.inject_sub_agent_context`` settings reach the hook as
    ``context.hook_config`` and win over the server entry's block (5 shown there)."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, server_config)
    sub_agents = [{"instance_id": f"sub_worker_{i:04d}", "agent_type": "worker", "status": "active",
                   "created_at": f"2026-09-30T10:0{i}:00+00:00", "task_summary": f"Task {i}"} for i in range(3)]
    with patch.object(server, '_get_manager') as mock_get_manager:
        mock_get_manager.return_value = MagicMock(list_sub_sessions=AsyncMock(return_value=sub_agents))
        context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="s",
                              agent=MagicMock(), agent_name="coordinator",
                              messages=[ChatMessage(role="user", content="Go")], step=1,
                              hook_config={"max_sub_agents_shown": 1})
        await server.on_pre_llm_call(context)
    block = context.messages[-1].content
    assert "sub_worker_0002" in block
    assert "sub_worker_0001" not in block and "sub_worker_0000" not in block
