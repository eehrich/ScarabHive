"""Tests for the Tavily search plugin."""

from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
from tavily.errors import BadRequestError, ForbiddenError, InvalidAPIKeyError, UsageLimitExceededError

import plugins.tavily_search.server as tavily_server
from agent_system.plugins.cache import PluginCache
from plugins.tavily_search.server import TavilySearchServer


@pytest.fixture(autouse=True)
def _cache_in_tmp(tmp_path, monkeypatch):
    """Every server here caches under tmp_path: the default is the real
    data/cache/tavily_search, where an entry from a real run would answer
    in place of the stubbed client."""
    monkeypatch.setattr(tavily_server, "PluginCache",
                        lambda **kw: PluginCache(cache_dir=tmp_path, **kw))


class TestTavilySearchServerInit:
    """Test TavilySearchServer initialization."""

    def test_server_initialization(self, mock_system_config, mock_server_config):
        """Test basic server initialization."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test-api-key'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            assert server.name == "tavily"
            assert server.api_key == "test-api-key"

    def test_server_initialization_with_config_api_key(self, mock_system_config):
        """Test server initialization with API key from config."""
        from agent_system.config.models import ToolServerConfig, AgentConfig
        server_config = ToolServerConfig(type="tavily_search", enabled=True, agent_config=AgentConfig())
        server_config.api_key = "config-api-key"
        
        with patch.dict('os.environ', {}, clear=True):
            server = TavilySearchServer("tavily", mock_system_config, server_config)
            assert server.api_key == "config-api-key"

    def test_server_initialization_without_api_key(self, mock_system_config, mock_server_config):
        """Test server initialization without API key logs warning."""
        with patch.dict('os.environ', {}, clear=True):
            with patch('plugins.tavily_search.server.logger') as mock_logger:
                server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
                assert server.api_key == ""
                mock_logger.warning.assert_called()

    def test_server_initialization_with_cache_config(self, mock_system_config):
        """Test server initialization with cache configuration."""
        from agent_system.config.models import ToolServerConfig, AgentConfig
        server_config = ToolServerConfig(type="tavily_search", enabled=True, agent_config=AgentConfig())
        server_config.cache_ttl = 600
        server_config.cache_enabled = False
        
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, server_config)
            assert server.cache_enabled is False


class TestTavilySearchServerSchema:
    """Test TavilySearchServer schema/tools."""

    def test_server_provides_tools(self, mock_system_config, mock_server_config):
        """Test server provides search and extract tools."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            tools = server.get_tools()
            
            assert isinstance(tools, list)
            assert len(tools) == 2
            
            tool_names = [t["function"]["name"] for t in tools]
            assert "tavily_web_search" in tool_names
            assert "tavily_extract" in tool_names

    def test_web_search_tool_schema(self, mock_system_config, mock_server_config):
        """Test web_search tool schema structure."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            tools = server.get_tools()
            
            search_tool = next(t for t in tools if "web_search" in t["function"]["name"])
            params = search_tool["function"]["parameters"]["properties"]
            
            assert "query" in params
            assert "max_results" in params
            assert "search_depth" in params
            assert "topic" in params
            assert "time_range" in params
            assert "include_domains" in params
            assert "exclude_domains" in params

    def test_extract_tool_schema(self, mock_system_config, mock_server_config):
        """Test extract tool schema structure."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            tools = server.get_tools()
            
            extract_tool = next(t for t in tools if "extract" in t["function"]["name"])
            params = extract_tool["function"]["parameters"]["properties"]
            
            assert "urls" in params
            assert "extract_depth" in params
            assert "format" in params
            assert "include_images" in params


