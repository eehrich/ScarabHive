"""
Tests for ToolService

Comprehensive test suite for tool management service.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import yaml

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
        mock_client.list_tools = AsyncMock(return_value=[mock_tool1, mock_tool2, mock_tool3])
        
        with patch.object(tool_service, '_get_client_safe', new=AsyncMock(return_value=mock_client)):
            result = await tool_service.list_tools("test_server")
        
        assert "available_tools" in result
        assert len(result["available_tools"]) == 3
        assert "effective_tools" in result
        # Only tool1 and tool2 should be in effective (allowed list)
        assert len(result["effective_tools"]) == 2
        assert "tool1" in result["effective_tools"]
        assert "tool2" in result["effective_tools"]
        assert "tool3" not in result["effective_tools"]

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


class TestBlockTool:
    """Test tool blocking functionality."""

    @pytest.mark.asyncio
    async def test_block_tool_server_not_found(self, tool_service):
        """Test blocking tool on non-existent server."""
        result = await tool_service.block_tool("nonexistent", "tool1")
        
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_block_tool_config_not_found(self, tool_service):
        """Test blocking tool when config file doesn't exist."""
        with patch('pathlib.Path.exists', return_value=False):
            result = await tool_service.block_tool("test_server", "tool1", Path("nonexistent.yaml"))
        
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_block_tool_success(self, tool_service, tmp_path):
        """Test successful tool blocking."""
        # Create temporary config file
        config_file = tmp_path / "mcp.yaml"
        config_data = {
            "external_servers": {
                "remote_servers": {
                    "test_server": {
                        "enabled": True,
                        "transport": "http",
                        "url": "http://localhost:8000",
                        "tools": {
                            "allowed": ["tool1", "tool2"],
                            "blocked": []
                        }
                    }
                }
            }
        }
        config_file.write_text(yaml.safe_dump(config_data))
        
        result = await tool_service.block_tool("test_server", "tool1", config_file)
        
        assert result["success"] is True
        assert result["tool"] == "tool1"
        
        # Verify file was updated
        updated = yaml.safe_load(config_file.read_text())
        server_tools = updated["external_servers"]["remote_servers"]["test_server"]["tools"]
        assert "tool1" in server_tools["blocked"]
        assert "tool1" not in server_tools["allowed"]

    @pytest.mark.asyncio
    async def test_block_tool_already_blocked(self, tool_service, tmp_path):
        """Test blocking tool that's already blocked."""
        config_file = tmp_path / "mcp.yaml"
        config_data = {
            "external_servers": {
                "remote_servers": {
                    "test_server": {
                        "tools": {
                            "blocked": ["tool1"]
                        }
                    }
                }
            }
        }
        config_file.write_text(yaml.safe_dump(config_data))
        
        result = await tool_service.block_tool("test_server", "tool1", config_file)
        
        assert result["success"] is True
        assert "already blocked" in result["message"]


class TestAllowTool:
    """Test tool allowing functionality."""

    @pytest.mark.asyncio
    async def test_allow_tool_server_not_found(self, tool_service):
        """Test allowing tool on non-existent server."""
        result = await tool_service.allow_tool("nonexistent", "tool1")
        
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_allow_tool_success(self, tool_service, tmp_path):
        """Test successful tool allowing."""
        config_file = tmp_path / "mcp.yaml"
        config_data = {
            "external_servers": {
                "remote_servers": {
                    "test_server": {
                        "enabled": True,
                        "transport": "http",
                        "url": "http://localhost:8000",
                        "tools": {
                            "allowed": [],
                            "blocked": ["tool1"]
                        }
                    }
                }
            }
        }
        config_file.write_text(yaml.safe_dump(config_data))
        
        result = await tool_service.allow_tool("test_server", "tool1", config_file)
        
        assert result["success"] is True
        assert result["tool"] == "tool1"
        
        # Verify file was updated
        updated = yaml.safe_load(config_file.read_text())
        server_tools = updated["external_servers"]["remote_servers"]["test_server"]["tools"]
        assert "tool1" in server_tools["allowed"]
        assert "tool1" not in server_tools["blocked"]

    @pytest.mark.asyncio
    async def test_allow_tool_already_allowed(self, tool_service, tmp_path):
        """Test allowing tool that's already allowed."""
        config_file = tmp_path / "mcp.yaml"
        config_data = {
            "external_servers": {
                "remote_servers": {
                    "test_server": {
                        "tools": {
                            "allowed": ["tool1"]
                        }
                    }
                }
            }
        }
        config_file.write_text(yaml.safe_dump(config_data))
        
        result = await tool_service.allow_tool("test_server", "tool1", config_file)
        
        assert result["success"] is True
        assert "already allowed" in result["message"]

    @pytest.mark.asyncio
    async def test_allow_tool_creates_tools_section(self, tool_service, tmp_path):
        """Test allowing tool when tools section doesn't exist."""
        config_file = tmp_path / "mcp.yaml"
        config_data = {
            "external_servers": {
                "remote_servers": {
                    "test_server": {
                        "enabled": True
                    }
                }
            }
        }
        config_file.write_text(yaml.safe_dump(config_data))
        
        result = await tool_service.allow_tool("test_server", "tool1", config_file)
        
        assert result["success"] is True
        
        # Verify tools section was created
        updated = yaml.safe_load(config_file.read_text())
        assert "tools" in updated["external_servers"]["remote_servers"]["test_server"]


class TestGetToolStatus:
    """Test tool status retrieval."""

    @pytest.mark.asyncio
    async def test_get_status_blocked(self, tool_service):
        """Test getting status of blocked tool."""
        result = await tool_service.get_tool_status("test_server", "tool3")
        
        assert result["status"] == "blocked"
        assert result["tool"] == "tool3"

    @pytest.mark.asyncio
    async def test_get_status_allowed(self, tool_service):
        """Test getting status of allowed tool."""
        result = await tool_service.get_tool_status("test_server", "tool1")
        
        assert result["status"] == "allowed"

    @pytest.mark.asyncio
    async def test_get_status_neutral(self, tool_service):
        """Test getting status of neutral tool (no filtering)."""
        result = await tool_service.get_tool_status("no_filter_server", "any_tool")
        
        assert result["status"] == "neutral"

    @pytest.mark.asyncio
    async def test_get_status_blocked_by_allowed_list(self, tool_service):
        """Test that tool not in allowed list is considered blocked."""
        result = await tool_service.get_tool_status("test_server", "tool_not_in_list")
        
        assert result["status"] == "blocked"

    @pytest.mark.asyncio
    async def test_get_status_server_not_found(self, tool_service):
        """Test getting status for non-existent server."""
        result = await tool_service.get_tool_status("nonexistent", "tool1")
        
        assert "error" in result


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
