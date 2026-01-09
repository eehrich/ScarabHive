import asyncio
from datetime import datetime

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
        assert len(bus._queue_handlers) == 1

        # Publish event with clean API
        event = StatusEvent(
            server="server1",
            request_id="req1", 
            message="message"
        )
        asyncio.run(bus.publish(event))

        # Should receive the event
        received = asyncio.run(queue.get())
        assert received == event
        assert queue.empty()


class TestPublishStatus:
    def test_publish_status_creates_event(self):
        """Test that publish_status creates and publishes a StatusEvent."""
        # Check that sequence counter increases after publish
        original_sequence = status_bus.sequence_counter
        
        # Publish status
        asyncio.run(publish_status("test_server", "test message", "req_123"))
        
        # Check that sequence counter increased
        assert status_bus.sequence_counter > original_sequence