class TestTavilyWebSearch:
    """Test TavilySearchServer web_search functionality."""

    @pytest.mark.asyncio
    async def test_web_search_empty_query(self, mock_system_config, mock_server_config):
        """Test web_search with empty query returns error."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            
            mock_status = AsyncMock()
            result = await server.call("web_search", {"query": "", "_status": mock_status})
            
            assert "error" in result
            assert "Empty query" in result["error"]

    @pytest.mark.asyncio
    async def test_web_search_no_api_key(self, mock_system_config, mock_server_config):
        """Test web_search without API key returns error."""
        with patch.dict('os.environ', {}, clear=True):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            
            mock_status = AsyncMock()
            result = await server.call("web_search", {"query": "test", "_status": mock_status})
            
            assert "error" in result

    @pytest.mark.asyncio
    async def test_web_search_success(self, mock_system_config, mock_server_config):
        """Test successful web search."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            server.cache_enabled = False  # Disable cache for this test
            
            mock_response = {
                "results": [
                    {"title": "Result 1", "url": "https://example.com/1", "content": "Content 1", "score": 0.9},
                    {"title": "Result 2", "url": "https://example.com/2", "content": "Content 2", "score": 0.8},
                ],
                "answer": "Test answer"
            }
            
            mock_client = AsyncMock()
            mock_client.search = AsyncMock(return_value=mock_response)
            
            async def mock_get_client():
                return mock_client
            
            with patch.object(server, '_get_client', mock_get_client):
                mock_status = AsyncMock()
                result = await server.call("web_search", {
                    "query": "test query",
                    "max_results": 5,
                    "include_answer": True,
                    "_status": mock_status
                })
                
                assert "error" not in result
                assert result["query"] == "test query"
                assert len(result["results"]) == 2
                assert result["results"][0]["title"] == "Result 1"
                assert result["answer"] == "Test answer"
                # The end line replaces the progress line: it names the query,
                # as the cached path does.
                assert mock_status.end.call_args[0][0] == "2 results -- test query"

    @pytest.mark.asyncio
    async def test_web_search_with_filters(self, mock_system_config, mock_server_config):
        """Test web search with domain filters."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            server.cache_enabled = False  # Disable cache for this test
            
            mock_response = {"results": []}
            mock_client = AsyncMock()
            mock_client.search = AsyncMock(return_value=mock_response)
            
            async def mock_get_client():
                return mock_client
            
            with patch.object(server, '_get_client', mock_get_client):
                mock_status = AsyncMock()
                await server.call("web_search", {
                    "query": "test",
                    "include_domains": ["example.com"],
                    "exclude_domains": ["spam.com"],
                    "time_range": "week",
                    "search_depth": "advanced",
                    "_status": mock_status
                })
                
                # Verify search was called with correct parameters
                mock_client.search.assert_called_once()
                call_kwargs = mock_client.search.call_args[1]
                assert call_kwargs["include_domains"] == ["example.com"]
                assert call_kwargs["exclude_domains"] == ["spam.com"]
                assert call_kwargs["time_range"] == "week"
                assert call_kwargs["search_depth"] == "advanced"

    @pytest.mark.asyncio
    async def test_web_search_cancellation(self, mock_system_config, mock_server_config):
        """Test web search respects cancellation token."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            
            mock_status = AsyncMock()
            mock_cancel = MagicMock()
            mock_cancel.is_cancelled = True
            
            result = await server.call("web_search", {
                "query": "test",
                "_status": mock_status,
                "_cancellation_token": mock_cancel
            })
            
            assert result.get("cancelled") is True

    @pytest.mark.asyncio
    async def test_web_search_cache_hit(self, mock_system_config, mock_server_config):
        """Test web search returns cached results."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            
            cached_data = {"query": "test", "results": [], "result_count": 0}
            
            with patch.object(server.cache, 'get', return_value=cached_data):
                mock_status = AsyncMock()
                result = await server.call("web_search", {"query": "test", "_status": mock_status})
                
                assert result == cached_data
                # Contract, not wording: the line that stays must name the
                # query and the count. "Retrieved from cache" said neither.
                message, kwargs = mock_status.end.call_args[0][0], mock_status.end.call_args[1]
                assert "test" in message and "0 results" in message, message
                assert kwargs["meta"]["cache_hit"] is True


class TestTavilyExtract:
    """Test TavilySearchServer extract functionality."""

    @pytest.mark.asyncio
    async def test_extract_no_urls(self, mock_system_config, mock_server_config):
        """Test extract with no URLs returns error."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            
            mock_status = AsyncMock()
            result = await server.call("extract", {"urls": [], "_status": mock_status})
            
            assert "error" in result
            assert "No URLs" in result["error"]

    @pytest.mark.asyncio
    async def test_extract_too_many_urls(self, mock_system_config, mock_server_config):
        """Test extract with too many URLs returns error."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            
            mock_status = AsyncMock()
            urls = [f"https://example.com/{i}" for i in range(25)]
            result = await server.call("extract", {"urls": urls, "_status": mock_status})
            
            assert "error" in result
            assert "Too many URLs" in result["error"]

    @pytest.mark.asyncio
    async def test_extract_success(self, mock_system_config, mock_server_config):
        """Test successful content extraction."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            server.cache_enabled = False  # Disable cache for this test
            
            mock_response = {
                "results": [
                    {"url": "https://example.com/1", "raw_content": "# Page Content\n\nHello world"},
                ],
                "failed_results": []
            }
            
            mock_client = AsyncMock()
            mock_client.extract = AsyncMock(return_value=mock_response)
            
            async def mock_get_client():
                return mock_client
            
            with patch.object(server, '_get_client', mock_get_client):
                mock_status = AsyncMock()
                result = await server.call("extract", {
                    "urls": ["https://example.com/1"],
                    "_status": mock_status
                })
                
                assert "error" not in result
                assert result["success_count"] == 1
                assert result["failed_count"] == 0
                assert result["results"][0]["raw_content"] == "# Page Content\n\nHello world"

    @pytest.mark.asyncio
    async def test_extract_with_failures(self, mock_system_config, mock_server_config):
        """Test extraction with some failures."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            server.cache_enabled = False  # Disable cache for this test
            
            mock_response = {
                "results": [
                    {"url": "https://example.com/1", "raw_content": "Content"},
                ],
                "failed_results": [
                    {"url": "https://blocked.com", "error": "Access denied"}
                ]
            }
            
            mock_client = AsyncMock()
            mock_client.extract = AsyncMock(return_value=mock_response)
            
            async def mock_get_client():
                return mock_client
            
            with patch.object(server, '_get_client', mock_get_client):
                mock_status = AsyncMock()
                result = await server.call("extract", {
                    "urls": ["https://example.com/1", "https://blocked.com"],
                    "_status": mock_status
                })
                
                assert result["success_count"] == 1
                assert result["failed_count"] == 1
                assert result["failed_results"][0]["error"] == "Access denied"

    @pytest.mark.asyncio
    async def test_extract_single_url_as_string(self, mock_system_config, mock_server_config):
        """A model that sends one URL as a string instead of a list: its
        characters were counted as URLs ("Too many URLs (34)")."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            server._client = AsyncMock()
            server._client.extract = AsyncMock(return_value={
                "results": [{"url": "https://example.com/a/long/path", "raw_content": "text"}], "failed_results": []})
            result = await server.call("extract", {"urls": "https://example.com/a/long/path", "_status": AsyncMock()})
            assert "error" not in result, result
            assert server._client.extract.call_args.kwargs["urls"] == ["https://example.com/a/long/path"]

    @pytest.mark.asyncio
    async def test_extract_cancellation(self, mock_system_config, mock_server_config):
        """Test extract respects cancellation token."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            
            mock_status = AsyncMock()
            mock_cancel = MagicMock()
            mock_cancel.is_cancelled = True
            
            result = await server.call("extract", {
                "urls": ["https://example.com"],
                "_status": mock_status,
                "_cancellation_token": mock_cancel
            })
            
            assert result.get("cancelled") is True


class TestTavilyErrorHandling:
    """tavily-python raises one class per HTTP status and carries only the
    API's detail text, never the status code: the class is what tells them
    apart. The real classes are raised here, not look-alike strings."""

    @pytest.mark.asyncio
    async def test_invalid_api_key_error(self, mock_system_config, mock_server_config):
        """Test handling of invalid API key error."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'invalid'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            server.cache_enabled = False
            
            mock_client = AsyncMock()
            mock_client.search = AsyncMock(side_effect=InvalidAPIKeyError("Unauthorized: missing or invalid API key."))
            
            async def mock_get_client():
                return mock_client
            
            with patch.object(server, '_get_client', mock_get_client):
                mock_status = AsyncMock()
                result = await server.call("web_search", {"query": "test", "_status": mock_status})
                
                assert result["error"] == "Invalid Tavily API key. Please check your configuration."

    @pytest.mark.asyncio
    async def test_rate_limit_error(self, mock_system_config, mock_server_config):
        """Test handling of rate limit error."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            server.cache_enabled = False
            
            mock_client = AsyncMock()
            mock_client.search = AsyncMock(side_effect=UsageLimitExceededError("Too many requests"))
            
            async def mock_get_client():
                return mock_client
            
            with patch.object(server, '_get_client', mock_get_client):
                mock_status = AsyncMock()
                result = await server.call("web_search", {"query": "test", "_status": mock_status})
                
                assert result["error"] == "Tavily API rate limit exceeded. Please try again later."

    @pytest.mark.asyncio
    async def test_plan_limit_is_not_reported_as_rate_limit(self, mock_system_config, mock_server_config):
        """432 (plan credits used up) arrives as ForbiddenError whose detail
        says "limit". Calling it a rate limit to retry later sent the model
        into retries that cannot succeed; the API's own words must reach it."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            server._client = AsyncMock()
            detail = "This request exceeds your plan's set usage limit."
            server._client.search = AsyncMock(side_effect=ForbiddenError(detail))
            server._client.extract = AsyncMock(side_effect=ForbiddenError(detail))
            result = await server.call("web_search", {"query": "test", "_status": AsyncMock()})
            assert result["error"] == f"ForbiddenError: {detail}"
            result = await server.call("extract", {"urls": ["https://example.com"], "_status": AsyncMock()})
            assert result["error"] == f"ForbiddenError: {detail}"
            assert result["failed_results"] == [{"url": "https://example.com", "error": f"ForbiddenError: {detail}"}]

    @pytest.mark.asyncio
    async def test_error_without_detail_still_says_what_failed(self, mock_system_config, mock_server_config):
        """A body without a detail gives the exception None or "": the model
        was told "None" or an empty error."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            server._client = AsyncMock()
            for exc, expected in ((ForbiddenError(None), "ForbiddenError"), (BadRequestError(""), "BadRequestError")):
                server._client.search = AsyncMock(side_effect=exc)
                result = await server.call("web_search", {"query": "test", "_status": AsyncMock()})
                assert result["error"] == expected

    async def test_a_server_error_is_one_bounded_line(self, mock_system_config, mock_server_config):
        """httpx's 5xx message spans two lines with a docs link."""
        request = httpx.Request("POST", "https://api.tavily.com/search")
        exc = httpx.HTTPStatusError("Server error '500' for url\nFor more information check: " + "x" * 500,
                                    request=request, response=httpx.Response(500, request=request))
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            server._client = AsyncMock()
            server._client.search = AsyncMock(side_effect=exc)
            result = await server.call("web_search", {"query": "test", "_status": AsyncMock()})
        assert "\n" not in result["error"] and "more information" not in result["error"], result


