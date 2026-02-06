"""Tests for status queue limits and backpressure handling."""
import asyncio
import pytest
from agent_system.mcp.status import StatusBus, StatusEvent, StatusPhase


@pytest.mark.asyncio
async def test_queue_has_default_limit():
    """Test that status queues have a default size limit."""
    bus = StatusBus()
    
    # Subscribe with default limit
    queue = await bus.subscribe()
    
    # Queue should have a maxsize set (not unlimited)
    assert queue.maxsize > 0
    assert queue.maxsize == 1000  # Default from subscribe()


@pytest.mark.asyncio
async def test_queue_custom_limit():
    """Test that status queues can have custom size limits."""
    bus = StatusBus()
    
    # Subscribe with custom limit
    queue = await bus.subscribe(maxsize=50)
    
    assert queue.maxsize == 50


@pytest.mark.asyncio
async def test_queue_drops_oldest_when_full():
    """Test that queue drops oldest events when full (prevents memory exhaustion)."""
    bus = StatusBus()
    
    # Small queue for testing
    queue = await bus.subscribe(maxsize=5)
    
    # Publish 10 events (more than queue can hold)
    for i in range(10):
        event = StatusEvent(
            server="test",
            request_id="req123",
            message=f"Event {i}",
            phase=StatusPhase.PROGRESS
        )
        await bus.publish(event)
    
    # Give queue handlers time to process
    await asyncio.sleep(0.1)
    
    # Queue should have exactly 5 events (maxsize)
    # The oldest 5 were dropped
    assert queue.qsize() == 5
    
    # Verify we have the NEWEST 5 events (5-9)
    events = []
    while not queue.empty():
        events.append(await queue.get())
    
    assert len(events) == 5
    messages = [e.message for e in events]
    # Should have events 5, 6, 7, 8, 9 (oldest 0-4 were dropped)
    assert messages == ["Event 5", "Event 6", "Event 7", "Event 8", "Event 9"]


@pytest.mark.asyncio
async def test_queue_full_does_not_block_other_sessions():
    """Test that one slow consumer doesn't block other sessions."""
    bus = StatusBus()
    
    # Two queues: one slow (full), one normal
    slow_queue = await bus.subscribe(request_id="slow", maxsize=3)
    fast_queue = await bus.subscribe(request_id="fast", maxsize=100)
    
    # Fill slow queue completely
    for i in range(5):
        event = StatusEvent(
            server="test",
            request_id="slow",
            message=f"Slow event {i}",
            phase=StatusPhase.PROGRESS
        )
        await bus.publish(event)
    
    await asyncio.sleep(0.05)
    
    # Slow queue should be full (maxsize=3, oldest dropped)
    assert slow_queue.qsize() == 3
    
    # Now publish to fast queue - should NOT be blocked by slow queue
    for i in range(10):
        event = StatusEvent(
            server="test",
            request_id="fast",
            message=f"Fast event {i}",
            phase=StatusPhase.PROGRESS
        )
        await bus.publish(event)
    
    await asyncio.sleep(0.05)
    
    # Fast queue should have all its events
    assert fast_queue.qsize() == 10
    
    # Slow queue should still be at maxsize (not affected by fast queue)
    assert slow_queue.qsize() == 3


@pytest.mark.asyncio
async def test_queue_performance_with_nowait():
    """Test that put_nowait is used for performance (no timeout overhead)."""
    bus = StatusBus()
    _ = await bus.subscribe(maxsize=1000)  # Create subscriber to activate handlers
    
    import time
    start = time.time()
    
    # Publish 100 events
    for i in range(100):
        event = StatusEvent(
            server="test",
            request_id="perf",
            message=f"Event {i}",
            phase=StatusPhase.PROGRESS
        )
        await bus.publish(event)
    
    elapsed = time.time() - start
    
    # Should be fast (< 0.5s for 100 events) because we use put_nowait
    # With blocking put() + timeout, this would take much longer
    assert elapsed < 0.5, f"Publishing 100 events took {elapsed:.2f}s (expected < 0.5s)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
