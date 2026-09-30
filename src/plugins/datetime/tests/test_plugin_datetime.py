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
        "operation": "convert_timezone",
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


# The schema offers only `operation`, `timezone`, `format`, `datetime`,
# `input_format`, the durations and `target_date`. These tests call the tool
# with exactly those names, the way a model does.

async def _run(server, **params):
    return await server.operations({"_status": MockStatus(), **params})


@pytest.mark.asyncio
async def test_convert_timezone_takes_the_schema_timezone_as_target(datetime_server):
    """`timezone` is the only target name the model sees; it was ignored and
    every conversion answered UTC."""
    result = await _run(datetime_server, operation="convert_timezone",
                        datetime="2026-01-15T14:30:00Z", timezone="America/New_York")
    assert result["converted_time"] == "2026-01-15T09:30:00-05:00", result


@pytest.mark.asyncio
async def test_business_days_counts_with_schema_names(datetime_server):
    result = await _run(datetime_server, operation="business_days",
                        datetime="2026-09-01", target_date="2026-09-10")
    assert result.get("business_days_between") == 7, result


@pytest.mark.asyncio
async def test_business_days_adds_with_schema_names(datetime_server):
    result = await _run(datetime_server, operation="business_days",
                        datetime="2026-09-04", days=1)
    assert result.get("result_date") == "2026-09-07", result  # Friday + 1 = Monday


@pytest.mark.asyncio
async def test_days_until_counts_from_datetime(datetime_server):
    result = await _run(datetime_server, operation="days_until",
                        datetime="2026-12-01", target_date="2026-12-24")
    assert result.get("days") == 23, result


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt,expected", [
    ("iso", "2026-01-15T14:30:00"),
    ("date_only", "2026-01-15"),
    ("human", "Thursday, January 15, 2026 at 02:30:00 PM"),
])
async def test_format_understands_the_named_formats(datetime_server, fmt, expected):
    """The schema names these formats; `format` used to print them literally."""
    result = await _run(datetime_server, operation="format",
                        datetime="2026-01-15T14:30:00", format=fmt)
    assert result.get("formatted_time") == expected, result


@pytest.mark.asyncio
async def test_format_short_unparseable_input_says_so(datetime_server):
    result = await _run(datetime_server, operation="format", datetime="today")
    assert result["error"] == "Could not parse datetime: today", result


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,field", [
    ("to_timestamp", "timestamp"),
    ("parse", "unix_timestamp"),
])
async def test_naive_datetime_is_utc_for_timestamps(
    datetime_server, monkeypatch, operation, field
):
    """Without an offset the moment is UTC, not the server's local zone.

    A naive `datetime.timestamp()` asks the host's zone, and on a UTC host
    (the server) that equals UTC -- so the host is made UTC+5 here."""
    import datetime as dt

    import plugins.datetime.server as server_module

    class HostIsUtcPlus5(dt.datetime):
        def timestamp(self):
            if self.tzinfo is None:
                return self.replace(tzinfo=dt.timezone(dt.timedelta(hours=5))).timestamp()
            return super().timestamp()

    monkeypatch.setattr(server_module, "datetime", HostIsUtcPlus5)
    result = await _run(datetime_server, operation=operation, datetime="2026-01-15T00:00:00")
    assert result.get(field) == 1768435200, result