class TestTavilyPluginFactory:
    """Test the Tavily plugin factory."""

    def test_plugin_factory_creates_server(self, mock_system_config, mock_server_config):
        """Test plugin factory creates server instance."""
        from plugins.tavily_search.plugin import PLUGIN_FACTORY
        
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = PLUGIN_FACTORY("tavily", mock_system_config, mock_server_config)
            assert server.name == "tavily"
            assert isinstance(server, TavilySearchServer)

    def test_plugin_factory_custom_name(self, mock_system_config, mock_server_config):
        """Test plugin factory with custom name."""
        from plugins.tavily_search.plugin import PLUGIN_FACTORY
        
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = PLUGIN_FACTORY("custom_tavily", mock_system_config, mock_server_config)
            assert server.name == "custom_tavily"


class TestToolsHiddenWithoutKey:
    """No key, no tools. The alternative -- two tools whose every call fails
    with 'API key not configured' -- costs an agent a step per session to
    find out, and a prompt rule to avoid. The schema hides them instead."""

    def test_without_a_key_the_server_offers_nothing(self, mock_system_config, mock_server_config):
        with patch.dict('os.environ', {}, clear=True):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            assert server.api_key == ""
            assert server.get_tools() == []

    def test_with_a_key_both_tools_are_back(self, mock_system_config, mock_server_config):
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'tvly-x'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_server_config)
            names = {t["function"]["name"] for t in server.get_tools()}
        assert names == {"tavily_web_search", "tavily_extract"}


