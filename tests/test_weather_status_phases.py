import pytest
import sys
from pathlib import Path

# Add plugins to path for imports
sys.path.append(str(Path(__file__).parent.parent / "src" / "plugins"))

from weather.server import WeatherServer
from agent_system.mcp.status import status_bus, PHASE_START, PHASE_END, PHASE_ERROR
import asyncio


@pytest.mark.anyio
async def test_weather_status_phases():
    """Test that weather plugin sends correct status phases."""
    server = WeatherServer("weather_test")
    
    # Subscribe to status events
    queue = await status_bus.subscribe(server="weather_test")
    
    try:
        # Call weather with valid location
        result = await server.call("forecast", {"location": "Munich, Germany"})
        
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
        
        assert PHASE_START in phases, "Weather should send START phase"
        if result.get("status") == "success":
            assert PHASE_END in phases, "Weather should send END phase on success"
        else:
            assert PHASE_ERROR in phases, "Weather should send ERROR phase on failure"
            
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
async def test_weather_error_status_phases():
    """Test that weather plugin sends ERROR phase for invalid input."""
    server = WeatherServer("weather_test_error")
    
    # Subscribe to status events
    queue = await status_bus.subscribe(server="weather_test_error")
    
    try:
        # Call weather with missing location
        result = await server.call("forecast", {})
        
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
        
        assert PHASE_ERROR in phases, "Weather should send ERROR phase for missing location"
        assert result["status"] == "error", "Result should indicate error"
        
    finally:
        status_bus.unsubscribe(queue)