# UTC+14 and UTC-11 are 25 hours apart: their dates always differ.
_EAST, _WEST = "Pacific/Kiritimati", "Pacific/Pago_Pago"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,params,field", [
    ("calendar_info", {}, "date"),
    ("day_of_week", {}, "day_of_week"),
    ("days_until", {"target_date": "2030-01-01"}, "days"),
    ("business_days", {"days": 0}, "result_date"),
    ("add_time", {}, "result"),
    ("current", {}, "current_time"),
])
async def test_now_follows_the_timezone(datetime_server, operation, params, field):
    """Every operation that falls back to "now" reads it in `timezone`."""
    east = await _run(datetime_server, operation=operation, timezone=_EAST, **params)
    west = await _run(datetime_server, operation=operation, timezone=_WEST, **params)
    # The date part only: full times differ anyway, by the microseconds
    # between the two calls.
    assert east.get(field) is not None, east
    assert str(east[field])[:10] != str(west[field])[:10], (east, west)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,field", [
    ("current", "current_time"),
    ("calendar_info", "date"),
    ("add_time", "result"),
])
async def test_now_defaults_to_the_prompt_timezone(
    mock_system_config, mock_server_config, operation, field
):
    """Without `timezone`, "now" is in `context.timezone` -- the zone the
    system prompt's current_date is rendered in -- so "today" agrees."""
    dates = []
    for zone in (_EAST, _WEST):
        mock_system_config.context.timezone = zone
        server = DateTimeServer("datetime", mock_system_config, mock_server_config)
        dates.append(str((await _run(server, operation=operation))[field])[:10])
    assert dates[0] != dates[1], dates


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", ["Mars/Olympus", None])
async def test_now_falls_back_to_utc_without_a_valid_context_timezone(
    mock_system_config, mock_server_config, configured
):
    mock_system_config.context.timezone = configured
    server = DateTimeServer("datetime", mock_system_config, mock_server_config)
    result = await _run(server, operation="add_time", hours=1)
    assert result["result"].endswith("+00:00"), result


@pytest.mark.asyncio
@pytest.mark.parametrize("text,iso", [
    ("2026-01-15T14:30", "2026-01-15T14:30:00"),
    ("2026-01-15T14:30:00.250", "2026-01-15T14:30:00.250000"),
])
@pytest.mark.parametrize("operation", ["parse", "format"])
async def test_parse_and_format_accept_iso_8601_beyond_the_pattern_list(
    datetime_server, operation, text, iso
):
    result = await _run(datetime_server, operation=operation, datetime=text)
    assert result.get("parsed_datetime") == iso, result


@pytest.mark.asyncio
async def test_add_time_across_dst_carries_the_offset_of_the_result_date(
    datetime_server, monkeypatch
):
    """pytz kept the start's offset: 10:00 CEST + 60 days read 10:00+02:00
    in November, an hour off Berlin's clock."""
    import datetime as dt

    import pytz

    import plugins.datetime.server as server_module

    start = pytz.timezone("Europe/Berlin").localize(dt.datetime(2026, 9, 30, 10, 0))
    monkeypatch.setattr(server_module, "_now", lambda params, default_tz: start)
    result = await _run(datetime_server, operation="add_time", days=60)
    assert result["result"] == "2026-11-29T10:00:00+01:00", result


@pytest.mark.asyncio
@pytest.mark.parametrize("start,hours,expected", [
    # Fall back: 01:30 CEST + 2 h of elapsed time is 02:30 CET, not 03:30.
    ((2026, 10, 25, 1, 30), 2, "2026-10-25T02:30:00+01:00"),
    # Spring forward: 02:30 does not exist; 01:30 CET + 1 h is 03:30 CEST.
    ((2026, 3, 29, 1, 30), 1, "2026-03-29T03:30:00+02:00"),
])
async def test_add_time_hours_across_dst_are_elapsed_time(
    datetime_server, monkeypatch, start, hours, expected
):
    import datetime as dt

    import pytz

    import plugins.datetime.server as server_module

    begin = pytz.timezone("Europe/Berlin").localize(dt.datetime(*start))
    monkeypatch.setattr(server_module, "_now", lambda params, default_tz: begin)
    result = await _run(datetime_server, operation="add_time", hours=hours)
    assert result["result"] == expected, result


@pytest.mark.asyncio
async def test_business_days_counts_when_target_date_is_set_even_with_days(datetime_server):
    result = await _run(datetime_server, operation="business_days",
                        datetime="2026-09-01", target_date="2026-09-10", days=0)
    assert result.get("business_days_between") == 7, result


@pytest.mark.asyncio
async def test_convert_timezone_names_an_unknown_zone(datetime_server):
    result = await _run(datetime_server, operation="convert_timezone", timezone="Mars/Olympus")
    assert result["error"] == "Invalid timezone: Mars/Olympus", result
