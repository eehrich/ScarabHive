import pytest
import sys
import asyncio
from pathlib import Path

# Add plugins to path for imports
sys.path.append(str(Path(__file__).parent.parent / "src" / "plugins"))

from agent_system.mcp.status import status_bus, PHASE_START, PHASE_END, PHASE_ERROR

# Import DateTimeServer using explicit module path to avoid conflict with built-in datetime
def _import_datetime_server():
    import importlib.util
    datetime_plugin_path = Path(__file__).parent.parent / "src" / "plugins" / "datetime" / "server.py"
    spec = importlib.util.spec_from_file_location("datetime_plugin.server", datetime_plugin_path)
    datetime_plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(datetime_plugin)
    return datetime_plugin.DateTimeServer

DateTimeServer = _import_datetime_server()


@pytest.mark.anyio
async def test_datetime_status_phases():
    """Test that datetime plugin sends correct status phases."""
    server = DateTimeServer("datetime_test")

    # Subscribe to status events
    queue = await status_bus.subscribe(server="datetime_test")

    try:
        # Call datetime with valid action
        result = await server.call("current", {"timezone": "UTC"})

        # Give a moment for async status events to be processed
        await asyncio.sleep(0.1)

        # Collect events from queue
        events = []
        while True:
            try:
                event = queue.get_nowait()
                events.append(event)
            except asyncio.QueueEmpty:
                break

        phases = [event.phase for event in events]

        assert PHASE_START in phases, "DateTime should send START phase"
        if result.get("status") == "error":
            assert PHASE_ERROR in phases, "DateTime should send ERROR phase on failure"
        else:
            assert PHASE_END in phases, "DateTime should send END phase on success"

        # Check event ordering - START should come before END/ERROR
        start_index = None
        end_or_error_index = None

        for i, phase in enumerate(phases):
            if phase == PHASE_START and start_index is None:
                start_index = i
            elif phase in [PHASE_END, PHASE_ERROR] and end_or_error_index is None:
                end_or_error_index = i

        assert start_index is not None, "Should have START event"
        assert end_or_error_index is not None, "Should have END or ERROR event"
        assert start_index < end_or_error_index, "START should come before END/ERROR"

    finally:
        status_bus.unsubscribe(queue)


@pytest.mark.anyio
async def test_datetime_error_status_phases():
    """Test that datetime plugin sends ERROR phase for invalid input."""
    server = DateTimeServer("datetime_test_error")

    # Subscribe to status events
    queue = await status_bus.subscribe(server="datetime_test_error")

    try:
        # Call datetime with invalid data that should trigger an error
        result = await server.call("parse", {"datetime": "invalid-date-format"})

        # Give a moment for async status events to be processed
        await asyncio.sleep(0.1)

        # Collect events from queue
        events = []
        while True:
            try:
                event = queue.get_nowait()
                events.append(event)
            except asyncio.QueueEmpty:
                break

        phases = [event.phase for event in events]

        # Should still start and then either succeed or fail
        assert PHASE_START in phases, "DateTime should send START phase even for errors"
        if result.get("status") == "error":
            assert PHASE_ERROR in phases, "DateTime should send ERROR phase for invalid input"
        else:
            assert PHASE_END in phases, "DateTime should send END phase for successful parsing"

    finally:
        status_bus.unsubscribe(queue)
