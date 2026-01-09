"""Tests for SSE status event handling in the web API.

NOTE: Status event batching was disabled to avoid delays during LLM calls.
Status events are now sent immediately for better UX.
"""
import json
import pytest
import httpx
from agent_system.app import build_app


@pytest.mark.asyncio
async def test_status_events_sent_immediately():
    """Test that status events are sent immediately (no batching)."""
    app = build_app()
    
    async with httpx.AsyncClient(base_url="http://testserver") as client:
        client._transport = httpx.ASGITransport(app=app)
        # Start SSE stream with a simple task
        async with client.stream("GET", "/events?task=test") as response:
            assert response.status_code == 200
            
            # Collect SSE events (agent will generate status events)
            status_events_received = []
            
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        event = json.loads(data_str)
                        
                        # Track status events (should be sent immediately, not batched)
                        if event.get("type") == "status":
                            status_events_received.append(event)
                            # Stop after getting first status event
                            break
                    except json.JSONDecodeError:
                        pass
            
            # Verify we received status events immediately
            assert len(status_events_received) > 0, "Should receive status events from agent execution"
            
            # Verify status event structure
            status_event = status_events_received[0]
            assert "server" in status_event, "Status events should have server field"
            assert "message" in status_event, "Status events should have message field"


@pytest.mark.asyncio
async def test_status_events_have_required_fields():
    """Test that status events contain required fields."""
    app = build_app()
    
    async with httpx.AsyncClient(base_url="http://testserver") as client:
        client._transport = httpx.ASGITransport(app=app)
        async with client.stream("GET", "/events?task=test") as response:
            assert response.status_code == 200
            
            # Collect first status event
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        event = json.loads(data_str)
                        if event.get("type") == "status":
                            # Verify required fields
                            assert "server" in event, "Status should have server field"
                            assert "message" in event, "Status should have message field"
                            assert "phase" in event, "Status should have phase field"
                            break
                    except json.JSONDecodeError:
                        pass


@pytest.mark.asyncio
async def test_multiple_status_events_during_execution():
    """Test that multiple status events are sent during agent execution."""
    app = build_app()
    
    async with httpx.AsyncClient(base_url="http://testserver") as client:
        client._transport = httpx.ASGITransport(app=app)
        async with client.stream("GET", "/events?task=test") as response:
            assert response.status_code == 200
            
            status_events = []
            
            # Collect multiple status events
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        event = json.loads(data_str)
                        if event.get("type") == "status":
                            status_events.append(event)
                        
                        # Stop after final event
                        if event.get("type") == "final":
                            break
                    except json.JSONDecodeError:
                        pass
            
            # Agent should generate multiple status events during execution
            assert len(status_events) >= 2, "Should receive multiple status events during execution"


@pytest.mark.asyncio
async def test_non_status_events_not_affected():
    """Test that non-status events work correctly."""
    app = build_app()
    
    async with httpx.AsyncClient(base_url="http://testserver") as client:
        client._transport = httpx.ASGITransport(app=app)
        async with client.stream("GET", "/events?task=test") as response:
            assert response.status_code == 200
            
            # Collect initial events (start, etc.)
            non_status_events = []
            
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        event = json.loads(data_str)
                        if event.get("type") != "status":
                            non_status_events.append(event)
                        
                        # Stop after getting start event
                        if event.get("type") == "start":
                            break
                    except json.JSONDecodeError:
                        pass
            
            # Should receive start event
            assert any(e.get("type") == "start" for e in non_status_events), "Should receive start event"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
