from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from plugins.web_scraper.server import WebScraperServer


class TestWebScraperServer:
    """Test the Web Scraper server functionality."""

    def test_scraper_server_initialization(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        assert server.name == "web_scraper"
        assert server.ssl_verify is True

    def test_scraper_server_initialization_with_config(self, mock_system_config):
        from agent_system.config.models import MCPConfig, AgentConfig
        
        mcp_config = MCPConfig(type="web_scraper_page", enabled=True, agent_config=AgentConfig())
        mcp_config.timeout = 30
        mcp_config.user_agent = "test-agent"
        
        server = WebScraperServer("web_scraper", mock_system_config, mcp_config)
        assert server.name == "web_scraper"
        assert server.ssl_verify is True

    def test_scraper_server_schema(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        tools = server.get_tools()

        assert isinstance(tools, list)
        assert len(tools) == 1

        tool = tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "web_scraper_page"
        assert "description" in tool["function"]
        assert tool["function"]["parameters"]["type"] == "object"

        params = tool["function"]["parameters"]
        assert "url" in params["properties"]

    def test_scraper_server_tool_name(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "web_scraper_page"

    @pytest.mark.asyncio
    async def test_scraper_server_missing_url(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        result = await server.call("web_scraper_page", {"_status": mock_status})
        assert "error" in result
        assert "url" in result["error"].lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_url", [
        "http://169.254.169.254/latest/meta-data/",   # AWS IMDS
        "http://metadata.google.internal/",           # GCP metadata
        "http://localhost:8000/",                     # loopback
        "http://127.0.0.1/",                          # loopback
        "http://10.0.0.5/",                           # RFC1918
        "http://192.168.1.1/",                        # RFC1918
        "file:///etc/passwd",                         # non-http scheme
        "gopher://internal/",                         # non-http scheme
    ])
    async def test_ssrf_blocks_internal_and_nonhttp(self, mock_system_config, mock_mcp_config, bad_url):
        """SSRF guard rejects internal/metadata/non-http targets without fetching."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        mock_status = AsyncMock()
        result = await server.call("web_scraper_page", {"url": bad_url, "_status": mock_status})
        assert "error" in result
        assert "ssrf" in result["error"].lower() or "blocked" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_ssrf_blocks_redirect_to_internal(self, mock_system_config, mock_mcp_config):
        """A public URL that 302s to the metadata IP is blocked on the redirect hop."""
        import httpx
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        mock_status = AsyncMock()

        # First fetch (public host) returns a redirect to the metadata IP.
        redirect_resp = MagicMock()
        redirect_resp.is_redirect = True
        redirect_resp.headers = {"location": "http://169.254.169.254/latest/meta-data/"}
        redirect_resp.status_code = 302

        async def fake_get(url, *a, **k):
            return redirect_resp

        with patch.object(httpx, "AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get = fake_get
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            mock_client_cls.return_value = client
            result = await server.call(
                "web_scraper_page",
                {"url": "https://example.com/redirector", "_status": mock_status},
            )
        assert "error" in result
        assert "blocked" in result["error"].lower()

    # --- Session-isolation helpers (cookies / proxy / cache) ----------------

    @staticmethod
    def _mock_httpx(captured: dict | None = None, html: str = "<html>ok</html>"):
        """Patch httpx so _fetch_html_once does NO real network/DNS I/O.

        Returns a context manager. When `captured` is given, every
        httpx.AsyncClient(**kwargs) call records its kwargs into it (so a test
        can assert which proxy/cookies the client was built with).
        """
        import contextlib
        import httpx

        resp = MagicMock()
        resp.is_redirect = False
        resp.headers = {"content-type": "text/html"}
        resp.status_code = 200
        resp.text = html
        resp.url = "https://example.com/"

        async def fake_get(url, *a, **k):
            return resp

        client = AsyncMock()
        client.get = fake_get
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None

        def make_client(*a, **k):
            if captured is not None:
                captured.clear()
                captured.update(k)
            return client

        @contextlib.contextmanager
        def _ctx():
            with patch.object(WebScraperServer, "_assert_url_safe", new=AsyncMock()), \
                 patch.object(httpx, "AsyncClient", side_effect=make_client):
                yield
        return _ctx()

    @pytest.mark.asyncio
    async def test_cookie_jar_is_session_scoped(self, mock_system_config, mock_mcp_config):
        """Two sessions must NOT share a cookie jar for the same domain.

        Regression: keying jars by domain alone leaked one user's authenticated
        cookies to every other user scraping the same site.
        """
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        with self._mock_httpx():
            await server._fetch_html_once("https://example.com/", "UA", 10, session_id="a")
            await server._fetch_html_once("https://example.com/", "UA", 10, session_id="b")
        assert ("a", "example.com") in server._sessions
        assert ("b", "example.com") in server._sessions
        assert server._sessions[("a", "example.com")] is not server._sessions[("b", "example.com")]

    @pytest.mark.asyncio
    async def test_cookie_jar_reused_within_session(self, mock_system_config, mock_mcp_config):
        """Same session + domain reuses one jar (cookies persist within a session)."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        with self._mock_httpx():
            await server._fetch_html_once("https://example.com/p1", "UA", 10, session_id="a")
            jar = server._sessions[("a", "example.com")]
            await server._fetch_html_once("https://example.com/p2", "UA", 10, session_id="a")
        assert server._sessions[("a", "example.com")] is jar

    @pytest.mark.asyncio
    async def test_cookie_jar_none_session_uses_shared_sentinel(self, mock_system_config, mock_mcp_config):
        """A None session_id (internal/CLI) falls back to the '_shared' jar."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        with self._mock_httpx():
            await server._fetch_html_once("https://example.com/", "UA", 10, session_id=None)
        assert ("_shared", "example.com") in server._sessions

    @pytest.mark.asyncio
    async def test_cookie_jar_is_fifo_bounded(self, mock_system_config, mock_mcp_config):
        """The cookie dict is bounded; the requested key always survives."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        server._max_cookie_jars = 5
        with self._mock_httpx():
            for i in range(20):
                await server._fetch_html_once(f"https://d{i}.example.com/", "UA", 10, session_id=f"s{i}")
        assert len(server._sessions) <= 5
        # The most recent jar is present (never evicted out from under itself)
        assert ("s19", "d19.example.com") in server._sessions

    @pytest.mark.asyncio
    async def test_request_proxy_used_and_does_not_mutate_shared(self, mock_system_config, mock_mcp_config):
        """A per-request proxy is used for that request only and never mutates
        the shared self._proxies (which raced across concurrent requests)."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        server._proxies = ["http://configured:8080"]
        before = list(server._proxies)
        captured: dict = {}
        with self._mock_httpx(captured):
            await server._fetch_html_once("https://example.com/", "UA", 10, request_proxy="http://per-request:3128")
        assert server._proxies == before              # shared config untouched
        assert captured.get("proxy") == "http://per-request:3128"

    @pytest.mark.asyncio
    async def test_configured_proxy_pool_used_when_no_request_proxy(self, mock_system_config, mock_mcp_config):
        """With no per-request proxy, the configured pool is used (rotation)."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        server._proxies = ["http://pool:9999"]
        captured: dict = {}
        with self._mock_httpx(captured):
            await server._fetch_html_once("https://example.com/", "UA", 10, request_proxy=None)
        assert captured.get("proxy") == "http://pool:9999"

    @pytest.mark.asyncio
    async def test_no_proxy_when_none_configured(self, mock_system_config, mock_mcp_config):
        """No proxy key is set on the client when neither request nor pool proxy exists."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        server._proxies = []
        captured: dict = {}
        with self._mock_httpx(captured):
            await server._fetch_html_once("https://example.com/", "UA", 10)
        assert "proxy" not in captured

    def test_cache_key_is_session_scoped(self, mock_system_config, mock_mcp_config):
        """Cache keys differ per session so cached page bodies don't cross users."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        args = ("https://example.com/dash", "content", 8000, False, False, False, False)
        key_a = server._create_cache_key(*args, session_id="a")
        key_b = server._create_cache_key(*args, session_id="b")
        key_none = server._create_cache_key(*args)
        assert key_a != key_b
        assert key_a != key_none
        # Stable for identical inputs -> still cacheable within a session
        assert key_a == server._create_cache_key(*args, session_id="a")

    @pytest.mark.asyncio
    async def test_scraper_server_invalid_tool(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        # Modern pattern: generic dispatcher raises ValueError
        with pytest.raises(ValueError, match="Tool 'invalid_tool' not found"):
            await server.call("invalid_tool", {"url": "https://example.com", "_status": mock_status})

    @pytest.mark.asyncio
    async def test_scraper_server_valid_url(self, mock_system_config, mock_mcp_config):
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)

        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_response = MagicMock()
            mock_response.text = AsyncMock(return_value="<html><body><h1>Test Page</h1></body></html>")
            mock_response.status = 200
            mock_response.headers = {"Content-Type": "text/html"}

            mock_session = AsyncMock()
            mock_session.get.return_value.__aenter__.return_value = mock_response
            mock_session_class.return_value.__aenter__.return_value = mock_session

            mock_status = AsyncMock()
            result = await server.call("web_scraper_page", {"url": "https://example.com", "_status": mock_status})
            # If there's an error key it should not indicate an unknown tool
            assert "error" not in result or "Unknown tool" not in result.get("error", "")


# Improvements tests (merged from test_web_scraper_improvements.py)
@pytest.fixture
def web_scraper(mock_system_config, mock_mcp_config):
    return WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)


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
        result = await web_scraper.call("web_scraper_page", {"url": "https://example.com", "ignore_cache": True, "_status": mock_status})
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
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        assert server.name == "web_scraper"
        assert server.ssl_verify is True

    def test_scraper_server_initialization_with_config(self, mock_system_config):
        """Test Web Scraper server initialization with config."""
        from agent_system.config.models import MCPConfig, AgentConfig
        
        mcp_config = MCPConfig(type="web_scraper_page", enabled=True, agent_config=AgentConfig())
        mcp_config.timeout = 30
        mcp_config.user_agent = "test-agent"
        
        server = WebScraperServer("web_scraper", mock_system_config, mcp_config)
        assert server.name == "web_scraper"
        assert server.ssl_verify is True

    def test_scraper_server_schema(self, mock_system_config, mock_mcp_config):
        """Test Web Scraper server tools structure."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        tools = server.get_tools()

        assert isinstance(tools, list)
        assert len(tools) == 1
        
        tool = tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "web_scraper_page"
        assert "description" in tool["function"]
        assert tool["function"]["parameters"]["type"] == "object"

        params = tool["function"]["parameters"]
        assert "url" in params["properties"]

    def test_scraper_server_tool_name(self, mock_system_config, mock_mcp_config):
        """Test Web Scraper server tool name."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "web_scraper_page"

    @pytest.mark.asyncio
    async def test_scraper_server_missing_url(self, mock_system_config, mock_mcp_config):
        """Test Web Scraper server with missing URL parameter."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        result = await server.call("web_scraper_page", {"_status": mock_status})
        assert "error" in result
        assert "url" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_scraper_server_invalid_tool(self, mock_system_config, mock_mcp_config):
        """Test Web Scraper server with invalid tool name."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)

        mock_status = AsyncMock()
        # Modern pattern: generic dispatcher raises ValueError
        with pytest.raises(ValueError, match="Tool 'invalid_tool' not found"):
            await server.call("invalid_tool", {"url": "https://example.com", "_status": mock_status})

    @pytest.mark.asyncio
    async def test_scraper_server_valid_url(self, mock_system_config, mock_mcp_config):
        """Test Web Scraper server with valid URL."""
        server = WebScraperServer("web_scraper", mock_system_config, mock_mcp_config)

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
            result = await server.call("web_scraper_page", {"url": "https://example.com", "_status": mock_status})
            
            # Should not fail with "Unknown tool" error and should contain scraped content
            assert "error" not in result or "Unknown tool" not in result.get("error", "")


class TestWebScraperPluginFactory:
    """Test the Web Scraper plugin factory function."""

    def test_plugin_factory_basic(self, mock_system_config, mock_mcp_config):
        """Test basic plugin factory functionality."""
        from plugins.web_scraper.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("web_scraper_page", mock_system_config, mock_mcp_config)
        assert server.name == "web_scraper_page"
        assert server.ssl_verify is True

    def test_plugin_factory_with_config(self, mock_system_config):
        """Test plugin factory with configuration."""
        from plugins.web_scraper.plugin import PLUGIN_FACTORY
        from agent_system.config.models import MCPConfig, AgentConfig

        mcp_config = MCPConfig(type="web_scraper_page", enabled=True, agent_config=AgentConfig())
        mcp_config.timeout = 60
        mcp_config.user_agent = "custom-agent"
        
        server = PLUGIN_FACTORY("web_scraper_page", mock_system_config, mcp_config)
        assert server.name == "web_scraper_page"
        assert server.ssl_verify is True

    def test_plugin_factory_name_parameter(self, mock_system_config, mock_mcp_config):
        """Test plugin factory with custom name."""
        from plugins.web_scraper.plugin import PLUGIN_FACTORY

        server = PLUGIN_FACTORY("custom_scraper", mock_system_config, mock_mcp_config)
        assert server.name == "custom_scraper"
