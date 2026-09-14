"""
Tests for MCPService

Comprehensive test suite for MCP server management service.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_system.services.mcp_service import MCPService
from agent_system.config.models import AgentSystemConfig, PluginsConfig, RemoteMCPConfig


@pytest.fixture
def mock_config():
    """Fixture providing a mock AgentSystemConfig with MCP servers."""
    return AgentSystemConfig(
        plugins=PluginsConfig(
            servers={},
            plugin_dirs=[]
        )
    )


@pytest.fixture
def mock_mcp_integration():
    """Fixture providing a mock MCPIntegration."""
    mcp = MagicMock()
    mcp.initialized = True
    mcp.configured_external_servers = {
        "test_server": RemoteMCPConfig(
            enabled=True,
            transport="http",
            url="http://localhost:8000",
            description="Test MCP Server"
        ),
        "disabled_server": RemoteMCPConfig(
            enabled=False,
            transport="http",
            url="http://localhost:8001",
            description="Disabled Server"
        )
    }
    # The external half is a plugin now: the integration looks the provider up
    # through the capability registry and reads its pool. A MagicMock would
    # answer every attribute, so the double has to be shaped like the real one.
    mcp.external_provider = MagicMock()
    mcp.external_provider.pool = MagicMock()
    mcp.external_provider.pool.get = MagicMock(return_value=None)
    mcp.list_external_clients = MagicMock(return_value=[])
    mcp.retry_connect_server = AsyncMock(return_value=True)
    mcp.remove_external_server = AsyncMock()
    return mcp


@pytest.fixture
def mcp_service(mock_mcp_integration, mock_config):
    """Fixture providing a MCPService instance."""
    return MCPService(mock_mcp_integration, mock_config)


class TestMCPServiceInit:
    """Test MCPService initialization."""

    def test_init(self, mock_mcp_integration, mock_config):
        """Test MCPService initialization."""
        service = MCPService(mock_mcp_integration, mock_config)
        
        assert service._mcp == mock_mcp_integration
        assert service._config == mock_config


class TestListServers:
    """Test server listing functionality."""

    @pytest.mark.asyncio
    async def test_list_all_servers(self, mcp_service):
        """Test listing all servers."""
        # Mock _get_client_safe to return None (not connected)
        with patch.object(mcp_service, '_get_client_safe', new=AsyncMock(return_value=None)):
            servers = await mcp_service.list_servers()
        
        assert len(servers) == 2
        assert any(s["name"] == "test_server" for s in servers)
        assert any(s["name"] == "disabled_server" for s in servers)

    @pytest.mark.asyncio
    async def test_list_enabled_servers_only(self, mcp_service):
        """Test listing only enabled servers."""
        with patch.object(mcp_service, '_get_client_safe', new=AsyncMock(return_value=None)):
            servers = await mcp_service.list_servers(enabled_only=True)
        
        assert len(servers) == 1
        assert servers[0]["name"] == "test_server"
        assert servers[0]["enabled"] is True

    @pytest.mark.asyncio
    async def test_list_servers_with_tools(self, mcp_service):
        """Test listing servers with tool information."""
        # Mock client with tools
        mock_client = AsyncMock()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"
        mock_client.list_tools = AsyncMock(return_value=[mock_tool])
        
        with patch.object(mcp_service, '_get_client_safe', new=AsyncMock(return_value=mock_client)):
            servers = await mcp_service.list_servers(include_tools=True)
        
        # Find test_server in results
        test_server = next((s for s in servers if s["name"] == "test_server"), None)
        assert test_server is not None
        assert "tools" in test_server
        assert test_server["tool_count"] == 1
        assert "test_tool" in test_server["tools"]

    @pytest.mark.asyncio
    async def test_list_servers_connection_status(self, mcp_service):
        """Test that connection status is correctly reflected."""
        # Mock one connected, one not
        async def mock_get_client(name):
            if name == "test_server":
                return AsyncMock()  # Connected
            return None  # Not connected
        
        with patch.object(mcp_service, '_get_client_safe', new=mock_get_client):
            servers = await mcp_service.list_servers()
        
        test_server = next((s for s in servers if s["name"] == "test_server"), None)
        disabled_server = next((s for s in servers if s["name"] == "disabled_server"), None)
        
        assert test_server["connected"] is True
        assert disabled_server["connected"] is False


class TestGetServerStatus:
    """Test server status functionality."""

    @pytest.mark.asyncio
    async def test_get_status_existing_server(self, mcp_service):
        """Test getting status for an existing server."""
        with patch.object(mcp_service, '_get_client_safe', new=AsyncMock(return_value=None)):
            status = await mcp_service.get_server_status("test_server")
        
        assert status is not None
        assert status["name"] == "test_server"
        assert status["enabled"] is True
        assert "url" in status

    @pytest.mark.asyncio
    async def test_get_status_nonexistent_server(self, mcp_service):
        """Test getting status for non-existent server."""
        status = await mcp_service.get_server_status("nonexistent")
        
        assert status is None

    @pytest.mark.asyncio
    async def test_get_status_with_tools(self, mcp_service):
        """Test getting status with tool information."""
        mock_client = AsyncMock()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"
        mock_client.list_tools = AsyncMock(return_value=[mock_tool])
        
        with patch.object(mcp_service, '_get_client_safe', new=AsyncMock(return_value=mock_client)):
            status = await mcp_service.get_server_status("test_server", include_tools=True)
        
        assert status["connected"] is True
        assert "tools" in status
        assert status["tool_count"] == 1

    @pytest.mark.asyncio
    async def test_get_status_connection_error(self, mcp_service):
        """Test getting status when connection fails."""
        async def mock_get_client_error(name):
            raise ConnectionError("Connection failed")
        
        with patch.object(mcp_service, '_get_client_safe', new=mock_get_client_error):
            status = await mcp_service.get_server_status("test_server")
        
        assert status["connected"] is False
        assert "error" in status


class TestTestServer:
    """Test server testing functionality."""

    @pytest.mark.asyncio
    async def test_test_nonexistent_server(self, mcp_service):
        """Test testing non-existent server."""
        result = await mcp_service.test_server("nonexistent")
        
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_test_disabled_server(self, mcp_service):
        """Test testing disabled server."""
        result = await mcp_service.test_server("disabled_server")
        
        assert result["success"] is False
        assert "disabled" in result["error"]

    @pytest.mark.asyncio
    async def test_test_success(self, mcp_service):
        """Test successful server test."""
        mock_client = AsyncMock()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"
        mock_client.list_tools = AsyncMock(return_value=[mock_tool])
        
        with patch.object(mcp_service, '_get_client_safe', new=AsyncMock(return_value=mock_client)):
            result = await mcp_service.test_server("test_server")
        
        assert result["success"] is True
        assert result["connected"] is True
        assert result["tools_available"] == 1
        assert "response_time_ms" in result

    @pytest.mark.asyncio
    async def test_test_connection_failure(self, mcp_service):
        """Test server test with connection failure."""
        with patch.object(mcp_service, '_get_client_safe', new=AsyncMock(return_value=None)):
            result = await mcp_service.test_server("test_server")
        
        assert result["success"] is False
        assert result["connected"] is False


class TestListAllTools:
    """Test tool listing functionality."""

    @pytest.mark.asyncio
    async def test_list_all_tools(self, mcp_service):
        """Test listing all tools."""
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {
                "test_server": [{"name": "tool1"}, {"name": "tool2"}]
            },
            "plugins": {
                "plugin1": [{"name": "tool3"}]
            }
        })
        
        tools = await mcp_service.list_all_tools()
        
        assert "test_server" in tools
        assert "plugin1" in tools
        assert len(tools["test_server"]) == 2

    @pytest.mark.asyncio
    async def test_list_tools_for_specific_server(self, mcp_service):
        """Test listing tools for specific server."""
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {
                "test_server": [{"name": "tool1"}]
            },
            "plugins": {}
        })
        
        tools = await mcp_service.list_all_tools(server_name="test_server")
        
        assert "test_server" in tools
        assert len(tools) == 1

    @pytest.mark.asyncio
    async def test_list_tools_filter_blocked(self, mcp_service):
        """Test filtering blocked tools with include_blocked=False."""
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {
                "test_server": [
                    {"name": "tool1", "blocked": False},
                    {"name": "tool2", "blocked": True},
                    {"name": "tool3", "blocked": False}
                ]
            },
            "plugins": {
                "plugin1": [
                    {"name": "tool4", "blocked": True},
                    {"name": "tool5", "blocked": False}
                ]
            }
        })
        
        # With include_blocked=False, should filter out blocked tools
        tools = await mcp_service.list_all_tools(include_blocked=False)
        
        assert "test_server" in tools
        assert "plugin1" in tools
        assert len(tools["test_server"]) == 2  # tool1, tool3 (tool2 blocked)
        assert len(tools["plugin1"]) == 1  # tool5 (tool4 blocked)
        
        # Verify blocked tools are actually filtered
        test_tool_names = [t["name"] for t in tools["test_server"]]
        assert "tool1" in test_tool_names
        assert "tool2" not in test_tool_names  # Blocked, should be filtered
        assert "tool3" in test_tool_names

    @pytest.mark.asyncio
    async def test_list_tools_include_blocked(self, mcp_service):
        """Test including blocked tools with include_blocked=True."""
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {
                "test_server": [
                    {"name": "tool1", "blocked": False},
                    {"name": "tool2", "blocked": True}
                ]
            },
            "plugins": {}
        })
        
        # With include_blocked=True (default), should include all tools
        tools = await mcp_service.list_all_tools(include_blocked=True)
        
        assert "test_server" in tools
        assert len(tools["test_server"]) == 2  # Both tools included

    @pytest.mark.asyncio
    async def test_list_tools_specific_server_filter_blocked(self, mcp_service):
        """Test filtering blocked tools for specific server."""
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {
                "test_server": [
                    {"name": "tool1", "blocked": False},
                    {"name": "tool2", "blocked": True}
                ]
            },
            "plugins": {}
        })
        
        # Filter blocked for specific server
        tools = await mcp_service.list_all_tools(server_name="test_server", include_blocked=False)
        
        assert "test_server" in tools
        assert len(tools["test_server"]) == 1  # Only tool1

    @pytest.mark.asyncio
    async def test_list_tools_error(self, mcp_service):
        """Test tool listing with error."""
        mcp_service._mcp.list_all_tools = AsyncMock(side_effect=RuntimeError("List failed"))
        
        tools = await mcp_service.list_all_tools()
        
        assert tools == {}


class TestGetClientSafe:
    """Test safe client retrieval."""

    @pytest.mark.asyncio
    async def test_get_client_safe_success(self, mcp_service):
        """Test successful client retrieval."""
        mock_client = AsyncMock()
        mcp_service._mcp.external_provider.pool.get = MagicMock(return_value=mock_client)
        
        client = await mcp_service._get_client_safe("test_server")
        
        assert client == mock_client

    @pytest.mark.asyncio
    async def test_get_client_safe_none(self, mcp_service):
        """Test client retrieval returning None."""
        mcp_service._mcp.external_provider.pool.get = MagicMock(return_value=None)
        
        client = await mcp_service._get_client_safe("test_server")
        
        assert client is None

    @pytest.mark.asyncio
    async def test_get_client_safe_exception(self, mcp_service):
        """Test client retrieval with exception."""
        mcp_service._mcp.external_provider.pool.get = MagicMock(side_effect=RuntimeError("Failed"))
        
        client = await mcp_service._get_client_safe("test_server")
        
        assert client is None


class TestGetComprehensiveStatus:
    """Test comprehensive MCP status retrieval."""

    @pytest.mark.asyncio
    async def test_comprehensive_status_with_external_servers(self, mcp_service):
        """Test comprehensive status with external servers."""
        # Mock external servers
        mcp_service._mcp.list_external_clients = MagicMock(return_value=["test_server"])
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {
                "test_server": [
                    {
                        "name": "test_tool",
                        "description": "Test Tool",
                        "parameters": {},
                        "blocked": False
                    }
                ]
            },
            "plugins": {}
        })
        
        status = await mcp_service.get_comprehensive_status(registry=None)
        
        assert "plugins" in status
        assert "external_servers" in status
        assert "test_server" in status["external_servers"]
        assert status["external_servers"]["test_server"]["tool_count"] == 1
        assert status["total_servers"] >= 1

    @pytest.mark.asyncio
    async def test_comprehensive_status_with_plugin_servers(self, mcp_service):
        """Test comprehensive status with plugin servers from registry."""
        # Mock registry with plugin servers
        mock_registry = MagicMock()
        mock_server = MagicMock()
        mock_server._mcp_public = True
        
        # Mock list_tools
        mock_tool = MagicMock()
        mock_tool.name = "plugin_tool"
        mock_tool.description = "Plugin Tool"
        mock_tool.input_schema = {}
        mock_server.list_tools = AsyncMock(return_value=[mock_tool])
        
        mock_registry._servers = {"plugin_server": mock_server}
        
        mcp_service._mcp.list_external_clients = MagicMock(return_value=[])
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {},
            "plugins": {}
        })
        
        status = await mcp_service.get_comprehensive_status(registry=mock_registry)
        
        assert "plugins" in status
        assert "plugin_server" in status["plugins"]
        assert status["plugins"]["plugin_server"]["tool_count"] == 1
        assert status["plugins"]["plugin_server"]["connected"] is True

    @pytest.mark.asyncio
    async def test_comprehensive_status_hybrid_plugin_tools_via_adapter(self, mcp_service):
        """A hybrid plugin (get_tools(), no list_tools()) must still report its tools.

        Deliberately built from a REAL class, not MagicMock: a MagicMock answers
        hasattr() for every name, so it cannot tell a plugin that carries
        list_tools() from one that does not -- which is precisely the shape
        difference under test. The registry holds the raw plugin server, so the
        service has to reach for the PluginMCPAdapter to read the tools.
        """
        from agent_system.plugins.mcp_adapter import PluginMCPAdapter

        class HybridPlugin:
            """Web+MCP plugin: no MCPServer base, therefore no list_tools()."""

            def get_tools(self):
                return [
                    {"type": "function", "function": {
                        "name": "hybrid_do", "description": "Do it",
                        "parameters": {"type": "object", "properties": {}}}},
                    {"type": "function", "function": {
                        "name": "hybrid_undo", "description": "Undo it",
                        "parameters": {"type": "object", "properties": {}}}},
                ]

        raw = HybridPlugin()
        assert not hasattr(raw, "list_tools")  # guards the premise of this test

        mock_registry = MagicMock()
        mock_registry._servers = {"hybrid": raw}

        plugin_registry = MagicMock()
        plugin_registry.get_server = MagicMock(return_value=PluginMCPAdapter("hybrid", raw))
        mcp_service._mcp.plugin_registry = plugin_registry

        mcp_service._mcp.list_external_clients = MagicMock(return_value=[])
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {},
            "plugins": {}
        })

        status = await mcp_service.get_comprehensive_status(registry=mock_registry)

        assert status["plugins"]["hybrid"]["tool_count"] == 2
        assert status["plugins"]["hybrid"]["tools"] == ["hybrid_do", "hybrid_undo"]

    @pytest.mark.asyncio
    async def test_comprehensive_status_skips_private_servers(self, mcp_service):
        """_mcp_public=False stays hidden -- including via the adapter fallback.

        Agent servers default to private ("secure by default", see
        servers/bootstrap.py). The flag lives on the raw registry object; the
        adapter does not carry it, so reading tools through the adapter must
        not become a way around the filter.
        """
        from agent_system.plugins.mcp_adapter import PluginMCPAdapter

        class PrivateHybrid:
            _mcp_public = False

            def get_tools(self):
                return [{"type": "function", "function": {
                    "name": "secret", "description": "", "parameters": {}}}]

        raw = PrivateHybrid()
        mock_registry = MagicMock()
        mock_registry._servers = {"private_agent": raw}

        plugin_registry = MagicMock()
        plugin_registry.get_server = MagicMock(
            return_value=PluginMCPAdapter("private_agent", raw))
        mcp_service._mcp.plugin_registry = plugin_registry

        mcp_service._mcp.list_external_clients = MagicMock(return_value=[])
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {},
            "plugins": {}
        })

        status = await mcp_service.get_comprehensive_status(registry=mock_registry)

        assert "private_agent" not in status["plugins"]

    @pytest.mark.asyncio
    async def test_comprehensive_status_empty(self, mcp_service):
        """Test comprehensive status with no servers."""
        mcp_service._mcp.configured_external_servers = {}
        mcp_service._mcp.list_external_clients = MagicMock(return_value=[])
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {},
            "plugins": {}
        })
        
        status = await mcp_service.get_comprehensive_status(registry=None)
        
        assert status["total_servers"] == 0
        assert status["total_tools"] == 0
        assert len(status["plugins"]) == 0
        assert len(status["external_servers"]) == 0

    @pytest.mark.asyncio
    async def test_comprehensive_status_filters_disabled_servers(self, mcp_service):
        """Test that disabled servers are filtered out."""
        mcp_service._mcp.list_external_clients = MagicMock(return_value=[])
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {},
            "plugins": {}
        })
        
        status = await mcp_service.get_comprehensive_status(registry=None)
        
        # disabled_server should not appear in status
        assert "disabled_server" not in status["external_servers"]
        # But test_server (enabled=True) might appear if connected
        # (depends on configured_external_servers in mock)

    @pytest.mark.asyncio
    async def test_comprehensive_status_with_connectivity_check_true(self, mcp_service):
        """Test comprehensive status with check_connectivity=True (real-time checks)."""
        from unittest.mock import patch
        from agent_system.config.models import RemoteMCPConfig
        
        # Mock external servers with proper config object
        config = RemoteMCPConfig(url="http://localhost:8080", enabled=True, description="Test Server")
        mcp_service._mcp.configured_external_servers = {"test_server": config}
        mcp_service._mcp.list_external_clients = MagicMock(return_value=["test_server"])
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {
                "test_server": [{"name": "tool1"}]
            },
            "plugins": {}
        })
        
        # Patch the connectivity helper method to return True
        with patch.object(mcp_service, '_check_external_connectivity', new=AsyncMock(return_value=True)) as mock_check:
            status = await mcp_service.get_comprehensive_status(registry=None, check_connectivity=True)
            
            # Verify connectivity check was called
            mock_check.assert_called_once_with("test_server", "http://localhost:8080")
            
            # Verify status reflects connectivity
            assert "test_server" in status["external_servers"]
            assert status["external_servers"]["test_server"]["connected"] is True

    @pytest.mark.asyncio
    async def test_comprehensive_status_with_connectivity_check_false(self, mcp_service):
        """Test comprehensive status with check_connectivity=False (fast path)."""
        from unittest.mock import patch
        from agent_system.config.models import RemoteMCPConfig
        
        # Mock external servers with proper config object
        config = RemoteMCPConfig(url="http://localhost:8080", enabled=True, description="Test Server")
        mcp_service._mcp.configured_external_servers = {"test_server": config}
        mcp_service._mcp.list_external_clients = MagicMock(return_value=["test_server"])
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {
                "test_server": [{"name": "tool1"}]
            },
            "plugins": {}
        })
        
        # Patch the connectivity helper method
        with patch.object(mcp_service, '_check_external_connectivity', new=AsyncMock(return_value=True)) as mock_check:
            status = await mcp_service.get_comprehensive_status(registry=None, check_connectivity=False)
            
            # Verify connectivity check was NOT called (fast path)
            mock_check.assert_not_called()
            
            # Verify status uses fast path (checks if in connected_servers list)
            assert "test_server" in status["external_servers"]
            # Connected=True because test_server is in list_clients result and has tools

    @pytest.mark.asyncio
    async def test_comprehensive_status_plugin_connectivity_check_true(self, mcp_service):
        """Test plugin connectivity with check_connectivity=True."""
        from unittest.mock import patch
        
        # Mock plugin registry
        mock_registry = MagicMock()
        mock_server = MagicMock()
        mock_server._mcp_public = True
        mock_tool = MagicMock()
        mock_tool.name = "plugin_tool"
        mock_tool.description = "Plugin Tool"
        mock_tool.input_schema = {}
        mock_server.list_tools = AsyncMock(return_value=[mock_tool])
        mock_registry._servers = {"plugin_server": mock_server}
        
        mcp_service._mcp.list_external_clients = MagicMock(return_value=[])
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {},
            "plugins": {}
        })
        
        # Patch plugin connectivity helper (synchronous method)
        with patch.object(mcp_service, '_check_plugin_connectivity', return_value=True) as mock_check:
            status = await mcp_service.get_comprehensive_status(registry=mock_registry, check_connectivity=True)
            
            # Verify plugin connectivity check was called
            mock_check.assert_called_once_with("plugin_server", mock_registry)
            
            # Verify plugin is marked as connected
            assert "plugin_server" in status["plugins"]
            assert status["plugins"]["plugin_server"]["connected"] is True

    @pytest.mark.asyncio
    async def test_comprehensive_status_connectivity_check_disconnected(self, mcp_service):
        """Test comprehensive status when connectivity check returns False."""
        from unittest.mock import patch
        from agent_system.config.models import RemoteMCPConfig
        
        # Mock external server that appears disconnected
        config = RemoteMCPConfig(url="http://localhost:8080", enabled=True, description="Test Server")
        mcp_service._mcp.configured_external_servers = {"test_server": config}
        mcp_service._mcp.list_external_clients = MagicMock(return_value=["test_server"])
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {
                "test_server": [{"name": "tool1"}]
            },
            "plugins": {}
        })
        
        # Patch connectivity check to return False (disconnected)
        with patch.object(mcp_service, '_check_external_connectivity', new=AsyncMock(return_value=False)):
            status = await mcp_service.get_comprehensive_status(registry=None, check_connectivity=True)
            
            # With strict mode (check_connectivity=True), connectivity check result is authoritative
            # Even if server has cached tools, if socket check fails, mark as disconnected
            # This prevents showing "connected" for servers that are actually down
            assert "test_server" in status["external_servers"]
            assert status["external_servers"]["test_server"]["connected"] is False  # Strict mode: trust socket check

    @pytest.mark.asyncio
    async def test_comprehensive_status_truly_disconnected(self, mcp_service):
        """Test comprehensive status when server is truly disconnected (no tools, no connectivity)."""
        from unittest.mock import patch
        from agent_system.config.models import RemoteMCPConfig
        
        # Mock external server that is truly disconnected
        config = RemoteMCPConfig(url="http://localhost:8080", enabled=True, description="Test Server")
        mcp_service._mcp.configured_external_servers = {"test_server": config}
        mcp_service._mcp.list_external_clients = MagicMock(return_value=[])  # No active client
        mcp_service._mcp.list_all_tools = AsyncMock(return_value={
            "external_servers": {
                "test_server": []  # No tools
            },
            "plugins": {}
        })
        
        # Patch connectivity check to return False (disconnected)
        with patch.object(mcp_service, '_check_external_connectivity', new=AsyncMock(return_value=False)):
            status = await mcp_service.get_comprehensive_status(registry=None, check_connectivity=True)
            
            # Server should be marked as disconnected (no connectivity, no active client, no tools)
            assert "test_server" in status["external_servers"]
            assert status["external_servers"]["test_server"]["connected"] is False

