"""
Tests for HTTP Streaming Transport (HTTPStreamingTransport)

These tests cover the HTTPStreamingTransport class which implements
the MCP Streamable HTTP transport protocol.
"""

import pytest

from agent_system.mcp.streaming_transport import HTTPStreamingTransport
from agent_system.mcp.core import MCPMessage


class TestHTTPStreamingTransport:
    """Test cases for HTTPStreamingTransport"""

    def test_init_with_url(self):
        """Test transport initialization with url parameter"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp")
        assert transport.url == "http://example.com/mcp"
        assert transport.timeout == 30.0
        assert transport.ssl_verify is True
        assert transport.session_id is None
        assert transport._connected is False

    def test_init_with_base_url(self):
        """Test transport initialization with base_url parameter (backward compatibility)"""
        transport = HTTPStreamingTransport(base_url="http://example.com/mcp")
        assert transport.url == "http://example.com/mcp"

    def test_init_with_params(self):
        """Test transport initialization with custom parameters"""
        transport = HTTPStreamingTransport(
            url="http://example.com/mcp",
            timeout=60.0,
            ssl_verify=False
        )
        assert transport.timeout == 60.0
        assert transport.ssl_verify is False

    def test_init_without_url_raises(self):
        """Test that initialization without url raises ValueError"""
        with pytest.raises(ValueError, match="Either url or base_url must be provided"):
            HTTPStreamingTransport()

    @pytest.mark.asyncio
    async def test_connect(self):
        """Test connection establishment"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp")
        
        await transport.connect()
        
        assert transport._connected is True

    @pytest.mark.asyncio
    async def test_disconnect(self):
        """Test disconnection"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp")
        await transport.connect()
        
        await transport.disconnect()
        
        assert transport._connected is False

    @pytest.mark.asyncio
    async def test_close_alias(self):
        """Test that close() is an alias for disconnect()"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp")
        await transport.connect()
        
        await transport.close()
        
        assert transport._connected is False

    @pytest.mark.asyncio
    async def test_receive_message_not_implemented(self):
        """Test that receive_message raises NotImplementedError"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp")
        
        with pytest.raises(NotImplementedError, match="Streamable HTTP uses send_request"):
            await transport.receive_message()

    @pytest.mark.asyncio
    async def test_send_message_not_connected_raises(self):
        """Test that send_message raises when not connected"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp")
        
        message = MCPMessage(
            jsonrpc="2.0",
            method="notifications/test",
            params={}
        )
        
        with pytest.raises(Exception, match="Not connected"):
            await transport.send_message(message)

    @pytest.mark.asyncio
    async def test_send_request_not_connected_raises(self):
        """Test that send_request raises when not connected"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp")
        
        message = MCPMessage(
            jsonrpc="2.0",
            id="test_1",
            method="tools/list",
            params={}
        )
        
        with pytest.raises(Exception, match="Not connected"):
            await transport.send_request(message)

    def test_message_to_dict(self):
        """Test _message_to_dict helper method"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp")
        
        message = MCPMessage(
            jsonrpc="2.0",
            id="test_1",
            method="tools/list",
            params={"key": "value"}
        )
        
        result = transport._message_to_dict(message)
        
        assert result["jsonrpc"] == "2.0"
        assert result["id"] == "test_1"
        assert result["method"] == "tools/list"
        assert result["params"] == {"key": "value"}

    def test_build_headers_without_session(self):
        """Test _build_headers without session ID"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp")
        
        headers = transport._build_headers(include_session=False)
        
        assert headers["Content-Type"] == "application/json"
        assert headers["Accept"] == "application/json, text/event-stream"
        assert "Mcp-Session-Id" not in headers

    def test_build_headers_with_session(self):
        """Test _build_headers with session ID"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp")
        transport.session_id = "test-session-123"
        
        headers = transport._build_headers(include_session=True)
        
        assert headers["Mcp-Session-Id"] == "test-session-123"

    @pytest.mark.asyncio
    async def test_disconnect_without_sse_task(self):
        """Test that disconnect works when no SSE task is running"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp")
        await transport.connect()
        
        # No SSE task set
        assert transport._standalone_sse_task is None
        
        await transport.disconnect()
        
        assert transport._connected is False

    @pytest.mark.asyncio
    async def test_url_and_timeout_properties(self):
        """Test that url and timeout properties are accessible"""
        transport = HTTPStreamingTransport(url="http://example.com/mcp", timeout=45.0)
        assert transport.url == "http://example.com/mcp"
        assert transport.timeout == 45.0