class TestContentCap:
    """Whole pages went to the model uncut: 20 search results with
    include_raw_content, or 20 extracted pages, made one tool result of
    millions of characters. Each page is cut to max_content_chars."""

    @staticmethod
    def _server(mock_system_config, cap=None):
        from agent_system.config.models import ToolServerConfig, AgentConfig
        config = ToolServerConfig(type="tavily_search", enabled=True, agent_config=AgentConfig())
        if cap is not None:
            config.max_content_chars = cap
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, config)
        server._client = AsyncMock()
        return server

    @pytest.mark.parametrize("cap", [None, 0, -5, "300", 2.5, True])
    def test_invalid_cap_is_the_default(self, mock_system_config, cap):
        assert self._server(mock_system_config, cap).max_content_chars == 20000

    @pytest.mark.asyncio
    async def test_search_pages_are_cut_and_marked(self, mock_system_config):
        server = self._server(mock_system_config, 100)
        server._client.search = AsyncMock(return_value={"results": [
            {"title": "long", "url": "https://e/1", "content": "c", "score": 1, "raw_content": "x" * 250},
            {"title": "short", "url": "https://e/2", "content": "c", "score": 1, "raw_content": "y" * 100},
        ]})
        result = await server.call("web_search", {"query": "q", "include_raw_content": True, "_status": AsyncMock()})
        long, short = result["results"]
        assert long["raw_content"] == "x" * 100 and long["truncated"] == 250
        assert short["raw_content"] == "y" * 100 and "truncated" not in short

    @pytest.mark.asyncio
    async def test_extract_pages_are_cut_and_marked(self, mock_system_config):
        server = self._server(mock_system_config, 100)
        server._client.extract = AsyncMock(return_value={
            "results": [{"url": "https://e/1", "raw_content": "x" * 250}], "failed_results": []})
        result = await server.call("extract", {"urls": ["https://e/1"], "_status": AsyncMock()})
        assert result["results"][0]["raw_content"] == "x" * 100
        assert result["results"][0]["truncated"] == 250

    @pytest.mark.asyncio
    async def test_cache_keeps_the_whole_page_so_a_new_cap_applies(self, mock_system_config):
        server = self._server(mock_system_config, 100)
        server._client.extract = AsyncMock(return_value={
            "results": [{"url": "https://e/1", "raw_content": "x" * 250}], "failed_results": []})
        await server.call("extract", {"urls": ["https://e/1"], "_status": AsyncMock()})
        server.max_content_chars = 200  # a raised cap, the next call served from the cache
        result = await server.call("extract", {"urls": ["https://e/1"], "_status": AsyncMock()})
        assert server._client.extract.await_count == 1
        assert result["results"][0]["raw_content"] == "x" * 200
        assert result["results"][0]["truncated"] == 250

    @pytest.mark.asyncio
    async def test_cached_search_is_cut_by_the_current_cap(self, mock_system_config):
        server = self._server(mock_system_config, 100)
        server._client.search = AsyncMock(return_value={"results": [
            {"title": "t", "url": "https://e/1", "content": "c", "score": 1, "raw_content": "x" * 250}]})
        params = {"query": "q", "include_raw_content": True}
        await server.call("web_search", {**params, "_status": AsyncMock()})
        server.max_content_chars = 200
        result = await server.call("web_search", {**params, "_status": AsyncMock()})
        assert server._client.search.await_count == 1
        assert result["results"][0]["raw_content"] == "x" * 200
        assert result["results"][0]["truncated"] == 250
