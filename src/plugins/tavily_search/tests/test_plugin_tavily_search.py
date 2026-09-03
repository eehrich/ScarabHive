"""Tests for the Tavily search plugin."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from plugins.tavily_search.server import TavilySearchServer


class TestTavilySearchServerInit:
    """Test TavilySearchServer initialization."""

    def test_server_initialization(self, mock_system_config, mock_mcp_config):
        """Test basic server initialization."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test-api-key'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
            assert server.name == "tavily"
            assert server.api_key == "test-api-key"

    def test_server_initialization_with_config_api_key(self, mock_system_config):
        """Test server initialization with API key from config."""
        from agent_system.config.models import MCPConfig, AgentConfig
        mcp_config = MCPConfig(type="tavily_search", enabled=True, agent_config=AgentConfig())
        mcp_config.api_key = "config-api-key"
        
        with patch.dict('os.environ', {}, clear=True):
            server = TavilySearchServer("tavily", mock_system_config, mcp_config)
            assert server.api_key == "config-api-key"

    def test_server_initialization_without_api_key(self, mock_system_config, mock_mcp_config):
        """Test server initialization without API key logs warning."""
        with patch.dict('os.environ', {}, clear=True):
            with patch('plugins.tavily_search.server.logger') as mock_logger:
                server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
                assert server.api_key == ""
                mock_logger.warning.assert_called()

    def test_server_initialization_with_cache_config(self, mock_system_config):
        """Test server initialization with cache configuration."""
        from agent_system.config.models import MCPConfig, AgentConfig
        mcp_config = MCPConfig(type="tavily_search", enabled=True, agent_config=AgentConfig())
        mcp_config.cache_ttl = 600
        mcp_config.cache_enabled = False
        
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mcp_config)
            assert server.cache_enabled is False


class TestTavilySearchServerSchema:
    """Test TavilySearchServer schema/tools."""

    def test_server_provides_tools(self, mock_system_config, mock_mcp_config):
        """Test server provides search and extract tools."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
            tools = server.get_tools()
            
            assert isinstance(tools, list)
            assert len(tools) == 2
            
            tool_names = [t["function"]["name"] for t in tools]
            assert "tavily_web_search" in tool_names
            assert "tavily_extract" in tool_names

    def test_web_search_tool_schema(self, mock_system_config, mock_mcp_config):
        """Test web_search tool schema structure."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
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

    def test_extract_tool_schema(self, mock_system_config, mock_mcp_config):
        """Test extract tool schema structure."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
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
    async def test_web_search_empty_query(self, mock_system_config, mock_mcp_config):
        """Test web_search with empty query returns error."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
            
            mock_status = AsyncMock()
            result = await server.call("web_search", {"query": "", "_status": mock_status})
            
            assert "error" in result
            assert "Empty query" in result["error"]

    @pytest.mark.asyncio
    async def test_web_search_no_api_key(self, mock_system_config, mock_mcp_config):
        """Test web_search without API key returns error."""
        with patch.dict('os.environ', {}, clear=True):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
            
            mock_status = AsyncMock()
            result = await server.call("web_search", {"query": "test", "_status": mock_status})
            
            assert "error" in result

    @pytest.mark.asyncio
    async def test_web_search_success(self, mock_system_config, mock_mcp_config):
        """Test successful web search."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
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

    @pytest.mark.asyncio
    async def test_web_search_with_filters(self, mock_system_config, mock_mcp_config):
        """Test web search with domain filters."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
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
    async def test_web_search_cancellation(self, mock_system_config, mock_mcp_config):
        """Test web search respects cancellation token."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
            
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
    async def test_web_search_cache_hit(self, mock_system_config, mock_mcp_config):
        """Test web search returns cached results."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
            
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
    async def test_extract_no_urls(self, mock_system_config, mock_mcp_config):
        """Test extract with no URLs returns error."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
            
            mock_status = AsyncMock()
            result = await server.call("extract", {"urls": [], "_status": mock_status})
            
            assert "error" in result
            assert "No URLs" in result["error"]

    @pytest.mark.asyncio
    async def test_extract_too_many_urls(self, mock_system_config, mock_mcp_config):
        """Test extract with too many URLs returns error."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
            
            mock_status = AsyncMock()
            urls = [f"https://example.com/{i}" for i in range(25)]
            result = await server.call("extract", {"urls": urls, "_status": mock_status})
            
            assert "error" in result
            assert "Too many URLs" in result["error"]

    @pytest.mark.asyncio
    async def test_extract_success(self, mock_system_config, mock_mcp_config):
        """Test successful content extraction."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
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
    async def test_extract_with_failures(self, mock_system_config, mock_mcp_config):
        """Test extraction with some failures."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
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
    async def test_extract_cancellation(self, mock_system_config, mock_mcp_config):
        """Test extract respects cancellation token."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
            
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
    """Test error handling in TavilySearchServer."""

    @pytest.mark.asyncio
    async def test_invalid_api_key_error(self, mock_system_config, mock_mcp_config):
        """Test handling of invalid API key error."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'invalid'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
            server.cache_enabled = False
            
            mock_client = AsyncMock()
            mock_client.search = AsyncMock(side_effect=Exception("401 Unauthorized"))
            
            async def mock_get_client():
                return mock_client
            
            with patch.object(server, '_get_client', mock_get_client):
                mock_status = AsyncMock()
                result = await server.call("web_search", {"query": "test", "_status": mock_status})
                
                assert "error" in result
                assert "Invalid" in result["error"] or "API key" in result["error"]

    @pytest.mark.asyncio
    async def test_rate_limit_error(self, mock_system_config, mock_mcp_config):
        """Test handling of rate limit error."""
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = TavilySearchServer("tavily", mock_system_config, mock_mcp_config)
            server.cache_enabled = False
            
            mock_client = AsyncMock()
            mock_client.search = AsyncMock(side_effect=Exception("429 Rate limit exceeded"))
            
            async def mock_get_client():
                return mock_client
            
            with patch.object(server, '_get_client', mock_get_client):
                mock_status = AsyncMock()
                result = await server.call("web_search", {"query": "test", "_status": mock_status})
                
                assert "error" in result
                assert "rate limit" in result["error"].lower()


class TestTavilyPluginFactory:
    """Test the Tavily plugin factory."""

    def test_plugin_factory_creates_server(self, mock_system_config, mock_mcp_config):
        """Test plugin factory creates server instance."""
        from plugins.tavily_search.plugin import PLUGIN_FACTORY
        
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = PLUGIN_FACTORY("tavily", mock_system_config, mock_mcp_config)
            assert server.name == "tavily"
            assert isinstance(server, TavilySearchServer)

    def test_plugin_factory_custom_name(self, mock_system_config, mock_mcp_config):
        """Test plugin factory with custom name."""
        from plugins.tavily_search.plugin import PLUGIN_FACTORY
        
        with patch.dict('os.environ', {'TAVILY_API_KEY': 'test'}):
            server = PLUGIN_FACTORY("custom_tavily", mock_system_config, mock_mcp_config)
            assert server.name == "custom_tavily"
