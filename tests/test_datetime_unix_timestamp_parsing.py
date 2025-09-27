"""Test datetime plugin Unix timestamp parsing fix."""

import sys
from pathlib import Path

import pytest

# Add src to path for plugin import
project_root = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(project_root))

from plugins.datetime.server import DateTimeServer  # noqa: E402


class MockStatus:
    """Mock status object for testing."""
    
    async def progress(self, msg: str) -> None:
        pass
    
    async def error(self, msg: str) -> None:
        pass
    
    async def end(self, msg: str) -> None:
        pass


@pytest.fixture
def datetime_server():
    """Create a DateTimeServer instance."""
    return DateTimeServer()


@pytest.fixture
def mock_status():
    """Create a mock status object."""
    return MockStatus()


@pytest.mark.asyncio
async def test_format_unix_timestamp_with_fractional_seconds(datetime_server, mock_status):
    """Test formatting Unix timestamp with fractional seconds (from log error)."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "format",
        "datetime": "1758826754.2517202",
        "format": "%Y-%m-%d %H:%M:%S",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["original"] == "1758826754.2517202"
    assert result["formatted"] == "2025-09-25 18:59:14"
    assert result["year"] == 2025
    assert result["month"] == 9
    assert result["day"] == 25


@pytest.mark.asyncio
async def test_format_unix_timestamp_integer(datetime_server, mock_status):
    """Test formatting Unix timestamp as integer (from log error)."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "format",
        "datetime": "1758826754",
        "format": "%Y-%m-%d %H:%M:%S",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["original"] == "1758826754"
    assert result["formatted"] == "2025-09-25 18:59:14"


@pytest.mark.asyncio
async def test_parse_unix_timestamp_with_fractional_seconds(datetime_server, mock_status):
    """Test parsing Unix timestamp with fractional seconds."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "parse",
        "datetime": "1758826754.2517202",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["original"] == "1758826754.2517202"
    assert result["parsed_format"] == "unix_timestamp"
    assert result["unix_timestamp"] == 1758826754
    assert result["components"]["year"] == 2025
    assert result["components"]["month"] == 9
    assert result["components"]["day"] == 25


@pytest.mark.asyncio
async def test_format_still_handles_iso_datetime(datetime_server, mock_status):
    """Test that format action still handles regular ISO datetime strings."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "format",
        "datetime": "2025-09-25T18:59:14",
        "format": "%d/%m/%Y %H:%M",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["original"] == "2025-09-25T18:59:14"
    assert result["formatted"] == "25/09/2025 18:59"


@pytest.mark.asyncio
async def test_parse_still_handles_iso_datetime(datetime_server, mock_status):
    """Test that parse action still handles regular ISO datetime strings."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "parse",
        "datetime": "2025-09-25T18:59:14",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["original"] == "2025-09-25T18:59:14"
    assert result["parsed_format"] == "%Y-%m-%dT%H:%M:%S"
    assert result["components"]["year"] == 2025


@pytest.mark.asyncio
async def test_invalid_unix_timestamp_still_errors_appropriately(datetime_server, mock_status):
    """Test that invalid timestamps still produce appropriate errors."""
    result = await datetime_server.call("datetime_operations", {
        "operation": "format",
        "datetime": "invalid_timestamp",
        "format": "%Y-%m-%d %H:%M:%S",
        "_status": mock_status
    })
    
    assert result["status"] == "error"
    assert "Could not parse datetime" in result["error"]
    assert result["input"] == "invalid_timestamp"


@pytest.mark.asyncio
async def test_extreme_unix_timestamp_handling(datetime_server, mock_status):
    """Test handling of edge case Unix timestamps."""
    # Test timestamp 0 (Unix epoch)
    result = await datetime_server.call("datetime_operations", {
        "operation": "format",
        "datetime": "0",
        "format": "%Y-%m-%d %H:%M:%S",
        "_status": mock_status
    })
    
    assert result["status"] == "success"
    assert result["original"] == "0"
    # Should be 1970-01-01 00:00:00 UTC
    assert result["formatted"] == "1970-01-01 00:00:00"
    assert result["year"] == 1970