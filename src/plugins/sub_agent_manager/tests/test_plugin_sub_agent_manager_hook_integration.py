"""Integration tests for hook in SubAgentManagerServer."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC

from plugins.sub_agent_manager.server import SubAgentManagerServer
from agent_system.hooks.plugin_hook import HookContext, HookType
from agent_system.llm.models import ChatMessage
from agent_system.config.models import MCPConfig


@pytest.fixture
def system_config():
    """Mock system configuration."""
    return MagicMock()


@pytest.fixture
def mcp_config():
    """Mock MCP configuration with hook settings."""
    return MCPConfig(
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
async def test_server_implements_hook_interface(system_config, mcp_config):
    """Test that server correctly implements PluginHook interface."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, mcp_config)

    # Verify hook method exists
    assert hasattr(server, "on_pre_llm_call")
    assert callable(server.on_pre_llm_call)


@pytest.mark.asyncio
async def test_on_pre_llm_call_lazy_loading(system_config, mcp_config):
    """Test that hook injector is lazy-loaded on first call."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, mcp_config)

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
async def test_on_pre_llm_call_injects_context(system_config, mcp_config):
    """Test that hook properly injects sub-agent context."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, mcp_config)

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
        injected = context.messages[1]
        assert injected.role == "system"
        assert "Active Sub-Agents" in injected.content
        assert "web_research_agent" in injected.content


@pytest.mark.asyncio
async def test_on_pre_llm_call_error_handling(system_config, mcp_config):
    """Test that hook handles errors gracefully."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, mcp_config)

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
    mcp_config = MCPConfig(
        enabled=True,
        storage_type="json",
        hook_config={
            "inject_sub_agent_context": {
                "enabled": False  # Disabled!
            }
        }
    )

    server = SubAgentManagerServer("sub_agent_manager", system_config, mcp_config)

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
async def test_on_pre_llm_call_reuses_injector(system_config, mcp_config):
    """Test that hook can be called multiple times."""
    server = SubAgentManagerServer("sub_agent_manager", system_config, mcp_config)

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
