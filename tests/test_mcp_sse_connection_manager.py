"""
Tests for SSE Connection Manager (SSEConnectionManager)

These tests cover the SSEConnectionManager class which manages
persistent SSE connections for MCP server communication.
"""

import pytest
import asyncio
import json
from unittest.mock import AsyncMock, patch

from agent_system.mcp.streaming_transport import SSEConnectionManager, ConnectionStatus
from agent_system.mcp.core import MCPMessage


class TestSSEConnectionManager:
    """Test cases for SSEConnectionManager"""

    def test_init(self):
        """Test connection manager initialization"""
        manager = SSEConnectionManager()
        assert manager.timeout == 30.0
        assert manager.max_reconnect_attempts == 3
        assert manager.connections == {}
        assert manager._next_connection_id == 0

    def test_init_with_params(self):
        """Test connection manager initialization with parameters"""
        manager = SSEConnectionManager(timeout=60.0, max_reconnect_attempts=5)
        assert manager.timeout == 60.0
        assert manager.max_reconnect_attempts == 5

    def test_generate_connection_id(self):
        """Test connection ID generation"""
        manager = SSEConnectionManager()
        id1 = manager._generate_connection_id()
        id2 = manager._generate_connection_id()

        assert id1 == "sse_conn_1"
        assert id2 == "sse_conn_2"
        assert manager._next_connection_id == 2

    def test_get_connection_status_unknown(self):
        """Test getting status of unknown connection"""
        manager = SSEConnectionManager()
        status = manager.get_connection_status("unknown")
        assert status == ConnectionStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """Test successful connection establishment"""
        manager = SSEConnectionManager()

        # Mock the entire connect process since aiohttp mocking is complex
        with patch.object(manager, '_start_sse_stream', new_callable=AsyncMock) as mock_start_stream:
            with patch('aiohttp.ClientSession'):
                connection_id = await manager.connect("http://example.com/mcp")

                assert connection_id.startswith("sse_conn_")
                assert connection_id in manager.connections
                assert manager.connections[connection_id]["status"] == ConnectionStatus.CONNECTED
                assert manager.connections[connection_id]["url"] == "http://example.com/mcp"
                mock_start_stream.assert_called_once_with(connection_id)

    @pytest.mark.asyncio
    async def test_connect_failure(self):
        """Test connection failure"""
        manager = SSEConnectionManager()

        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_session_class.side_effect = Exception("Connection failed")

            with pytest.raises(Exception, match="Connection failed"):
                await manager.connect("http://example.com/mcp")

    @pytest.mark.asyncio
    async def test_send_request_connection_not_found(self):
        """Test sending request with unknown connection"""
        manager = SSEConnectionManager()

        message = MCPMessage(
            jsonrpc="2.0",
            id="test_id",
            method="tools/list",
            params={}
        )

        with pytest.raises(ValueError, match="Unknown connection"):
            await manager.send_request("unknown_conn", message)

    @pytest.mark.asyncio
    async def test_send_request_not_connected(self):
        """Test sending request when not connected"""
        manager = SSEConnectionManager()

        # Set up a disconnected connection
        connection_id = "test_conn"
        manager.connections[connection_id] = {
            "url": "http://example.com/mcp",
            "status": ConnectionStatus.DISCONNECTED,
            "session": None,
            "response": None,
            "pending_requests": {},
            "reconnect_attempts": 0,
            "last_activity": None
        }

        message = MCPMessage(
            jsonrpc="2.0",
            id="test_id",
            method="tools/list",
            params={}
        )

        with pytest.raises(Exception, match="is not connected"):
            await manager.send_request(connection_id, message)

    @pytest.mark.asyncio
    async def test_disconnect(self):
        """Test connection disconnect"""
        manager = SSEConnectionManager()

        # Set up a mock connection
        connection_id = "test_conn"
        mock_session = AsyncMock()
        mock_future = asyncio.Future()  # Use real Future
        manager.connections[connection_id] = {
            "url": "http://example.com/mcp",
            "status": ConnectionStatus.CONNECTED,
            "session": mock_session,
            "response": None,
            "pending_requests": {"req1": mock_future},
            "reconnect_attempts": 0,
            "last_activity": None
        }

        await manager.disconnect(connection_id)

        # Check that session was closed
        mock_session.close.assert_called_once()

        # Check that connection was removed
        assert connection_id not in manager.connections

    @pytest.mark.asyncio
    async def test_disconnect_all(self):
        """Test disconnecting all connections"""
        manager = SSEConnectionManager()

        # Set up multiple mock connections
        for i in range(3):
            conn_id = f"conn_{i}"
            mock_session = AsyncMock()
            manager.connections[conn_id] = {
                "url": f"http://example{i}.com/mcp",
                "status": ConnectionStatus.CONNECTED,
                "session": mock_session,
                "response": None,
                "pending_requests": {},
                "reconnect_attempts": 0,
                "last_activity": None
            }

        await manager.disconnect_all()

        # Check that all sessions were closed
        for conn_info in manager.connections.values():
            conn_info["session"].close.assert_called_once()

        # Check that all connections were removed
        assert len(manager.connections) == 0

    @pytest.mark.asyncio
    async def test_handle_sse_event_success(self):
        """Test successful SSE event handling"""
        manager = SSEConnectionManager()

        # Set up a mock connection with pending request
        connection_id = "test_conn"
        future = asyncio.Future()
        manager.connections[connection_id] = {
            "url": "http://example.com/mcp",
            "status": ConnectionStatus.CONNECTED,
            "session": AsyncMock(),
            "response": None,
            "pending_requests": {"test_id": future},
            "reconnect_attempts": 0,
            "last_activity": None
        }

        # Mock asyncio.get_event_loop().time()
        with patch('asyncio.get_event_loop') as mock_loop:
            mock_loop.return_value.time.return_value = 123456.789

            # Handle SSE event
            event_data = {
                "jsonrpc": "2.0",
                "id": "test_id",
                "result": {"tools": [{"name": "test_tool"}]}
            }

            await manager._handle_sse_event(connection_id, json.dumps(event_data))

            # Check that future was resolved
            assert future.done()
            result = future.result()
            assert result.id == "test_id"
            assert result.result == {"tools": [{"name": "test_tool"}]}

            # Check that request was removed from pending
            assert "test_id" not in manager.connections[connection_id]["pending_requests"]

            # Check that last_activity was updated
            assert manager.connections[connection_id]["last_activity"] == 123456.789

    @pytest.mark.asyncio
    async def test_handle_sse_event_error_response(self):
        """Test SSE event handling with error response"""
        manager = SSEConnectionManager()

        # Set up a mock connection with pending request
        connection_id = "test_conn"
        future = asyncio.Future()
        manager.connections[connection_id] = {
            "url": "http://example.com/mcp",
            "status": ConnectionStatus.CONNECTED,
            "session": AsyncMock(),
            "response": None,
            "pending_requests": {"test_id": future},
            "reconnect_attempts": 0,
            "last_activity": None
        }

        # Handle SSE error event
        event_data = {
            "jsonrpc": "2.0",
            "id": "test_id",
            "error": {
                "code": -32000,
                "message": "Server error",
                "data": {"details": "Something went wrong"}
            }
        }

        await manager._handle_sse_event(connection_id, json.dumps(event_data))

        # Check that future was resolved with error
        assert future.done()
        result = future.result()
        assert result.id == "test_id"
        assert result.error is not None
        assert result.error.code == -32000
        assert result.error.message == "Server error"

    @pytest.mark.asyncio
    async def test_handle_sse_event_invalid_json(self):
        """Test SSE event handling with invalid JSON"""
        manager = SSEConnectionManager()

        # Set up a mock connection
        connection_id = "test_conn"
        manager.connections[connection_id] = {
            "url": "http://example.com/mcp",
            "status": ConnectionStatus.CONNECTED,
            "session": AsyncMock(),
            "response": None,
            "pending_requests": {},
            "reconnect_attempts": 0,
            "last_activity": None
        }

        # Handle invalid JSON - should not raise exception
        await manager._handle_sse_event(connection_id, "invalid json")

        # Connection should still be connected
        assert manager.connections[connection_id]["status"] == ConnectionStatus.CONNECTED