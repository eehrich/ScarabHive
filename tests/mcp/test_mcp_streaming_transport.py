"""
Tests for HTTP Streaming Transport (HTTPStreamingTransport)

These tests cover the HTTPStreamingTransport class which implements
SSE-based HTTP transport for MCP communication.
"""

import pytest
from unittest.mock import AsyncMock, patch

from agent_system.mcp.streaming_transport import HTTPStreamingTransport
from agent_system.mcp.core import MCPMessage


class TestHTTPStreamingTransport:
    """Test cases for HTTPStreamingTransport"""

    def test_init(self):
        """Test transport initialization"""
        transport = HTTPStreamingTransport(url="http://example.com")
        assert transport.url == "http://example.com"
        assert transport.timeout == 30.0
        assert transport.ssl_verify is True
        assert transport.session_id is None
        assert transport._connected is False

    def test_init_with_base_url(self):
        """Test transport initialization with base_url (backward compatibility)"""
        transport = HTTPStreamingTransport(base_url="http://example.com/")
        assert transport.url == "http://example.com/"
        assert transport.timeout == 30.0
        assert transport.ssl_verify is True

    def test_init_with_params(self):
        """Test transport initialization with parameters"""
        transport = HTTPStreamingTransport(
            url="http://example.com/",
            timeout=60.0,
            ssl_verify=False
        )
        assert transport.url == "http://example.com/"
        assert transport.timeout == 60.0
        assert transport.ssl_verify is False

    def test_init_requires_url(self):
        """Test that initialization requires url or base_url"""
        with pytest.raises(ValueError, match="Either url or base_url must be provided"):
            HTTPStreamingTransport()

    @pytest.mark.asyncio
    async def test_connect_sets_connected_flag(self):
        """Test that connect sets the connected flag"""
        transport = HTTPStreamingTransport(url="http://example.com")

        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_session = AsyncMock()
            mock_session_class.return_value = mock_session

            await transport.connect()

            assert transport._connected is True

    @pytest.mark.asyncio
    async def test_disconnect_clears_connected_flag(self):
        """Test that disconnect clears the connected flag"""
        transport = HTTPStreamingTransport(url="http://example.com")
        transport._connected = True

        await transport.disconnect()

        assert transport._connected is False
        assert transport.session_id is None

    @pytest.mark.asyncio
    async def test_close_aliases_disconnect(self):
        """Test that close is an alias for disconnect"""
        transport = HTTPStreamingTransport(url="http://example.com")
        transport._connected = True

        await transport.close()

        assert transport._connected is False

    @pytest.mark.asyncio
    async def test_send_message_requires_connection(self):
        """Test that send_message requires connection"""
        transport = HTTPStreamingTransport(url="http://example.com")

        message = MCPMessage(
            jsonrpc="2.0",
            method="test",
            params={},
            id=1
        )

        with pytest.raises(Exception, match="Not connected"):
            await transport.send_message(message)

    @pytest.mark.asyncio
    async def test_receive_message_not_implemented(self):
        """Test that receive_message raises NotImplementedError"""
        transport = HTTPStreamingTransport(url="http://example.com")

        with pytest.raises(NotImplementedError):
            await transport.receive_message()

    @pytest.mark.asyncio
    async def test_send_request_requires_connection(self):
        """Test that send_request requires connection"""
        transport = HTTPStreamingTransport(url="http://example.com")

        message = MCPMessage(
            jsonrpc="2.0",
            method="tools/list",
            params={},
            id=1
        )

        with pytest.raises(Exception, match="Not connected"):
            await transport.send_request(message)

    def test_build_headers(self):
        """Test header building"""
        transport = HTTPStreamingTransport(url="http://example.com")

        headers = transport._build_headers()
        assert headers['Content-Type'] == 'application/json'
        assert 'text/event-stream' in headers['Accept']
        assert 'application/json' in headers['Accept']

    def test_build_headers_with_session(self):
        """Test header building with session ID"""
        transport = HTTPStreamingTransport(url="http://example.com")
        transport.session_id = "test-session"

        headers = transport._build_headers(include_session=True)
        assert headers['Mcp-Session-Id'] == 'test-session'

    def test_message_to_dict(self):
        """Test message to dict conversion"""
        transport = HTTPStreamingTransport(url="http://example.com")

        message = MCPMessage(
            jsonrpc="2.0",
            method="tools/list",
            params={"test": "value"},
            id=123
        )

        result = transport._message_to_dict(message)
        assert result == {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {"test": "value"},
            "id": 123
        }

    def test_parse_json_response(self):
        """Test JSON response parsing"""
        transport = HTTPStreamingTransport(url="http://example.com")

        data = {
            "jsonrpc": "2.0",
            "result": {"tools": []},
            "id": 123
        }

        message = transport._parse_json_response(data)
        assert message.jsonrpc == "2.0"
        assert message.result == {"tools": []}
        assert message.id == 123

    def test_next_request_id(self):
        """Test request ID generation"""
        transport = HTTPStreamingTransport(url="http://example.com")

        id1 = transport._next_request_id()
        id2 = transport._next_request_id()

        assert id1 == 1
        assert id2 == 2