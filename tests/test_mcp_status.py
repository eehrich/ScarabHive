import pytest
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

from agent_system.mcp.status import StatusBus, StatusEvent, publish_status, status_bus


class TestStatusEvent:
    def test_status_event_creation(self):
        """Test StatusEvent dataclass creation."""
        timestamp = datetime.now()
        event = StatusEvent(
            server="test_server",
            request_id="req_123",
            message="Test message",
            timestamp=timestamp,
            level="info"
        )

        assert event.server == "test_server"
        assert event.request_id == "req_123"
        assert event.message == "Test message"
        assert event.timestamp == timestamp
        assert event.level == "info"


class TestStatusBus:
    def test_subscribe_without_filters(self):
        """Test subscribing without filters receives all events."""
        bus = StatusBus()

        # Subscribe without filters
        queue = asyncio.run(bus.subscribe())
        assert bus.get_subscriber_count() == 1

        # Publish event
        event = StatusEvent("server1", "req1", "message", datetime.now())
        asyncio.run(bus.publish(event))

        # Should receive the event
        received = asyncio.run(queue.get())
        assert received == event
        assert queue.empty()

    def test_subscribe_with_server_filter(self):
        """Test subscribing with server filter."""
        bus = StatusBus()

        # Subscribe to specific server
        queue = asyncio.run(bus.subscribe(server="server1"))

        # Publish matching event
        event1 = StatusEvent("server1", "req1", "message1", datetime.now())
        asyncio.run(bus.publish(event1))

        # Publish non-matching event
        event2 = StatusEvent("server2", "req2", "message2", datetime.now())
        asyncio.run(bus.publish(event2))

        # Should only receive matching event
        received = asyncio.run(queue.get())
        assert received == event1
        assert queue.empty()

    def test_subscribe_with_request_filter(self):
        """Test subscribing with request_id filter."""
        bus = StatusBus()

        # Subscribe to specific request
        queue = asyncio.run(bus.subscribe(request_id="req1"))

        # Publish matching event
        event1 = StatusEvent("server1", "req1", "message1", datetime.now())
        asyncio.run(bus.publish(event1))

        # Publish non-matching event
        event2 = StatusEvent("server1", "req2", "message2", datetime.now())
        asyncio.run(bus.publish(event2))

        # Should only receive matching event
        received = asyncio.run(queue.get())
        assert received == event1
        assert queue.empty()

    def test_subscribe_with_both_filters(self):
        """Test subscribing with both server and request filters."""
        bus = StatusBus()

        # Subscribe to specific server and request
        queue = asyncio.run(bus.subscribe(server="server1", request_id="req1"))

        # Publish matching event
        event1 = StatusEvent("server1", "req1", "message1", datetime.now())
        asyncio.run(bus.publish(event1))

        # Publish events that don't match both filters
        event2 = StatusEvent("server2", "req1", "message2", datetime.now())
        event3 = StatusEvent("server1", "req2", "message3", datetime.now())
        asyncio.run(bus.publish(event2))
        asyncio.run(bus.publish(event3))

        # Should only receive fully matching event
        received = asyncio.run(queue.get())
        assert received == event1
        assert queue.empty()

    def test_unsubscribe(self):
        """Test unsubscribing removes the subscriber."""
        bus = StatusBus()

        queue = asyncio.run(bus.subscribe())
        assert bus.get_subscriber_count() == 1

        bus.unsubscribe(queue)
        assert bus.get_subscriber_count() == 0

    def test_multiple_subscribers(self):
        """Test multiple subscribers receive events."""
        bus = StatusBus()

        queue1 = asyncio.run(bus.subscribe())
        queue2 = asyncio.run(bus.subscribe())
        assert bus.get_subscriber_count() == 2

        event = StatusEvent("server1", "req1", "message", datetime.now())
        asyncio.run(bus.publish(event))

        # Both should receive
        received1 = asyncio.run(queue1.get())
        received2 = asyncio.run(queue2.get())
        assert received1 == event
        assert received2 == event

    def test_publish_with_no_subscribers(self):
        """Test publishing with no subscribers doesn't crash."""
        bus = StatusBus()

        event = StatusEvent("server1", "req1", "message", datetime.now())
        # Should not raise
        asyncio.run(bus.publish(event))


class TestPublishStatus:
    def test_publish_status_creates_event(self):
        """Test publish_status convenience function."""
        # Mock the global bus publish
        original_publish = status_bus.publish
        status_bus.publish = AsyncMock()

        try:
            asyncio.run(publish_status("test_server", "Test message", "req_123", "warning"))

            # Check that publish was called with correct event
            status_bus.publish.assert_called_once()
            call_args = status_bus.publish.call_args[0][0]

            assert isinstance(call_args, StatusEvent)
            assert call_args.server == "test_server"
            assert call_args.request_id == "req_123"
            assert call_args.message == "Test message"
            assert call_args.level == "warning"
            assert isinstance(call_args.timestamp, datetime)

        finally:
            status_bus.publish = original_publish
