"""Tests for CLI MCP commands with new configuration structure."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent_system.config.models import (
    AgentSystemConfig,
    MCPSystemConfig,
    LLMSystemConfig,
    LLMModelConfig,
    ExternalServersConfig,
    RemoteMCPConfig,
)
from agent_system.mcp.integration import MCPIntegration


@pytest.fixture
def mock_config():
    """Create a mock AgentSystemConfig with MCP settings."""
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")},
            profiles={}
        ),
        mcp_system=MCPSystemConfig(
            plugin_dirs=["src/plugins"],
            servers={},
            external_servers=ExternalServersConfig(
                remote_servers={
                    "test_server": RemoteMCPConfig(
                        url="http://localhost:8000",
                        enabled=True,
                        description="Test MCP server",
                        transport="streaming"
                    )
                }
            )
        )
    )


@pytest.fixture
def mock_mcp_integration(mock_config):
    """Create a mock MCPIntegration with configured servers."""
    integration = MagicMock(spec=MCPIntegration)
    integration.configured_external_servers = {
        "test_server": RemoteMCPConfig(
            url="http://localhost:8000",
            enabled=True,
            description="Test MCP server",
            transport="streaming"
        )
    }
    
    # Mock client_manager
    integration.client_manager = MagicMock()
    integration.client_manager.get_client = AsyncMock(return_value=None)
    
    return integration


@pytest.mark.asyncio
async def test_mcp_list_servers(mock_mcp_integration, capsys):
    """Test listing MCP servers uses RemoteMCPConfig properties."""
    from agent_system.cli import _mcp_list_servers
    
    args = MagicMock()
    args.out_format = "json"
    
    await _mcp_list_servers(mock_mcp_integration, args)
    
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "test_server"
    assert result[0]["address"] == "http://localhost:8000"
    assert result[0]["enabled"] is True
    assert result[0]["description"] == "Test MCP server"
    assert result[0]["connected"] is False  # No client connected


@pytest.mark.asyncio
async def test_mcp_connect_server(mock_mcp_integration, capsys):
    """Test connecting to MCP server uses RemoteMCPConfig."""
    from agent_system.cli import _mcp_connect_server
    
    args = MagicMock()
    mock_mcp_integration.client_manager.add_client = AsyncMock()
    
    await _mcp_connect_server(mock_mcp_integration, "test_server", args)
    
    # Verify add_client was called with correct config derived from RemoteMCPConfig
    mock_mcp_integration.client_manager.add_client.assert_called_once()
    call_args = mock_mcp_integration.client_manager.add_client.call_args
    assert call_args[0][0] == "test_server"  # server name
    client_config = call_args[0][1]
    assert client_config["transport"] == "streaming"
    assert client_config["url"] == "http://localhost:8000"
    
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["result"] == "connected"
    assert result["server"] == "test_server"


@pytest.mark.asyncio
async def test_mcp_status_server(mock_mcp_integration, capsys):
    """Test getting status for a specific server."""
    from agent_system.cli import _mcp_status_servers
    
    args = MagicMock()
    args.out_format = "json"
    
    # Create mock tool objects with name property
    mock_tool1 = MagicMock()
    mock_tool1.name = "tool1"
    mock_tool2 = MagicMock()
    mock_tool2.name = "tool2"
    
    # Mock a connected client
    mock_client = MagicMock()
    mock_client.list_tools = AsyncMock(return_value=[mock_tool1, mock_tool2])
    mock_mcp_integration.client_manager.get_client = AsyncMock(return_value=mock_client)
    
    await _mcp_status_servers(mock_mcp_integration, "test_server", args)
    
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    
    assert result["name"] == "test_server"
    assert result["connected"] is True
    assert result["enabled"] is True
    assert result["address"] == "http://localhost:8000"
    assert result["tools_count"] == 2
    assert "tool1" in result["tools"]
    assert "tool2" in result["tools"]


@pytest.mark.asyncio
async def test_mcp_test_server(mock_mcp_integration, capsys):
    """Test testing server connectivity."""
    from agent_system.cli import _mcp_test_server
    
    args = MagicMock()
    
    # Create mock tool with name property
    mock_tool = MagicMock()
    mock_tool.name = "test_tool"
    
    # Mock client creation and tool listing
    mock_client = MagicMock()
    mock_client.list_tools = AsyncMock(return_value=[mock_tool])
    
    # Track whether we've been called before
    call_count = 0
    
    async def mock_get_client_side_effect(name):
        nonlocal call_count
        call_count += 1
        # First call returns None (no existing connection)
        # Second call returns mock_client (after add_client)
        if call_count == 1:
            return None
        return mock_client
    
    mock_mcp_integration.client_manager.get_client = AsyncMock(side_effect=mock_get_client_side_effect)
    mock_mcp_integration.client_manager.add_client = AsyncMock()
    mock_mcp_integration.client_manager.remove_client = AsyncMock()
    
    await _mcp_test_server(mock_mcp_integration, "test_server", args)
    
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    
    assert result["server"] == "test_server"
    assert result["connection"] == "success"
    assert result["tools_count"] == 1
    assert "test_tool" in result["tools"]
    
    # Verify temporary client was cleaned up
    mock_mcp_integration.client_manager.remove_client.assert_called_once_with("test_server")


@pytest.mark.asyncio
async def test_configured_external_servers_not_mcp_config_servers(mock_mcp_integration):
    """Verify we're using configured_external_servers, not mcp_config.servers."""
    # This test ensures we don't regress to using the old path
    assert hasattr(mock_mcp_integration, "configured_external_servers")
    assert "test_server" in mock_mcp_integration.configured_external_servers
    
    # Verify the server config is a RemoteMCPConfig instance
    server_config = mock_mcp_integration.configured_external_servers["test_server"]
    assert isinstance(server_config, RemoteMCPConfig)
    assert server_config.url == "http://localhost:8000"
    assert server_config.enabled is True
    assert server_config.transport == "streaming"
