"""
Test status event streaming integration with MCP transport.

This test demonstrates how status events can be sent as MCP notifications
over the streaming HTTP transport.
"""

import pytest
import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock, Mock

from agent_system.mcp.status_streaming import MCPStatusStreamingTransport, MCPStatusNotificationHandler
from agent_system.mcp.status import StatusEvent, status_bus, publish_status


class TestMCPStatusStreaming:
    """Test MCP status event streaming functionality."""

    @pytest.mark.asyncio
    async def test_status_notification_creation(self):
        """Test converting StatusEvent to MCP notification."""
        # Create a test transport
        transport = MCPStatusStreamingTransport("http://localhost:8000")
        
        # Create a test status event
        event = StatusEvent(
            server="test_server",
            request_id="req_123",
            message="Test operation in progress",
            timestamp=datetime.now(),
            phase="progress",
            level="info",
            meta={"step": 1, "total": 5}
        )
        
        # Convert to MCP notification
        notification = transport._create_status_notification(event)
        
        # Verify the notification structure
        assert notification.jsonrpc == "2.0"
        assert notification.method == "notifications/status"
        assert notification.params["server"] == "test_server"
        assert notification.params["request_id"] == "req_123"
        assert notification.params["message"] == "Test operation in progress"
        assert notification.params["phase"] == "progress"
        assert notification.params["level"] == "info"
        assert notification.params["meta"]["step"] == 1
        assert notification.params["meta"]["total"] == 5

    @pytest.mark.asyncio
    async def test_status_streaming_transport_integration(self):
        """Test that status events are forwarded as MCP notifications."""
        transport = MCPStatusStreamingTransport("http://localhost:8000")
        
        # Mock the session and notification sending
        transport.session = MagicMock()
        transport.session_id = "test_session"
        
        sent_notifications = []
        
        async def mock_send_notification(notification):
            sent_notifications.append(notification)
        
        transport._send_status_notification = mock_send_notification
        
        # Start the transport (this will start status streaming)
        await transport.connect()
        
        # Wait a bit for the background task to start
        await asyncio.sleep(0.1)
        
        try:
            # Publish a status event
            await publish_status(
                server="test_plugin",
                message="Starting operation",
                request_id="test_req",
                phase="start",
                level="info"
            )
            
            # Wait for the notification to be processed
            await asyncio.sleep(0.1)
            
            # Verify that a notification was sent
            assert len(sent_notifications) >= 1
            
            notification = sent_notifications[-1]  # Get the last notification
            assert notification.method == "notifications/status"
            assert notification.params["server"] == "test_plugin"
            assert notification.params["message"] == "Starting operation"
            assert notification.params["request_id"] == "test_req"
            assert notification.params["phase"] == "start"
            
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_status_notification_handler(self):
        """Test MCP client-side status notification handling."""
        handler = MCPStatusNotificationHandler()
        
        received_events = []
        
        async def status_handler(server, request_id, message, timestamp, phase, level, meta):
            received_events.append({
                "server": server,
                "request_id": request_id,
                "message": message,
                "timestamp": timestamp,
                "phase": phase,
                "level": level,
                "meta": meta
            })
        
        handler.add_status_handler(status_handler)
        
        # Simulate receiving a status notification
        params = {
            "server": "web_scraper",
            "request_id": "req_456",
            "message": "Scraping page 3 of 10",
            "timestamp": "2025-01-15T10:30:00",
            "phase": "progress",
            "level": "info",
            "meta": {"page": 3, "total_pages": 10}
        }
        
        await handler.handle_status_notification(params)
        
        # Verify the handler was called
        assert len(received_events) == 1
        event = received_events[0]
        assert event["server"] == "web_scraper"
        assert event["request_id"] == "req_456"
        assert event["message"] == "Scraping page 3 of 10"
        assert event["phase"] == "progress"
        assert event["level"] == "info"
        assert event["meta"]["page"] == 3

    @pytest.mark.asyncio
    async def test_filtered_status_streaming(self):
        """Test status streaming with server and request_id filters."""
        # Create transport with filters
        transport = MCPStatusStreamingTransport(
            "http://localhost:8000",
            server_filter="specific_server",
            request_id_filter="specific_request"
        )
        
        # Mock session and subscription
        transport.session = MagicMock()
        transport.session_id = "test_session"
        
        # Mock the status bus subscription to return our filter
        original_subscribe = status_bus.subscribe
        
        async def mock_subscribe(server=None, request_id=None):
            # Verify filters are passed correctly
            assert server == "specific_server"
            assert request_id == "specific_request"
            return await original_subscribe(server=server, request_id=request_id)
        
        with patch.object(status_bus, 'subscribe', mock_subscribe):
            await transport.connect()
            
            # Verify subscription was called with correct filters
            assert transport._server_filter == "specific_server"
            assert transport._request_id_filter == "specific_request"
            
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_status_streaming_with_live_transport(self):
        """Integration test with actual HTTP transport (mocked HTTP calls)."""
        transport = MCPStatusStreamingTransport("http://localhost:8000")
        
        # Mock aiohttp session
        mock_session = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"mcp-session-id": "test_session_123"}
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {"capabilities": {}}
        }
        
        # Create an async context manager that yields mock_response
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_response
        mock_session.post = Mock(return_value=mock_cm)
        
        # Start transport first; if it created a real session, close it before
        # replacing with our mocked session to avoid leaving sessions open.
        await transport.connect()
        # If a real session was created, disconnect to close it
        if transport.session and not isinstance(transport.session, AsyncMock):
            try:
                await transport.disconnect()
            except Exception:
                pass
        transport.session = mock_session
        
        try:
            # Create and send a status notification
            event = StatusEvent(
                server="test_server",
                request_id="req_789",
                message="Processing data",
                timestamp=datetime.now(),
                phase="progress",
                level="info"
            )
            
            notification = transport._create_status_notification(event)
            await transport._send_status_notification(notification)
            
            # Verify HTTP POST was called for the notification
            call_args = mock_session.post.call_args
            
            # Verify the payload structure
            payload = call_args[1]["json"]  # kwargs["json"]
            assert payload["jsonrpc"] == "2.0"
            assert payload["method"] == "notifications/status"
            assert payload["params"]["server"] == "test_server"
            assert payload["params"]["message"] == "Processing data"
            
        finally:
            await transport.disconnect()

    def test_status_event_to_json_serialization(self):
        """Test that status events can be properly serialized to JSON for MCP."""
        event = StatusEvent(
            server="json_test_server",
            request_id="json_req_123",
            message="JSON serialization test",
            timestamp=datetime(2025, 1, 15, 10, 30, 45),
            phase="end",
            level="info",
            meta={"result": "success", "duration": 1.5}
        )
        
        # Convert to MCP notification params
        params = {
            "server": event.server,
            "request_id": event.request_id,
            "message": event.message,
            "timestamp": event.timestamp.isoformat(),
            "phase": event.phase,
            "level": event.level,
            "meta": event.meta
        }
        
        # Verify JSON serialization works
        json_str = json.dumps(params)
        parsed = json.loads(json_str)
        
        assert parsed["server"] == "json_test_server"
        assert parsed["request_id"] == "json_req_123"
        assert parsed["timestamp"] == "2025-01-15T10:30:45"
        assert parsed["meta"]["result"] == "success"
        assert parsed["meta"]["duration"] == 1.5

    @pytest.mark.asyncio
    async def test_multiple_status_handlers(self):
        """Test that multiple handlers can process the same status notification."""
        handler = MCPStatusNotificationHandler()
        
        handler1_calls = []
        handler2_calls = []
        
        async def handler1(server, request_id, message, timestamp, phase, level, meta):
            handler1_calls.append(f"Handler1: {server} - {message}")
        
        async def handler2(server, request_id, message, timestamp, phase, level, meta):
            handler2_calls.append(f"Handler2: {server} - {phase}")
        
        handler.add_status_handler(handler1)
        handler.add_status_handler(handler2)
        
        # Send notification
        params = {
            "server": "multi_handler_test",
            "request_id": "multi_req",
            "message": "Testing multiple handlers",
            "timestamp": datetime.now().isoformat(),
            "phase": "progress",
            "level": "info",
            "meta": None
        }
        
        await handler.handle_status_notification(params)
        
        # Verify both handlers were called
        assert len(handler1_calls) == 1
        assert len(handler2_calls) == 1
        assert "multi_handler_test" in handler1_calls[0]
        assert "progress" in handler2_calls[0]