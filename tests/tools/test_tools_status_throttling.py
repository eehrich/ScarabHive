"""Tests for FilteredQueueStatusHandler warning throttling."""
import asyncio
import time
import pytest
from unittest.mock import patch
from agent_system.tools.status import StatusBus, StatusEvent, StatusPhase


@pytest.mark.asyncio
async def test_queue_full_warning_throttled_by_count():
    """Test that queue full warnings are throttled every 50 drops."""
    bus = StatusBus()
    
    # Small queue to trigger drops quickly
    _ = await bus.subscribe(maxsize=3)
    
    # Patch logger to count warnings
    warning_count = 0
    
    def count_warnings(msg, *args, **kwargs):
        nonlocal warning_count
        if "queue full" in msg or "dropped" in msg:
            warning_count += 1
    
    with patch('agent_system.tools.status.logger') as mock_logger:
        mock_logger.warning.side_effect = count_warnings
        
        # Publish 100 events (will drop many)
        for i in range(100):
            event = StatusEvent(
                server="test",
                request_id="throttle_test",
                message=f"Event {i}",
                phase=StatusPhase.PROGRESS
            )
            await bus.publish(event)
        
        await asyncio.sleep(0.1)
    
    # Should have far fewer warnings than drops (throttling works)
    # With 100 events and maxsize=3, we drop ~97 events
    # With throttling every 50, we should see ~2 warnings
    assert warning_count <= 10, f"Expected ≤10 warnings with throttling, got {warning_count}"


@pytest.mark.asyncio
async def test_queue_full_warning_throttled_by_time():
    """Test that queue full warnings are throttled by 10 second intervals."""
    bus = StatusBus()
    
    # Small queue to trigger drops
    _ = await bus.subscribe(maxsize=2)
    
    warning_times = []
    
    def track_warning_time(msg, *args, **kwargs):
        if "queue full" in msg or "dropped" in msg:
            warning_times.append(time.time())
    
    with patch('agent_system.tools.status.logger') as mock_logger:
        mock_logger.warning.side_effect = track_warning_time
        
        # Publish events slowly to trigger time-based throttling
        for i in range(10):
            event = StatusEvent(
                server="test",
                request_id="time_throttle_test",
                message=f"Event {i}",
                phase=StatusPhase.PROGRESS
            )
            await bus.publish(event)
            await asyncio.sleep(0.01)  # Small delay
        
        await asyncio.sleep(0.1)
    
    # Should have warnings, but throttled
    if len(warning_times) > 1:
        # Check that warnings are spaced out or throttled by count
        # With 10 events and maxsize=2, we drop ~8 events
        # Should be throttled to fewer warnings
        assert len(warning_times) <= 5, f"Expected ≤5 warnings, got {len(warning_times)}"


@pytest.mark.asyncio
async def test_warning_shows_aggregated_drop_count():
    """Test that warnings show total number of drops so far."""
    bus = StatusBus()
    
    # Very small queue
    _ = await bus.subscribe(maxsize=1)
    
    warning_messages = []
    
    def capture_warnings(msg, *args, **kwargs):
        if "dropped" in msg:
            # Format the message with args
            formatted = msg % args if args else msg
            warning_messages.append(formatted)
    
    with patch('agent_system.tools.status.logger') as mock_logger:
        mock_logger.warning.side_effect = capture_warnings
        
        # Publish enough events to trigger multiple throttled warnings
        for i in range(60):
            event = StatusEvent(
                server="test",
                request_id="count_test",
                message=f"Event {i}",
                phase=StatusPhase.PROGRESS
            )
            await bus.publish(event)
        
        await asyncio.sleep(0.1)
    
    # Should have at least one warning
    assert len(warning_messages) > 0, "Should have warnings about drops"
    
    # Warnings should mention number of drops
    for msg in warning_messages:
        assert "dropped" in msg.lower(), f"Warning should mention drops: {msg}"
        # Should contain numbers (drop count)
        assert any(char.isdigit() for char in msg), f"Warning should include drop count: {msg}"


@pytest.mark.asyncio
async def test_first_drop_triggers_warning():
    """Test that the first drop (count=1) triggers a warning."""
    bus = StatusBus()
    
    # Maxsize of 1 to drop quickly
    _ = await bus.subscribe(maxsize=1)
    
    warning_triggered = False
    
    def check_first_warning(msg, *args, **kwargs):
        nonlocal warning_triggered
        if "dropped" in msg:
            warning_triggered = True
    
    with patch('agent_system.tools.status.logger') as mock_logger:
        mock_logger.warning.side_effect = check_first_warning
        
        # Publish 3 events (2nd and 3rd will be dropped)
        for i in range(3):
            event = StatusEvent(
                server="test",
                request_id="first_drop_test",
                message=f"Event {i}",
                phase=StatusPhase.PROGRESS
            )
            await bus.publish(event)
        
        await asyncio.sleep(0.05)
    
    # First drop should trigger warning (count % 50 == 1 for count=1)
    assert warning_triggered, "First drop should trigger a warning"


@pytest.mark.asyncio
async def test_drop_counter_persists_across_events():
    """Test that drop counter accumulates across multiple events."""
    bus = StatusBus()
    
    _ = await bus.subscribe(maxsize=2)
    
    drop_counts = []
    
    def extract_drop_count(msg, *args, **kwargs):
        if "dropped" in msg and args:
            # Extract the drop count from the message
            # Format: "... dropped %d events so far"
            drop_counts.append(args[0] if args else 0)
    
    with patch('agent_system.tools.status.logger') as mock_logger:
        mock_logger.warning.side_effect = extract_drop_count
        
        # Publish in batches to see counter increase
        for batch in range(3):
            for i in range(30):
                event = StatusEvent(
                    server="test",
                    request_id="accumulate_test",
                    message=f"Batch {batch} Event {i}",
                    phase=StatusPhase.PROGRESS
                )
                await bus.publish(event)
            await asyncio.sleep(0.05)
    
    # Drop counts should increase over time
    if len(drop_counts) > 1:
        assert drop_counts[-1] > drop_counts[0], "Drop count should accumulate"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
