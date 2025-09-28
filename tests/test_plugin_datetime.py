"""Test plugin for datetime operations."""

import pytest

from plugins.datetime.plugin import PLUGIN_FACTORY as datetime_factory
from plugins.datetime.server import DateTimeServer


class MockStatus:
    """Mock status object for testing."""
    def __init__(self):
        self.messages = []
    
    async def progress(self, message: str):
        self.messages.append(('progress', message))
    
    async def error(self, message: str):
        self.messages.append(('error', message))
    
    async def end(self, message: str = None):
        self.messages.append(('end', message))


@pytest.fixture
def mock_status():
    """Create mock status for testing."""
    return MockStatus()


@pytest.fixture
def datetime_server():
    """Create datetime server for testing."""
    return DateTimeServer("datetime_test")


# Basic datetime operations tests
@pytest.mark.asyncio
async def test_current_time_utc(datetime_server, mock_status):
    """Test getting current time in UTC."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "current",
        "timezone": "UTC",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert "current_time" in result
    assert "UTC" in result["current_time"] or "+00:00" in result["current_time"]


@pytest.mark.asyncio
async def test_current_time_local(datetime_server, mock_status):
    """Test getting current time in local timezone."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "current",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert "current_time" in result


@pytest.mark.asyncio
async def test_format_datetime(datetime_server, mock_status):
    """Test formatting a datetime string."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "format",
        "datetime": "2021-01-01T00:00:00Z",
        "format": "%Y/%m/%d",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert "formatted_time" in result
    assert result["formatted_time"] == "2021/01/01"


@pytest.mark.asyncio
async def test_parse_datetime(datetime_server, mock_status):
    """Test parsing a datetime string."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "parse",
        "datetime": "2021-01-01 12:00:00",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    # Check for either old or new API response format
    assert "parsed_datetime" in result or "iso_format" in result or "parsed" in result


@pytest.mark.asyncio
async def test_convert_timezone(datetime_server, mock_status):
    """Test converting timezone of a datetime."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "convert_timezone",
        "datetime": "2021-01-01T00:00:00Z",
        "to_timezone": "America/New_York",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert "converted_time" in result


@pytest.mark.asyncio
async def test_invalid_operation(datetime_server, mock_status):
    """Test invalid operation returns error."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "invalid_op",
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "error" in result or "message" in result


@pytest.mark.asyncio
async def test_missing_required_parameters(datetime_server, mock_status):
    """Test missing required parameters returns error."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "format",
        # Missing datetime parameter
        "format": "%Y-%m-%d",
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "error" in result or "message" in result


@pytest.mark.asyncio
async def test_invalid_datetime_string(datetime_server, mock_status):
    """Test invalid datetime string returns error."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "parse",
        "datetime": "not-a-datetime",
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "error" in result or "message" in result


@pytest.mark.asyncio
async def test_invalid_timezone(datetime_server, mock_status):
    """Test invalid timezone returns error."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "convert",
        "datetime": "2021-01-01T00:00:00Z",
        "to_timezone": "Invalid/Timezone",
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "error" in result or "message" in result


# Unix timestamp parsing tests
@pytest.mark.asyncio
async def test_parse_unix_timestamp(datetime_server, mock_status):
    """Test parsing Unix timestamp."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "parse",
        "datetime": "1609459200",  # 2021-01-01T00:00:00Z
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert "parsed_datetime" in result or "iso_format" in result or "parsed" in result
    result_str = str(result)
    assert "2021" in result_str


@pytest.mark.asyncio
async def test_parse_unix_timestamp_with_fractional_seconds(datetime_server, mock_status):
    """Test parsing Unix timestamp with fractional seconds."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "parse",
        "datetime": "1609459200.123",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert "parsed_datetime" in result or "iso_format" in result or "parsed" in result
    # Should contain fractional seconds in ISO format
    result_str = str(result)
    assert ".123" in result_str or "123" in result_str


@pytest.mark.asyncio
async def test_format_still_handles_iso_datetime(datetime_server, mock_status):
    """Test that format operation still handles ISO datetime strings."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "format",
        "datetime": "2021-01-01T00:00:00Z",
        "format": "%Y/%m/%d",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert "formatted_time" in result
    assert result["formatted_time"] == "2021/01/01"


# Plugin factory tests  
@pytest.mark.asyncio
async def test_datetime_plugin_factory():
    """Test datetime plugin factory creates server."""
    plugin = datetime_factory("test_datetime")
    assert plugin is not None
    assert hasattr(plugin, 'call')


@pytest.mark.asyncio
async def test_datetime_server_get_tools():
    """Test datetime server exposes correct tools."""
    server = DateTimeServer("test")
    tools = server.get_tools()
    
    assert len(tools) > 0
    # Extract tool names from the nested structure
    tool_names = []
    for tool in tools:
        if isinstance(tool, dict):
            if "function" in tool and "name" in tool["function"]:
                tool_names.append(tool["function"]["name"])
            elif "name" in tool:
                tool_names.append(tool["name"])
        elif hasattr(tool, 'name'):
            tool_names.append(tool.name)
        else:
            tool_names.append(str(tool))
    
    assert "datetime_operations" in tool_names