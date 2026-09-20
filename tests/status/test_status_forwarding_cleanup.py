"""Tests for StatusEventForwarder cleanup and memory leak prevention."""
import pytest
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder
from agent_system.tools.status import status_bus, StatusEvent, StatusPhase


@pytest.mark.asyncio
async def test_stop_forwarding_removes_handler_from_status_bus():
    """Test that stop_forwarding() removes handler from status_bus to prevent memory leak."""
    forwarder = StatusEventForwarder()
    
    # Start forwarding
    await forwarder.start_forwarding("test_request_123")
    
    # Verify handler was created and registered
    assert forwarder._handler is not None, "handler should be created"
    assert forwarder._handler in status_bus.handlers, "handler should be in status_bus"
    original_handler = forwarder._handler
    
    # Stop forwarding
    await forwarder.stop_forwarding()
    
    # Verify handler was removed
    assert original_handler not in status_bus.handlers, "handler should be removed from status_bus"
    assert forwarder._handler is None, "handler reference should be cleared"


@pytest.mark.asyncio
async def test_multiple_start_stop_cycles_cleanup_properly():
    """Test that multiple start/stop cycles don't accumulate handlers."""
    forwarder = StatusEventForwarder()
    
    initial_handler_count = len(status_bus.handlers)
    
    # Multiple start/stop cycles
    for i in range(3):
        await forwarder.start_forwarding(f"request_{i}")
        assert forwarder._handler is not None
        assert forwarder._handler in status_bus.handlers
        
        await forwarder.stop_forwarding()
        assert forwarder._handler is None
    
    # Should not have accumulated handlers
    final_handler_count = len(status_bus.handlers)
    assert final_handler_count == initial_handler_count, \
        f"Handler count should return to {initial_handler_count}, got {final_handler_count}"


@pytest.mark.asyncio
async def test_stop_forwarding_handles_missing_handler_gracefully():
    """Test that stop_forwarding() handles missing handler without error."""
    forwarder = StatusEventForwarder()
    
    # Don't start forwarding, so _handler is None
    assert forwarder._handler is None
    
    # Should not raise exception
    await forwarder.stop_forwarding()
    
    assert forwarder._handler is None


@pytest.mark.asyncio
async def test_stop_forwarding_handles_remove_error():
    """Test that stop_forwarding() handles handler removal errors gracefully."""
    forwarder = StatusEventForwarder()
    
    await forwarder.start_forwarding("test_request")
    assert forwarder._handler is not None
    
    # Manually remove handler to simulate error condition
    status_bus.handlers.remove(forwarder._handler)
    
    # Should not raise exception (handler already removed)
    await forwarder.stop_forwarding()
    
    # Handler reference should still be cleared
    assert forwarder._handler is None


@pytest.mark.asyncio
async def test_direct_handler_receives_events():
    """Test that DirectStatusHandler receives and filters events correctly."""
    forwarder = StatusEventForwarder()
    
    await forwarder.start_forwarding("leak_test_req")
    
    # Publish a status event
    event1 = StatusEvent(
        server="test",
        request_id="leak_test_req",
        message="Event for test",
        phase=StatusPhase.PROGRESS
    )
    await status_bus.publish(event1)
    
    # Events should be immediately available (no background task)
    pending = forwarder.get_pending_events()
    assert len(pending) > 0, "Should receive events immediately"
    assert pending[0]["message"] == "Event for test"
    
    # Stop forwarding (remove handler)
    await forwarder.stop_forwarding()
    
    # Publish another event after handler removal
    event2 = StatusEvent(
        server="test",
        request_id="leak_test_req",
        message="Event after removal",
        phase=StatusPhase.PROGRESS
    )
    await status_bus.publish(event2)
    
    # Should not receive new events (handler removed)
    pending_after = forwarder.get_pending_events()
    assert len(pending_after) == 0, "Should not receive events after handler removal"


@pytest.mark.asyncio
async def test_direct_handler_no_background_task():
    """Test that new architecture doesn't create background tasks."""
    forwarder = StatusEventForwarder()
    
    await forwarder.start_forwarding("no_task_test")
    
    # Verify no background task exists (new architecture is synchronous)
    assert not hasattr(forwarder, 'forwarding_task'), \
        "New architecture should not have forwarding_task"
    
    # Events should be immediately available
    event = StatusEvent(
        server="test",
        request_id="no_task_test",
        message="Sync event",
        phase=StatusPhase.PROGRESS
    )
    await status_bus.publish(event)
    
    # No waiting needed - events are synchronously appended
    pending = forwarder.get_pending_events()
    assert len(pending) == 1, "Events should be immediately available"
    
    await forwarder.stop_forwarding()


@pytest.mark.asyncio
async def test_a_forwarded_event_carries_the_tree_that_says_what_ran_under_what():
    """The run's own stream is the one the chat reads, and it left the tree out.

    SSEStatusHandler has always sent it; this handler did not, so parent_id and
    depth_level never reached the page and the whole nesting half of the status
    display -- indentation, connectors, collapsing a sub-tree -- was inert on the
    path that actually feeds it. A sub-agent's lines sat flat between the parent's
    own with nothing saying they belonged to a run of their own.
    """
    forwarder = StatusEventForwarder()
    await forwarder.start_forwarding("root_req")
    try:
        # `_sub_001_001` is how a sub-agent's own coordinator is named; the hierarchy
        # is computed from the id by publish_status, not passed in here.
        await status_bus.publish(StatusEvent(
            server="sub_agent.coordinator",
            request_id="root_req_sub_001_001",
            message="working",
            phase=StatusPhase.START,
            parent_id="root_req_sub_001",
            depth_level=2,
        ))
        forwarded = forwarder.get_pending_events()
    finally:
        await forwarder.stop_forwarding()

    assert len(forwarded) == 1, forwarded
    tree = forwarded[0].get("tree")
    assert tree is not None, "the chat cannot nest what it is never told about"
    assert tree["parent_id"] == "root_req_sub_001"
    assert tree["depth_level"] == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
