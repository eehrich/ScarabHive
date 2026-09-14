"""
Tests for ToolService

Comprehensive test suite for tool management service.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_system.services.tool_service import ToolService
from agent_system.utils.io import atomic_write_text
from agent_system.config.models import AgentSystemConfig, PluginsConfig, RemoteMCPConfig, ToolConfig


@pytest.fixture
def mock_config():
    """Fixture providing a mock AgentSystemConfig."""
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
            description="Test Server",
            tools=ToolConfig(
                allowed=["tool1", "tool2"],
                blocked=["tool3"]
            )
        ),
        "no_filter_server": RemoteMCPConfig(
            enabled=True,
            transport="http",
            url="http://localhost:8001",
            description="No Filter Server",
            tools=None
        )
    }
    mcp.client_manager = MagicMock()
    return mcp


@pytest.fixture
def tool_service(mock_mcp_integration, mock_config):
    """Fixture providing a ToolService instance."""
    return ToolService(mock_mcp_integration, mock_config)


class TestToolServiceInit:
    """Test ToolService initialization."""

    def test_init(self, mock_mcp_integration, mock_config):
        """Test ToolService initialization."""
        service = ToolService(mock_mcp_integration, mock_config)
        
        assert service._mcp == mock_mcp_integration
        assert service._config == mock_config


class TestListTools:
    """Test tool listing functionality."""

    @pytest.mark.asyncio
    async def test_list_tools_server_not_found(self, tool_service):
        """Test listing tools for non-existent server."""
        result = await tool_service.list_tools("nonexistent")
        
        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_list_tools_with_filtering(self, tool_service):
        """Test listing tools with filtering configuration."""
        # Mock client with tools
        mock_client = AsyncMock()
        mock_tool1 = MagicMock()
        mock_tool1.name = "tool1"
        mock_tool2 = MagicMock()
        mock_tool2.name = "tool2"
        mock_tool3 = MagicMock()
        mock_tool3.name = "tool3"
        mock_tool4 = MagicMock()
        mock_tool4.name = "tool4"
        mock_client.list_tools = AsyncMock(return_value=[mock_tool1, mock_tool2, mock_tool3, mock_tool4])
        
        with patch.object(tool_service, '_get_client_safe', new=AsyncMock(return_value=mock_client)):
            result = await tool_service.list_tools("test_server")
        
        assert len(result["available_tools"]) == 4
        # Only the blocked list filters: it is what mcp_client refuses to call.
        # tool4 is outside the server's allowed list and still callable,
        # because nothing enforces that list -- it must not look filtered.
        assert result["effective_tools"] == ["tool1", "tool2", "tool4"]
        assert result["filtering"] == {"blocked_tools": ["tool3"]}

    @pytest.mark.asyncio
    async def test_list_tools_no_filtering(self, tool_service):
        """Test listing tools with no filtering configured."""
        mock_client = AsyncMock()
        mock_tool = MagicMock()
        mock_tool.name = "tool1"
        mock_client.list_tools = AsyncMock(return_value=[mock_tool])
        
        with patch.object(tool_service, '_get_client_safe', new=AsyncMock(return_value=mock_client)):
            result = await tool_service.list_tools("no_filter_server")
        
        assert result["available_tools"] == ["tool1"]
        assert result["effective_tools"] == ["tool1"]

    @pytest.mark.asyncio
    async def test_list_tools_blocked_filtering(self, tool_service):
        """Test listing tools with blocked list only."""
        # Create server with only blocked tools
        tool_service._mcp.configured_external_servers["blocked_only"] = RemoteMCPConfig(
            enabled=True,
            transport="http",
            url="http://localhost:8002",
            tools=ToolConfig(allowed=None, blocked=["bad_tool"])
        )
        
        mock_client = AsyncMock()
        mock_tool1 = MagicMock()
        mock_tool1.name = "good_tool"
        mock_tool2 = MagicMock()
        mock_tool2.name = "bad_tool"
        mock_client.list_tools = AsyncMock(return_value=[mock_tool1, mock_tool2])
        
        with patch.object(tool_service, '_get_client_safe', new=AsyncMock(return_value=mock_client)):
            result = await tool_service.list_tools("blocked_only")
        
        assert "good_tool" in result["effective_tools"]
        assert "bad_tool" not in result["effective_tools"]

    @pytest.mark.asyncio
    async def test_list_tools_client_creation(self, tool_service):
        """Test that temporary client is created and cleaned up."""
        call_count = {"count": 0}
        
        async def mock_get_client(name):
            call_count["count"] += 1
            if call_count["count"] == 1:
                return None  # First call: no client
            # After add_client, return mock client
            mock_client = AsyncMock()
            mock_tool = MagicMock()
            mock_tool.name = "tool1"
            mock_client.list_tools = AsyncMock(return_value=[mock_tool])
            return mock_client
        
        # The temporary connection goes through the integration now, which
        # hands it to the client plugin's pool. The old version passed a plain
        # dict where a RemoteMCPConfig was expected, so it could never connect.
        tool_service._mcp.retry_connect_server = AsyncMock(return_value=True)
        tool_service._mcp.remove_external_server = AsyncMock()

        with patch.object(tool_service, '_get_client_safe', new=mock_get_client):
            result = await tool_service.list_tools("test_server")

        assert tool_service._mcp.retry_connect_server.called
        assert tool_service._mcp.remove_external_server.called
        assert "available_tools" in result


class TestAtomicWrite:
    """Test atomic file writing utility."""

    def test_atomic_write_success(self, tmp_path):
        """Test successful atomic write."""
        test_file = tmp_path / "test.txt"
        content = "test content"
        
        atomic_write_text(test_file, content)
        
        assert test_file.exists()
        assert test_file.read_text() == content

    def test_atomic_write_overwrites(self, tmp_path):
        """Test that atomic write overwrites existing file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("old content")
        
        atomic_write_text(test_file, "new content")
        
        assert test_file.read_text() == "new content"


class TestGetClientSafe:
    """Test safe client retrieval."""

    @pytest.mark.asyncio
    async def test_get_client_safe_success(self, tool_service):
        """Test successful client retrieval."""
        mock_client = AsyncMock()
        tool_service._mcp.external_provider.pool.get = MagicMock(return_value=mock_client)
        
        client = await tool_service._get_client_safe("test_server")
        
        assert client == mock_client

    @pytest.mark.asyncio
    async def test_get_client_safe_none(self, tool_service):
        """Test client retrieval returning None."""
        tool_service._mcp.external_provider.pool.get = MagicMock(return_value=None)
        
        client = await tool_service._get_client_safe("test_server")
        
        assert client is None

    @pytest.mark.asyncio
    async def test_get_client_safe_exception(self, tool_service):
        """Test client retrieval with exception."""
        tool_service._mcp.external_provider.pool.get = MagicMock(side_effect=RuntimeError("Failed"))
        
        client = await tool_service._get_client_safe("test_server")
        
        assert client is None
