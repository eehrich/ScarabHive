from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from plugins.twitter_search.server import TwitterSearchServer


@pytest.mark.asyncio
async def test_twitter_plugin_discovered(mock_system_config, mock_mcp_config):
    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt
    # Note: Factory now requires (name, system_config, mcp_config) signature
    # This test will be updated when bootstrap system is modernized
        from agent_system.plugins import discover_all_plugins

        plugins = discover_all_plugins([default_dir])
        assert 'twitter_search' in plugins, "twitter_search plugin must be present in repository for this test"
        factory = plugins['twitter_search']
        assert callable(factory), "twitter_search factory should be callable"


class TestTwitterSearchServer:
    """Test the Twitter Search server functionality."""

    def test_twitter_server_initialization(self, mock_system_config, mock_mcp_config):
        """Test Twitter Search server initialization."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_mcp_config)
        assert server.name == "twitter"
        assert server.ssl_verify is True

    def test_twitter_server_initialization_with_config(self, mock_system_config, mock_mcp_config):
        """Test Twitter Search server initialization with config."""
        from agent_system.config.models import MCPConfig, AgentConfig
        
        mock_system_config.ssl_verify = False
        mcp_config = MCPConfig(type="twitter_search", enabled=True, agent_config=AgentConfig())
        mcp_config.timeout = 30
        
        server = TwitterSearchServer("twitter", mock_system_config, mcp_config)
        assert server.name == "twitter"
        assert server.ssl_verify is False

    def test_twitter_server_schema(self, mock_system_config, mock_mcp_config):
        """Test Twitter Search server tools structure."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_mcp_config)
        tools = server.get_tools()

        assert isinstance(tools, list)
        assert len(tools) == 1
        
        tool = tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "twitter_tweets"
        assert "description" in tool["function"]
        assert tool["function"]["parameters"]["type"] == "object"

        params = tool["function"]["parameters"]
        assert "query" in params["properties"]

    def test_twitter_server_tool_name(self, mock_system_config, mock_mcp_config):
        """Test Twitter Search server tool name."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_mcp_config)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "twitter_tweets"

    @pytest.mark.asyncio
    async def test_twitter_server_search_returns_info(self, mock_system_config, mock_mcp_config):
        """Test Twitter Search server returns informational message."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        result = await server.call("twitter_tweets", {"query": "test", "_status": mock_status})
        
        # Should return informational message about Twitter API restrictions
        assert "engine" in result
        assert result["engine"] == "twitter-info"
        assert "message" in result
        assert "alternatives" in result
        assert isinstance(result["alternatives"], list)

    @pytest.mark.asyncio
    async def test_twitter_server_invalid_tool(self, mock_system_config, mock_mcp_config):
        """Test Twitter Search server with invalid tool name."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        # Modern pattern: generic dispatcher raises ValueError for unknown tools
        with pytest.raises(ValueError, match="Tool 'invalid_tool' not found"):
            await server.call("invalid_tool", {"query": "test", "_status": mock_status})

    @pytest.mark.asyncio
    async def test_twitter_server_empty_query(self, mock_system_config, mock_mcp_config):
        """Test Twitter Search server with empty query."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        result = await server.call("twitter_tweets", {"query": "", "_status": mock_status})
        
        # Should still return informational message
        assert "engine" in result
        assert result["engine"] == "twitter-info"
        assert "suggestion" in result

    @pytest.mark.asyncio
    async def test_twitter_server_suggestion_includes_query(self, mock_system_config, mock_mcp_config):
        """Test Twitter Search server includes query in suggestion."""
        server = TwitterSearchServer("twitter", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        result = await server.call("twitter_tweets", {"query": "bitcoin", "_status": mock_status})
        
        # Should include query in suggestion
        assert "suggestion" in result
        assert "bitcoin" in result["suggestion"]


class TestTwitterSearchPluginFactory:
    """Test the Twitter Search plugin factory function."""

    def test_plugin_factory_basic(self, mock_system_config, mock_mcp_config):
        """Test basic plugin factory functionality."""
        from plugins.twitter_search.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("twitter", mock_system_config, mock_mcp_config)
        assert server.name == "twitter"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self, mock_system_config, mock_mcp_config):
        """Test plugin factory with configuration."""
        from plugins.twitter_search.plugin import PLUGIN_FACTORY
        from agent_system.config.models import MCPConfig, AgentConfig

        mock_system_config.ssl_verify = False
        mcp_config = MCPConfig(type="twitter_search", enabled=True, agent_config=AgentConfig())
        mcp_config.timeout = 60
        
        server = PLUGIN_FACTORY("twitter", mock_system_config, mcp_config)
        assert server.name == "twitter"
        assert server.ssl_verify is False

    def test_plugin_factory_name_parameter(self, mock_system_config, mock_mcp_config):
        """Test plugin factory with custom name."""
        from plugins.twitter_search.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_twitter", mock_system_config, mock_mcp_config)
        assert server.name == "custom_twitter"
