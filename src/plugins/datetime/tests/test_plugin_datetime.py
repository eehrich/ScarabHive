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
def datetime_server(mock_system_config, mock_server_config):
    """Create datetime server for testing."""
    return DateTimeServer("datetime", mock_system_config, mock_server_config)


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
async def test_datetime_plugin_factory(mock_system_config, mock_server_config):
    """Test datetime plugin factory creates server."""
    plugin = datetime_factory("test_datetime", mock_system_config, mock_server_config)
    assert plugin is not None
    assert hasattr(plugin, 'call')


@pytest.mark.asyncio
async def test_datetime_server_get_tools(mock_system_config, mock_server_config):
    """Test datetime server exposes correct tools."""
    server = DateTimeServer("datetime", mock_system_config, mock_server_config)
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

def test_answer_fields_covers_every_operation():
    """Every operation the schema offers must have an answer field mapped.

    Without this the end line degrades to the bare operation name, which is a
    restatement of the argument rather than a result. Measured 2026-09-03: the
    first version of `_ANSWER_FIELDS` was keyed on the HANDLERS and therefore
    missed `add_time`, `subtract_time` and `to_timestamp` -- which are exactly
    the spellings `schema.yaml` advertises, so three of eleven operations
    reported nothing but their own name to every caller.
    """
    from pathlib import Path

    import yaml

    from plugins.datetime.server import _ANSWER_FIELDS

    schema = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "schema.yaml").read_text(encoding="utf-8")
    )

    def enums(node):
        if isinstance(node, dict):
            if isinstance(node.get("enum"), list):
                yield node["enum"]
            for value in node.values():
                yield from enums(value)
        elif isinstance(node, list):
            for value in node:
                yield from enums(value)

    # The operation enum is the only one in this schema; assert that, so a
    # second enum cannot silently widen what this test looks at.
    all_enums = list(enums(schema))
    assert len(all_enums) == 1, f"expected one enum in schema.yaml, found {len(all_enums)}"

    operations = set(all_enums[0])
    missing = sorted(operations - set(_ANSWER_FIELDS))
    assert not missing, f"_ANSWER_FIELDS has no entry for: {missing}"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,params", [
    ("current", {}),
    ("add_time", {"days": 1}),
    ("subtract_time", {"days": 1}),
    ("to_timestamp", {}),
    # The OTHER direction, where `timestamp` is the echoed INPUT and the
    # answer is the moment it denotes. A table that lists `timestamp` first
    # reports the number the caller just sent.
    ("to_timestamp", {"timestamp": 1756900000}),
    ("business_days", {"start_date": "2026-09-01", "end_date": "2026-09-10"}),
    ("day_of_week", {}),
    ("calendar_info", {}),
])
async def test_every_operation_reports_an_answer_not_its_own_name(
    mock_system_config, mock_server_config, operation, params
):
    """The end line must carry a value, not repeat the operation argument."""
    server = DateTimeServer("datetime", mock_system_config, mock_server_config)
    status = MockStatus()

    result = await server.operations({"operation": operation, "_status": status, **params})

    assert result.get("status") == "success", result
    ends = [message for phase, message in status.messages if phase == "end"]
    assert len(ends) == 1, status.messages
    assert ends[0] != operation, \
        f"{operation}: end line is the bare operation name, no answer in it"
    assert ends[0].startswith(f"{operation}: "), ends[0]
    answer = ends[0][len(operation) + 2:].strip()
    assert answer, f"{operation}: empty answer"
    # The answer must not be a value the CALLER supplied: echoing an input
    # back is the failure mode this table exists to prevent.
    echoed = [str(v) for v in params.values()]
    assert answer not in echoed, \
        f"{operation}: the line reports the echoed input {answer!r}, not a result"
