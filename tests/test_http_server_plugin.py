"""
HTTP Server Plugin Tests - Multi-Tool Architecture Validation
Test comprehensive MCP server health checks and tool proxying functionality.
"""
import pytest
from unittest.mock import AsyncMock

from src.plugins.http_server.server import HTTPServer


@pytest.fixture
def mock_wrapped_server():
    """Mock wrapped MCP server for testing."""
    mock_server = AsyncMock()
    mock_server.call = AsyncMock()
    return mock_server


class TestHTTPServerPlugin:
    """Test HTTP Server plugin Multi-Tool architecture."""
    
    def test_init(self):
        """Test HTTP server initialization."""
        server = HTTPServer(name="HTTP Server")
        assert server.name == "HTTP Server"
        assert server.wrapped_server is None
    
    def test_init_custom_name(self):
        """Test HTTP server initialization with custom name.""" 
        server = HTTPServer(name="Custom HTTP")
        assert server.name == "Custom HTTP"
        assert server.wrapped_server is None

    def test_get_tools_returns_list(self):
        """Test get_tools returns list for Multi-Tool architecture."""
        server = HTTPServer(name="Test Server")
        tools = server.get_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0

    def test_get_tools_structure(self):
        """Test Multi-Tool format structure validation."""
        server = HTTPServer(name="Test Server")
        tools = server.get_tools()
        
        # Validate Multi-Tool structure
        assert isinstance(tools, list)
        tool = tools[0]
        assert "type" in tool
        assert tool["type"] == "function"
        assert "function" in tool
        
        func = tool["function"]
        assert "name" in func
        assert "description" in func  
        assert "parameters" in func
        assert func["name"] == "http_server_ops"

    def test_get_tools_parameters_schema(self):
        """Test tool parameters schema structure."""
        server = HTTPServer(name="Test Server")
        tools = server.get_tools()
        tool = tools[0]
        params = tool["function"]["parameters"]
        
        assert params["type"] == "object"
        assert "properties" in params
        assert "operation" in params["properties"]
        assert "tool" in params["properties"] 
        assert "params" in params["properties"]
        assert params["required"] == ["operation"]

    def test_get_tools_operation_enum(self):
        """Test operation parameter enum values."""
        server = HTTPServer(name="Test Server")
        tools = server.get_tools()
        operation_prop = tools[0]["function"]["parameters"]["properties"]["operation"]
        
        assert operation_prop["type"] == "string"
        assert "enum" in operation_prop
        assert set(operation_prop["enum"]) == {"health", "call"}

    def test_wrap_server(self, mock_wrapped_server):
        """Test wrapping an MCP server."""
        server = HTTPServer(name="Test Server")
        server.wrap_server(mock_wrapped_server)
        assert server.wrapped_server == mock_wrapped_server

    @pytest.mark.asyncio
    async def test_call_health_no_server(self):
        """Test health check without wrapped server."""
        server = HTTPServer(name="Test Server")
        result = await server.call("http_server_ops", {"operation": "health"})
        assert "error" in result
        assert "No server wrapped" in result["error"]

    @pytest.mark.asyncio  
    async def test_call_health_with_server(self, mock_wrapped_server):
        """Test health check with wrapped server."""
        server = HTTPServer(name="Test Server")
        server.wrap_server(mock_wrapped_server)
        
        result = await server.call("http_server_ops", {"operation": "health"})
        assert result == {"status": "ok", "server": "Test Server"}

    @pytest.mark.asyncio
    async def test_call_proxy_no_server(self):
        """Test tool call proxy without wrapped server."""
        server = HTTPServer(name="Test Server")
        result = await server.call("http_server_ops", {
            "operation": "call",
            "tool": "test_tool", 
            "params": {"test": "value"}
        })
        assert "error" in result
        assert "No server wrapped" in result["error"]

    @pytest.mark.asyncio
    async def test_call_proxy_with_server(self, mock_wrapped_server):
        """Test tool call proxy with wrapped server."""
        server = HTTPServer(name="Test Server")
        server.wrap_server(mock_wrapped_server)
        
        mock_wrapped_server.call.return_value = {"success": True, "data": "test"}
        
        result = await server.call("http_server_ops", {
            "operation": "call",
            "tool": "test_tool",
            "params": {"test": "value"}
        })
        
        mock_wrapped_server.call.assert_called_once_with("test_tool", {"test": "value"})
        assert result == {"success": True, "data": "test"}

    @pytest.mark.asyncio
    async def test_call_invalid_operation(self):
        """Test invalid operation parameter."""
        server = HTTPServer(name="Test Server")
        result = await server.call("http_server_ops", {"operation": "invalid"})
        assert "error" in result
        assert "Invalid operation" in result["error"]

    @pytest.mark.asyncio
    async def test_call_missing_tool_for_call_operation(self, mock_wrapped_server):
        """Test missing tool parameter for call operation."""
        server = HTTPServer(name="Test Server")
        server.wrap_server(mock_wrapped_server)
        
        result = await server.call("http_server_ops", {"operation": "call"})
        assert "error" in result
        assert "Tool name required" in result["error"]

    @pytest.mark.asyncio
    async def test_call_proxy_with_empty_params(self, mock_wrapped_server):
        """Test tool call proxy with empty params."""
        server = HTTPServer(name="Test Server")
        server.wrap_server(mock_wrapped_server)
        
        mock_wrapped_server.call.return_value = {"success": True}
        
        result = await server.call("http_server_ops", {
            "operation": "call",
            "tool": "test_tool"
            # No params provided
        })
        
        mock_wrapped_server.call.assert_called_once_with("test_tool", {})
        assert result == {"success": True}

    def test_get_default_action(self):
        """Test default action for HTTP server."""
        server = HTTPServer(name="Test Server")
        assert server.get_default_action() == "health"

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self, mock_wrapped_server):
        """Test calling unknown tool name."""
        server = HTTPServer(name="Test Server")
        server.wrap_server(mock_wrapped_server)
        
        result = await server.call("unknown_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_schema_loading_error_handling(self, monkeypatch):
        """Test schema loading error handling."""
        # Mock schema loader to return None
        def mock_load_schema(*args, **kwargs):
            return None
        
        monkeypatch.setattr("agent_system.plugins.schema_loader.load_schema_from_dir", mock_load_schema)
        
        server = HTTPServer(name="Test Server")
        with pytest.raises(RuntimeError, match="Failed to load schema for.*Missing or invalid schema.yaml"):
            server.get_tools()

    @pytest.mark.asyncio
    async def test_wrapped_server_exception_handling(self, mock_wrapped_server):
        """Test exception handling in wrapped server calls."""
        server = HTTPServer(name="Test Server")
        server.wrap_server(mock_wrapped_server)
        
        # Configure mock to raise exception
        mock_wrapped_server.call.side_effect = Exception("Server error")
        
        with pytest.raises(Exception, match="Server error"):
            await server.call("http_server_ops", {
                "operation": "call",
                "tool": "test_tool",
                "params": {}
            })
        
        # Verify the wrapped server was called
        assert mock_wrapped_server.call.called