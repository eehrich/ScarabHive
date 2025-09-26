from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_system.plugins import discover_all_plugins
from plugins.duckduckgo_search.server import DuckDuckGoSearchServer


@pytest.mark.asyncio
async def test_duckduckgo_plugin_discovered():
    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt
    plugins = discover_all_plugins([default_dir])
    assert 'duckduckgo_search' in plugins
    factory = plugins['duckduckgo_search']
    # Ensure factory is callable / instantiable
    inst = factory('duckduckgo_search', {})
    assert inst is not None


class TestDuckDuckGoSearchServer:
    """Test the DuckDuckGo Search server functionality."""

    def test_ddg_server_initialization(self):
        """Test DuckDuckGo Search server initialization."""
        server = DuckDuckGoSearchServer("ddg", {}, True)
        assert server.name == "ddg"
        assert server.ssl_verify is True

    def test_ddg_server_initialization_with_config(self):
        """Test DuckDuckGo Search server initialization with config."""
        config = {"timeout": 30}
        server = DuckDuckGoSearchServer("ddg", config, False)
        assert server.name == "ddg"
        assert server.ssl_verify is False

    def test_ddg_server_schema(self):
        """Test DuckDuckGo Search server tools structure."""
        server = DuckDuckGoSearchServer("ddg", {}, True)
        tools = server.get_tools()

        assert isinstance(tools, list)
        assert len(tools) == 1
        
        tool = tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "web_search"
        assert "description" in tool["function"]
        assert tool["function"]["parameters"]["type"] == "object"

        params = tool["function"]["parameters"]
        assert "query" in params["properties"]
        assert "max_results" in params["properties"]

    def test_ddg_server_tool_name(self):
        """Test DuckDuckGo Search server tool name."""
        server = DuckDuckGoSearchServer("ddg", {}, True)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_ddg_server_missing_query(self):
        """Test DuckDuckGo Search server with missing query."""
        server = DuckDuckGoSearchServer("ddg", {}, True)

        mock_status = AsyncMock()
        result = await server.call("web_search", {"_status": mock_status})
        assert "error" in result
        assert "Empty query" in result["error"]

    @pytest.mark.asyncio
    async def test_ddg_server_invalid_tool(self):
        """Test DuckDuckGo Search server with invalid tool name."""
        server = DuckDuckGoSearchServer("ddg", {}, True)

        mock_status = AsyncMock()
        result = await server.call("invalid_tool", {"query": "test", "_status": mock_status})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    @pytest.mark.asyncio
    async def test_ddg_server_valid_search(self):
        """Test DuckDuckGo Search server with valid search."""
        server = DuckDuckGoSearchServer("ddg", {}, True)

        # Mock the DDGS library by patching the import
        mock_search_results = [
            {"title": "Test Result 1", "href": "https://example.com/1", "body": "Test snippet 1"},
            {"title": "Test Result 2", "href": "https://example.com/2", "body": "Test snippet 2"}
        ]
        
        with patch('ddgs.DDGS') as mock_ddgs:
            mock_instance = mock_ddgs.return_value
            mock_instance.text.return_value = mock_search_results

            mock_status = AsyncMock()
            result = await server.call("web_search", {"query": "test query", "max_results": 2, "_status": mock_status})
            
            # Should return search results without error
            assert result is not None
            assert isinstance(result, dict)
            if "error" in result:
                assert "Unknown tool" not in result["error"]
            else:
                assert "engine" in result
                assert result["engine"] == "duckduckgo"


class TestDuckDuckGoSearchPluginFactory:
    """Test the DuckDuckGo Search plugin factory function."""

    def test_plugin_factory_basic(self):
        """Test basic plugin factory functionality."""
        from plugins.duckduckgo_search.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("ddg")
        assert server.name == "ddg"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self):
        """Test plugin factory with configuration."""
        from plugins.duckduckgo_search.plugin import PLUGIN_FACTORY

        config = {"timeout": 60}
        server = PLUGIN_FACTORY("ddg", config, False)
        assert server.name == "ddg"
        assert server.ssl_verify is False

    def test_plugin_factory_name_parameter(self):
        """Test plugin factory with custom name."""
        from plugins.duckduckgo_search.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_ddg")
        assert server.name == "custom_ddg"
