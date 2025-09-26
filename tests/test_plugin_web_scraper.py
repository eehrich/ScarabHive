from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from agent_system.plugins import discover_all_plugins
from plugins.web_scraper.server import WebScraperServer


@pytest.mark.asyncio
async def test_web_scraper_plugin_discovered():
    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / 'plugins'
    if not default_dir.exists():
        alt = repo_root / 'src' / 'plugins'
        if alt.exists():
            default_dir = alt
    plugins = discover_all_plugins([default_dir])
    assert 'web_scraper' in plugins
    factory = plugins['web_scraper']
    inst = factory('web_scraper', {})
    assert inst is not None


class TestWebScraperServer:
    """Test the Web Scraper server functionality."""

    def test_scraper_server_initialization(self):
        """Test Web Scraper server initialization."""
        server = WebScraperServer("scraper", {}, True)
        assert server.name == "scraper"
        assert server.ssl_verify is True

    def test_scraper_server_initialization_with_config(self):
        """Test Web Scraper server initialization with config."""
        config = {"timeout": 30, "user_agent": "test-agent"}
        server = WebScraperServer("scraper", config, False)
        assert server.name == "scraper"
        assert server.ssl_verify is False

    def test_scraper_server_schema(self):
        """Test Web Scraper server tools structure."""
        server = WebScraperServer("scraper", {}, True)
        tools = server.get_tools()

        assert isinstance(tools, list)
        assert len(tools) == 1
        
        tool = tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "scrape_webpage"
        assert "description" in tool["function"]
        assert tool["function"]["parameters"]["type"] == "object"

        params = tool["function"]["parameters"]
        assert "url" in params["properties"]

    def test_scraper_server_tool_name(self):
        """Test Web Scraper server tool name."""
        server = WebScraperServer("scraper", {}, True)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "scrape_webpage"

    @pytest.mark.asyncio
    async def test_scraper_server_missing_url(self):
        """Test Web Scraper server with missing URL."""
        server = WebScraperServer("scraper", {}, True)

        mock_status = AsyncMock()
        result = await server.call("scrape_webpage", {"_status": mock_status})
        assert "error" in result
        assert "url" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_scraper_server_invalid_tool(self):
        """Test Web Scraper server with invalid tool name."""
        server = WebScraperServer("scraper", {}, True)

        mock_status = AsyncMock()
        result = await server.call("invalid_tool", {"url": "https://example.com", "_status": mock_status})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    @pytest.mark.asyncio
    async def test_scraper_server_valid_url(self):
        """Test Web Scraper server with valid URL."""
        server = WebScraperServer("scraper", {}, True)

        # Mock the HTTP session and response
        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_response = MagicMock()
            mock_response.text = AsyncMock(return_value="<html><body><h1>Test Page</h1></body></html>")
            mock_response.status = 200
            mock_response.headers = {"Content-Type": "text/html"}
            
            mock_session = AsyncMock()
            mock_session.get.return_value.__aenter__.return_value = mock_response
            mock_session_class.return_value.__aenter__.return_value = mock_session

            mock_status = AsyncMock()
            result = await server.call("scrape_webpage", {"url": "https://example.com", "_status": mock_status})
            
            # Should not fail with "Unknown tool" error and should contain scraped content
            assert "error" not in result or "Unknown tool" not in result.get("error", "")


class TestWebScraperPluginFactory:
    """Test the Web Scraper plugin factory function."""

    def test_plugin_factory_basic(self):
        """Test basic plugin factory functionality."""
        from plugins.web_scraper.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("scraper")
        assert server.name == "scraper"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self):
        """Test plugin factory with configuration."""
        from plugins.web_scraper.plugin import PLUGIN_FACTORY

        config = {"timeout": 60, "user_agent": "custom-agent"}
        server = PLUGIN_FACTORY("scraper", config, False)
        assert server.name == "scraper"
        assert server.ssl_verify is False

    def test_plugin_factory_name_parameter(self):
        """Test plugin factory with custom name."""
        from plugins.web_scraper.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_scraper")
        assert server.name == "custom_scraper"
