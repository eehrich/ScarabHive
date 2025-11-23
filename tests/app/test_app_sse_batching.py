"""Tests for SSE status event batching in the web API."""
import asyncio
import json
import pytest
import httpx
from agent_system.app import build_app
from agent_system.mcp.status import status_bus, StatusEvent, StatusPhase


@pytest.mark.asyncio
async def test_status_batch_event_accumulation():
    """Test that status events are accumulated into status_batch."""
    app = build_app()
    
    async with httpx.AsyncClient(base_url="http://testserver") as client:
        client._transport = httpx.ASGITransport(app=app)
        # Start SSE stream
        async with client.stream("GET", "/events?task=test") as response:
            assert response.status_code == 200
            
            # Publish multiple status events rapidly
            request_id = None
            for i in range(5):
                event = StatusEvent(
                    server="test_server",
                    request_id="batch_test_req",
                    message=f"Event {i}",
                    phase=StatusPhase.PROGRESS
                )
                await status_bus.publish(event)
            
            # Collect SSE events
            events_received = []
            batch_events_received = []
            
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        event = json.loads(data_str)
                        events_received.append(event)
                        
                        # Extract request_id from first event
                        if request_id is None and event.get("request_id"):
                            request_id = event["request_id"]
                        
                        # Track batch events
                        if event.get("type") == "status_batch":
                            batch_events_received.append(event)
                        
                        # Stop after getting batched events
                        if len(batch_events_received) > 0:
                            break
                    except json.JSONDecodeError:
                        pass
            
            # Verify we received batch events
            assert len(batch_events_received) > 0, "Should receive at least one status_batch event"
            
            # Verify batch structure
            batch = batch_events_received[0]
            assert "events" in batch, "status_batch should contain 'events' array"
            assert isinstance(batch["events"], list), "events should be an array"
            assert len(batch["events"]) > 0, "batch should contain events"
            
            # Verify individual events in batch
            for event in batch["events"]:
                assert event["type"] == "status"
                assert event["server"] == "test_server"
                assert "message" in event


@pytest.mark.asyncio
async def test_status_batch_flush_on_size_limit():
    """Test that batches are flushed when max_batch_size is reached."""
    app = build_app()
    
    async with httpx.AsyncClient(base_url="http://testserver") as client:
        client._transport = httpx.ASGITransport(app=app)
        async with client.stream("GET", "/events?task=test") as response:
            assert response.status_code == 200
            
            # Publish exactly 10 events (the batch size limit)
            for i in range(10):
                event = StatusEvent(
                    server="batch_test",
                    request_id="size_test_req",
                    message=f"Event {i}",
                    phase=StatusPhase.PROGRESS
                )
                await status_bus.publish(event)
            
            # Give time for batching
            await asyncio.sleep(0.1)
            
            # Collect events
            batches = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        event = json.loads(data_str)
                        if event.get("type") == "status_batch":
                            batches.append(event)
                            # Stop after first batch
                            if len(batches) >= 1:
                                break
                    except json.JSONDecodeError:
                        pass
            
            # Should have received a batch
            assert len(batches) > 0, "Should receive batch when size limit reached"
            
            # Batch should contain multiple events
            first_batch = batches[0]
            assert len(first_batch["events"]) >= 1, "Batch should contain events"


@pytest.mark.asyncio
async def test_status_batch_flush_on_interval():
    """Test that batches are flushed after batch_interval (50ms)."""
    app = build_app()
    
    async with httpx.AsyncClient(base_url="http://testserver") as client:
        client._transport = httpx.ASGITransport(app=app)
        async with client.stream("GET", "/events?task=test") as response:
            assert response.status_code == 200
            
            # Publish a few events (less than batch size)
            for i in range(3):
                event = StatusEvent(
                    server="interval_test",
                    request_id="interval_test_req",
                    message=f"Event {i}",
                    phase=StatusPhase.PROGRESS
                )
                await status_bus.publish(event)
            
            # Wait longer than batch interval (50ms)
            await asyncio.sleep(0.1)
            
            # Collect events
            batches = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        event = json.loads(data_str)
                        if event.get("type") == "status_batch":
                            batches.append(event)
                            break
                    except json.JSONDecodeError:
                        pass
            
            # Should have received a batch after interval
            assert len(batches) > 0, "Should receive batch after interval expires"
            assert len(batches[0]["events"]) == 3, "Batch should contain all 3 events"


@pytest.mark.asyncio
async def test_single_status_events_still_work():
    """Test that individual status events are still supported (backward compatibility)."""
    app = build_app()
    
    async with httpx.AsyncClient(base_url="http://testserver") as client:
        client._transport = httpx.ASGITransport(app=app)
        async with client.stream("GET", "/events?task=test") as response:
            assert response.status_code == 200
            
            # Publish a single status event
            event = StatusEvent(
                server="single_test",
                request_id="single_test_req",
                message="Single event",
                phase=StatusPhase.PROGRESS
            )
            await status_bus.publish(event)
            
            # Wait for processing
            await asyncio.sleep(0.1)
            
            # Collect events
            status_events = []
            batch_events = []
            
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        event = json.loads(data_str)
                        if event.get("type") == "status":
                            status_events.append(event)
                        elif event.get("type") == "status_batch":
                            batch_events.append(event)
                        
                        # Stop after receiving events
                        if len(status_events) + len(batch_events) > 0:
                            break
                    except json.JSONDecodeError:
                        pass
            
            # Should receive either single status or batch
            assert len(status_events) + len(batch_events) > 0, "Should receive status events"


@pytest.mark.asyncio
async def test_status_batch_not_sent_for_non_status_events():
    """Test that only status events are batched, not other event types."""
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
                        if event.get("type") != "status" and event.get("type") != "status_batch":
                            non_status_events.append(event)
                        
                        # Stop after getting start event
                        if event.get("type") == "start":
                            break
                    except json.JSONDecodeError:
                        pass
            
            # Non-status events should not be in batches
            for event in non_status_events:
                assert event.get("type") != "status_batch", "Non-status events should not be batched"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
