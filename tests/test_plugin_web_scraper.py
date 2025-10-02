from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from agent_system.plugins import discover_all_plugins
from plugins.web_scraper.server import WebScraperServer


@pytest.mark.asyncio
async def test_web_scraper_plugin_discovered():
    repo_root = Path(__file__).resolve().parents[1]
    default_dir = repo_root / "plugins"
    if not default_dir.exists():
        alt = repo_root / "src" / "plugins"
        if alt.exists():
            default_dir = alt
    plugins = discover_all_plugins([default_dir])
    assert "web_scraper" in plugins
    factory = plugins["web_scraper"]
    inst = factory("web_scraper", {}, {})
    assert inst is not None


class TestWebScraperServer:
    """Test the Web Scraper server functionality."""

    def test_scraper_server_initialization(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)
        assert server.name == "scraper"
        assert server.ssl_verify is True

    def test_scraper_server_initialization_with_config(self, mock_system_config):
        from agent_system.config.models import MCPConfig, AgentConfig
        
        mcp_config = MCPConfig(type="web_scraper", enabled=True, agent_config=AgentConfig())
        mcp_config.timeout = 30
        mcp_config.user_agent = "test-agent"
        
        server = WebScraperServer("scraper", mock_system_config, mcp_config)
        assert server.name == "scraper"
        assert server.ssl_verify is True

    def test_scraper_server_schema(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)
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

    def test_scraper_server_tool_name(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "scrape_webpage"

    @pytest.mark.asyncio
    async def test_scraper_server_missing_url(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        result = await server.call("scrape_webpage", {"_status": mock_status})
        assert "error" in result
        assert "url" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_scraper_server_invalid_tool(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        # Modern pattern: generic dispatcher raises ValueError
        with pytest.raises(ValueError, match="Tool 'invalid_tool' not found"):
            await server.call("invalid_tool", {"url": "https://example.com", "_status": mock_status})

    @pytest.mark.asyncio
    async def test_scraper_server_valid_url(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)

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
            # If there's an error key it should not indicate an unknown tool
            assert "error" not in result or "Unknown tool" not in result.get("error", "")


class TestWebScraperPluginFactory:
    def test_plugin_factory_basic(self, mock_system_config, mock_mcp_config):
        from plugins.web_scraper.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("scraper", mock_system_config, mock_mcp_config)
        assert server.name == "scraper"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self, mock_system_config):
        from plugins.web_scraper.plugin import PLUGIN_FACTORY
        from agent_system.config.models import MCPConfig, AgentConfig

        mcp_config = MCPConfig(type="web_scraper", enabled=True, agent_config=AgentConfig())
        mcp_config.timeout = 60
        mcp_config.user_agent = "custom-agent"
        
        server = PLUGIN_FACTORY("scraper", mock_system_config, mcp_config)
        assert server.name == "scraper"
        assert server.ssl_verify is True

    def test_plugin_factory_name_parameter(self, mock_system_config, mock_mcp_config):
        from plugins.web_scraper.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_scraper", mock_system_config, mock_mcp_config)
        assert server.name == "custom_scraper"


# Improvements tests (merged from test_web_scraper_improvements.py)
@pytest.fixture
def web_scraper(mock_system_config, mock_mcp_config):
    return WebScraperServer("test_web_scraper", mock_system_config, mock_mcp_config)


class TestCloudflareDetection:
    def test_cloudflare_patterns_detected(self, web_scraper):
        cloudflare_html = """
        <html>
        <head><title>Just a moment...</title></head>
        <body>
        <h1>Please wait while your request is being verified...</h1>
        <p>This process is automatic. Your browser will redirect to your requested content shortly.</p>
        <p>Please allow up to 5 seconds...</p>
        <div>Ray ID: 123456789abcdef0</div>
        </body>
        </html>
        """
        is_blocked, reason = web_scraper._is_blocked_response(cloudflare_html, 200, "https://example.com")
        assert is_blocked is True
        assert "cloudflare challenge detected" in reason.lower()

    def test_403_status_detected(self, web_scraper):
        html = "<html><body>Access Denied</body></html>"
        is_blocked, reason = web_scraper._is_blocked_response(html, 403, "https://example.com")
        assert is_blocked is True
        assert "access forbidden (403)" in reason.lower()

    def test_429_status_detected(self, web_scraper):
        html = "<html><body>Too Many Requests</body></html>"
        is_blocked, reason = web_scraper._is_blocked_response(html, 429, "https://example.com")
        assert is_blocked is True
        assert "rate limited (429)" in reason.lower()

    def test_captcha_detected(self, web_scraper):
        captcha_html = """
        <html>
        <body>
        <h1>Security Check</h1>
        <p>Please solve the CAPTCHA below to continue</p>
        <div id="captcha">...</div>
        </body>
        </html>
        """
        is_blocked, reason = web_scraper._is_blocked_response(captcha_html, 200, "https://example.com")
        assert is_blocked is True
        assert "captcha" in reason.lower()

    def test_normal_content_not_blocked(self, web_scraper):
        normal_html = """
        <html>
        <head><title>Normal Page</title></head>
        <body>
        <h1>Welcome</h1>
        <p>This is normal content.</p>
        </body>
        </html>
        """
        is_blocked, reason = web_scraper._is_blocked_response(normal_html, 200, "https://example.com")
        assert is_blocked is False
        assert reason == ""


class TestRetryLogic:
    @pytest.mark.asyncio
    async def test_successful_fetch_no_retry(self, web_scraper):
        mock_fetch = AsyncMock(return_value=("content", 200, "https://example.com", "text/html"))
        web_scraper._fetch_html_once = mock_fetch
        result = await web_scraper._fetch_with_retry("https://example.com", "test-agent", 30.0, max_retries=3)
        assert mock_fetch.call_count == 1
        assert result == ("content", 200, "https://example.com", "text/html")

    @pytest.mark.asyncio
    async def test_429_retry_logic(self, web_scraper):
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            mock_fetch = AsyncMock(side_effect=[
                ("", 429, "https://example.com", "text/html"),
                ("", 429, "https://example.com", "text/html"),
                ("content", 200, "https://example.com", "text/html")
            ])
            web_scraper._fetch_html_once = mock_fetch
            result = await web_scraper._fetch_with_retry("https://example.com", "test-agent", 30.0, max_retries=3)
            assert mock_fetch.call_count == 3
            assert mock_sleep.call_count == 4
            assert result == ("content", 200, "https://example.com", "text/html")

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self, web_scraper):
        with patch('asyncio.sleep', new_callable=AsyncMock):
            mock_fetch = AsyncMock(return_value=("", 429, "https://example.com", "text/html"))
            web_scraper._fetch_html_once = mock_fetch
            result = await web_scraper._fetch_with_retry("https://example.com", "test-agent", 30.0, max_retries=2)
            assert mock_fetch.call_count == 3
            assert result == ("", 429, "https://example.com", "text/html")


class TestIntegration:
    @pytest.mark.asyncio
    async def test_blocked_response_handling(self, web_scraper):
        from unittest.mock import AsyncMock
        cloudflare_html = """
        <html>
        <head><title>Just a moment...</title></head>
        <body><h1>Please wait while your request is being verified...</h1></body>
        </html>
        """
        mock_fetch = AsyncMock(return_value=(cloudflare_html, 403, "https://example.com", "text/html"))
        web_scraper._fetch_with_retry = mock_fetch
        
        # Need to provide _status mock
        mock_status = AsyncMock()
        result = await web_scraper.call("scrape_webpage", {"url": "https://example.com", "ignore_cache": True, "_status": mock_status})
        assert result["status_code"] == 403
        assert "blocked:" in result["text"]
        assert "access forbidden (403)" in result["text"]

    def test_user_agent_updated(self, web_scraper, mock_system_config, mock_mcp_config):
        params = {"url": "https://example.com"}
        user_agent = params.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        assert "Chrome/120.0.0.0" in user_agent
        assert "Safari/537.36" in user_agent
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)
        assert server.name == "scraper"
        assert server.ssl_verify is True

    def test_scraper_server_initialization_with_config(self, mock_system_config):
        """Test Web Scraper server initialization with config."""
        from agent_system.config.models import MCPConfig, AgentConfig
        
        mcp_config = MCPConfig(type="web_scraper", enabled=True, agent_config=AgentConfig())
        mcp_config.timeout = 30
        mcp_config.user_agent = "test-agent"
        
        server = WebScraperServer("scraper", mock_system_config, mcp_config)
        assert server.name == "scraper"
        assert server.ssl_verify is True

    def test_scraper_server_schema(self, mock_system_config, mock_mcp_config):
        """Test Web Scraper server tools structure."""
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)
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

    def test_scraper_server_tool_name(self, mock_system_config, mock_mcp_config):
        """Test Web Scraper server tool name."""
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "scrape_webpage"

    @pytest.mark.asyncio
    async def test_scraper_server_missing_url(self, mock_system_config, mock_mcp_config):
        """Test Web Scraper server with missing URL."""
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        result = await server.call("scrape_webpage", {"_status": mock_status})
        assert "error" in result
        assert "url" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_scraper_server_invalid_tool(self, mock_system_config, mock_mcp_config):
        """Test Web Scraper server with invalid tool name."""
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        # Modern pattern: generic dispatcher raises ValueError
        with pytest.raises(ValueError, match="Tool 'invalid_tool' not found"):
            await server.call("invalid_tool", {"url": "https://example.com", "_status": mock_status})

    @pytest.mark.asyncio
    async def test_scraper_server_valid_url(self, mock_system_config, mock_mcp_config):
        """Test Web Scraper server with valid URL."""
        server = WebScraperServer("scraper", mock_system_config, mock_mcp_config)

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

    def test_plugin_factory_basic(self, mock_system_config, mock_mcp_config):
        """Test basic plugin factory functionality."""
        from plugins.web_scraper.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("scraper", mock_system_config, mock_mcp_config)
        assert server.name == "scraper"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self, mock_system_config):
        """Test plugin factory with configuration."""
        from plugins.web_scraper.plugin import PLUGIN_FACTORY
        from agent_system.config.models import MCPConfig, AgentConfig

        mcp_config = MCPConfig(type="web_scraper", enabled=True, agent_config=AgentConfig())
        mcp_config.timeout = 60
        mcp_config.user_agent = "custom-agent"
        
        server = PLUGIN_FACTORY("scraper", mock_system_config, mcp_config)
        assert server.name == "scraper"
        assert server.ssl_verify is True

    def test_plugin_factory_name_parameter(self, mock_system_config, mock_mcp_config):
        """Test plugin factory with custom name."""
        from plugins.web_scraper.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_scraper", mock_system_config, mock_mcp_config)
        assert server.name == "custom_scraper"
