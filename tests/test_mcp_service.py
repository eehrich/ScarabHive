"""
Tests for MCPService

Comprehensive test suite for MCP server management service.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_system.services.mcp_service import MCPService
from agent_system.config.models import AgentSystemConfig, MCPSystemConfig, RemoteMCPConfig


@pytest.fixture
def mock_config():
    """Fixture providing a mock AgentSystemConfig with MCP servers."""
    return AgentSystemConfig(
        mcp_system=MCPSystemConfig(
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
    mcp.client_manager = MagicMock()
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


class TestConnectServer:
    """Test server connection functionality."""

    @pytest.mark.asyncio
    async def test_connect_nonexistent_server(self, mcp_service):
        """Test connecting to non-existent server."""
        result = await mcp_service.connect_server("nonexistent")
        
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_connect_already_connected(self, mcp_service):
        """Test connecting to already connected server."""
        mock_client = AsyncMock()
        
        with patch.object(mcp_service, '_get_client_safe', new=AsyncMock(return_value=mock_client)):
            result = await mcp_service.connect_server("test_server")
        
        assert result["success"] is True
        assert "already connected" in result["message"]

    @pytest.mark.asyncio
    async def test_connect_success(self, mcp_service):
        """Test successful server connection."""
        # Mock not connected initially, then connected
        call_count = {"count": 0}
        
        async def mock_get_client(name):
            call_count["count"] += 1
            if call_count["count"] == 1:
                return None  # First call: not connected
            return AsyncMock()  # Second call: connected
        
        mcp_service._mcp.connect_external_server = AsyncMock()
        
        with patch.object(mcp_service, '_get_client_safe', new=mock_get_client):
            result = await mcp_service.connect_server("test_server")
        
        assert result["success"] is True


class TestDisconnectServer:
    """Test server disconnection functionality."""

    @pytest.mark.asyncio
    async def test_disconnect_not_connected(self, mcp_service):
        """Test disconnecting from not connected server."""
        with patch.object(mcp_service, '_get_client_safe', new=AsyncMock(return_value=None)):
            result = await mcp_service.disconnect_server("test_server")
        
        assert result["success"] is True
        assert "not connected" in result["message"]

    @pytest.mark.asyncio
    async def test_disconnect_success(self, mcp_service):
        """Test successful disconnection."""
        mock_client = AsyncMock()
        mcp_service._mcp.disconnect_external_server = AsyncMock()
        
        with patch.object(mcp_service, '_get_client_safe', new=AsyncMock(return_value=mock_client)):
            result = await mcp_service.disconnect_server("test_server")
        
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_disconnect_error(self, mcp_service):
        """Test disconnection with error."""
        mock_client = AsyncMock()
        mcp_service._mcp.disconnect_external_server = AsyncMock(side_effect=RuntimeError("Disconnect failed"))
        
        with patch.object(mcp_service, '_get_client_safe', new=AsyncMock(return_value=mock_client)):
            result = await mcp_service.disconnect_server("test_server")
        
        assert result["success"] is False
        assert "error" in result


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
            "plugin_servers": {
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
            "plugin_servers": {}
        })
        
        tools = await mcp_service.list_all_tools(server_name="test_server")
        
        assert "test_server" in tools
        assert len(tools) == 1

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
        mcp_service._mcp.client_manager.get_client = MagicMock(return_value=mock_client)
        
        client = await mcp_service._get_client_safe("test_server")
        
        assert client == mock_client

    @pytest.mark.asyncio
    async def test_get_client_safe_none(self, mcp_service):
        """Test client retrieval returning None."""
        mcp_service._mcp.client_manager.get_client = MagicMock(return_value=None)
        
        client = await mcp_service._get_client_safe("test_server")
        
        assert client is None

    @pytest.mark.asyncio
    async def test_get_client_safe_exception(self, mcp_service):
        """Test client retrieval with exception."""
        mcp_service._mcp.client_manager.get_client = MagicMock(side_effect=RuntimeError("Failed"))
        
        client = await mcp_service._get_client_safe("test_server")
        
        assert client is None
