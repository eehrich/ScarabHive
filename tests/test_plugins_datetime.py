import pytest
from plugins.datetime.plugin import PLUGIN_FACTORY as datetime_factory
from plugins.datetime.server import DateTimeServer

def test_plugin_factory_creates_server():
    server = datetime_factory("datetime_test", cfg=None, ssl_verify=True)
    assert server is not None
    assert server.name == "datetime_test"


@pytest.mark.asyncio
async def test_current_action_returns_success():
    server = datetime_factory("datetime_test")
    from unittest.mock import AsyncMock
    mock_status = AsyncMock()
    result = await server.call("datetime_operations", {"operation": "current", "timezone": "UTC", "format": "iso", "_status": mock_status})
    assert isinstance(result, dict)
    assert result.get("status") == "success"
    assert "current_time" in result


@pytest.mark.asyncio
async def test_format_and_add_actions():
    server = datetime_factory("datetime_test")
    from unittest.mock import AsyncMock

    # Test formatting a known date
    mock_status = AsyncMock()
    r = await server.call("datetime_operations", {"operation": "format", "datetime": "2020-01-02T15:04:05", "format": "%Y/%m/%d", "_status": mock_status})
    assert r.get("status") == "success"
    assert r.get("formatted") == "2020/01/02"

    # Test adding days
    r2 = await server.call("datetime_operations", {"operation": "add_time", "datetime": "2020-01-01T00:00:00", "days": 1, "_status": mock_status})
    assert r2.get("status") == "success"
    assert r2.get("result", "").startswith("2020-01-02")


class TestDateTimeServer:
    """Test the DateTime server functionality."""
    
    def test_datetime_server_initialization(self):
        """Test datetime server initialization."""
        server = DateTimeServer("datetime", {}, True)
        assert server.name == "datetime"
        assert server.ssl_verify is True
    
    def test_datetime_server_schema(self):
        """Test datetime server tools structure."""
        server = DateTimeServer("datetime", {}, True)
        tools = server.get_tools()
        
        assert isinstance(tools, list)
        assert len(tools) == 1
        
        tool = tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "datetime_operations"
        assert "operation" in tool["function"]["parameters"]["properties"]
    
    def test_datetime_server_tool_name(self):
        """Test datetime server tool name."""
        server = DateTimeServer("datetime", {}, True)
        tools = server.get_tools()
        assert tools[0]["function"]["name"] == "datetime_operations"
    
    @pytest.mark.asyncio
    async def test_datetime_current(self):
        """Test current datetime functionality."""
        server = DateTimeServer("datetime", {}, True)
        from unittest.mock import AsyncMock
        
        mock_status = AsyncMock()
        result = await server.call("datetime_operations", {"operation": "current", "_status": mock_status})
        assert isinstance(result, dict)
        # Just check that it returns something, don't assume structure
    
    @pytest.mark.asyncio
    async def test_datetime_invalid_tool(self):
        """Test datetime server with invalid tool name."""
        server = DateTimeServer("datetime", {}, True)
        from unittest.mock import AsyncMock
        
        mock_status = AsyncMock()
        result = await server.call("invalid_tool", {"_status": mock_status})
        assert result["status"] == "error"
        assert "Unknown tool" in result["error"]
