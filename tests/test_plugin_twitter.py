from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_system.plugins import discover_all_plugins
from plugins.twitter_search.server import TwitterSearchServer


@pytest.mark.asyncio
async def test_twitter_plugin_discovered():
    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt
    plugins = discover_all_plugins([default_dir])
    assert 'twitter_search' in plugins
    factory = plugins['twitter_search']
    inst = factory('twitter_search', {})
    assert inst is not None


class TestTwitterSearchServer:
    """Test the Twitter Search server functionality."""

    def test_twitter_server_initialization(self):
        """Test Twitter Search server initialization."""
        server = TwitterSearchServer("twitter", {}, True)
        assert server.name == "twitter"
        assert server.ssl_verify is True

    def test_twitter_server_initialization_with_config(self):
        """Test Twitter Search server initialization with config."""
        config = {"timeout": 30}
        server = TwitterSearchServer("twitter", config, False)
        assert server.name == "twitter"
        assert server.ssl_verify is False

    def test_twitter_server_schema(self):
        """Test Twitter Search server tools structure."""
        server = TwitterSearchServer("twitter", {}, True)
        tools = server.get_tools()

        assert isinstance(tools, list)
        assert len(tools) == 1
        
        tool = tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "search_tweets"
        assert "description" in tool["function"]
        assert tool["function"]["parameters"]["type"] == "object"

        params = tool["function"]["parameters"]
        assert "query" in params["properties"]

    def test_twitter_server_tool_name(self):
        """Test Twitter Search server tool name."""
        server = TwitterSearchServer("twitter", {}, True)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "search_tweets"

    @pytest.mark.asyncio
    async def test_twitter_server_search_returns_info(self):
        """Test Twitter Search server returns informational message."""
        server = TwitterSearchServer("twitter", {}, True)

        mock_status = AsyncMock()
        result = await server.call("search_tweets", {"query": "test", "_status": mock_status})
        
        # Should return informational message about Twitter API restrictions
        assert "engine" in result
        assert result["engine"] == "twitter-info"
        assert "message" in result
        assert "alternatives" in result
        assert isinstance(result["alternatives"], list)

    @pytest.mark.asyncio
    async def test_twitter_server_invalid_tool(self):
        """Test Twitter Search server with invalid tool name."""
        server = TwitterSearchServer("twitter", {}, True)

        mock_status = AsyncMock()
        result = await server.call("invalid_tool", {"query": "test", "_status": mock_status})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    @pytest.mark.asyncio
    async def test_twitter_server_empty_query(self):
        """Test Twitter Search server with empty query."""
        server = TwitterSearchServer("twitter", {}, True)

        mock_status = AsyncMock()
        result = await server.call("search_tweets", {"query": "", "_status": mock_status})
        
        # Should still return informational message
        assert "engine" in result
        assert result["engine"] == "twitter-info"
        assert "suggestion" in result

    @pytest.mark.asyncio
    async def test_twitter_server_suggestion_includes_query(self):
        """Test Twitter Search server includes query in suggestion."""
        server = TwitterSearchServer("twitter", {}, True)

        mock_status = AsyncMock()
        result = await server.call("search_tweets", {"query": "bitcoin", "_status": mock_status})
        
        # Should include query in suggestion
        assert "suggestion" in result
        assert "bitcoin" in result["suggestion"]


class TestTwitterSearchPluginFactory:
    """Test the Twitter Search plugin factory function."""

    def test_plugin_factory_basic(self):
        """Test basic plugin factory functionality."""
        from plugins.twitter_search.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("twitter")
        assert server.name == "twitter"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self):
        """Test plugin factory with configuration."""
        from plugins.twitter_search.plugin import PLUGIN_FACTORY

        config = {"timeout": 60}
        server = PLUGIN_FACTORY("twitter", config, False)
        assert server.name == "twitter"
        assert server.ssl_verify is False

    def test_plugin_factory_name_parameter(self):
        """Test plugin factory with custom name."""
        from plugins.twitter_search.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_twitter")
        assert server.name == "custom_twitter"
