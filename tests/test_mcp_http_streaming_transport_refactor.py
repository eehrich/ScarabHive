"""
Test for refactored HTTPStreamingTransport with SSE integration
"""

import pytest
from unittest.mock import AsyncMock, patch

from agent_system.mcp.streaming_transport import HTTPStreamingTransport
from agent_system.mcp.core import MCPMessage


@pytest.mark.asyncio
async def test_http_streaming_transport_init():
    """Test HTTPStreamingTransport initialization with SSE manager"""
    transport = HTTPStreamingTransport("http://example.com", timeout=60.0, ssl_verify=False)

    assert transport.base_url == "http://example.com"
    assert transport.timeout == 60.0
    assert transport.ssl_verify is False
    assert transport.sse_manager is not None
    assert transport.connection_id is None


@pytest.mark.asyncio
async def test_http_streaming_transport_connect():
    """Test connection establishment"""
    transport = HTTPStreamingTransport("http://example.com")

    with patch.object(transport.sse_manager, 'connect', new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = "test_conn_1"

        await transport.connect()

        assert transport.connection_id == "test_conn_1"
        mock_connect.assert_called_once_with("http://example.com/mcp")


@pytest.mark.asyncio
async def test_http_streaming_transport_disconnect():
    """Test connection disconnect"""
    transport = HTTPStreamingTransport("http://example.com")
    transport.connection_id = "test_conn_1"

    with patch.object(transport.sse_manager, 'disconnect', new_callable=AsyncMock) as mock_disconnect:
        await transport.disconnect()

        mock_disconnect.assert_called_once_with("test_conn_1")
        assert transport.connection_id is None


@pytest.mark.asyncio
async def test_http_streaming_transport_send_request():
    """Test sending request through SSE manager"""
    transport = HTTPStreamingTransport("http://example.com")
    transport.connection_id = "test_conn_1"

    message = MCPMessage(
        jsonrpc="2.0",
        id="test_123",
        method="tools/list",
        params={}
    )

    expected_response = MCPMessage(
        jsonrpc="2.0",
        id="test_123",
        result={"tools": []}
    )

    with patch.object(transport.sse_manager, 'send_request', new_callable=AsyncMock) as mock_send:
        mock_send.return_value = expected_response

        response = await transport.send_request(message)

        assert response.id == "test_123"
        assert response.result == {"tools": []}
        mock_send.assert_called_once_with("test_conn_1", message)


@pytest.mark.asyncio
async def test_http_streaming_transport_send_request_auto_connect():
    """Test that send_request automatically connects if not connected"""
    transport = HTTPStreamingTransport("http://example.com")

    message = MCPMessage(
        jsonrpc="2.0",
        id="test_123",
        method="tools/list",
        params={}
    )

    expected_response = MCPMessage(
        jsonrpc="2.0",
        id="test_123",
        result={"tools": []}
    )

    with patch.object(transport.sse_manager, 'connect', new_callable=AsyncMock) as mock_connect, \
         patch.object(transport.sse_manager, 'send_request', new_callable=AsyncMock) as mock_send:

        mock_connect.return_value = "test_conn_1"
        mock_send.return_value = expected_response

        response = await transport.send_request(message)

        assert transport.connection_id == "test_conn_1"
        mock_connect.assert_called_once_with("http://example.com/mcp")
        mock_send.assert_called_once_with("test_conn_1", message)
        assert response.result == {"tools": []}