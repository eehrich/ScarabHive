"""
Tests for HTTP Streaming Transport (HTTPStreamingTransport)

These tests cover the HTTPStreamingTransport class which implements
SSE-based HTTP transport for MCP communication.
"""

import json
import base64
import pytest
from unittest.mock import AsyncMock, Mock, patch
from aiohttp import ClientTimeout, TCPConnector
from aiohttp.client_exceptions import ClientError

from agent_system.mcp.streaming_transport import HTTPStreamingTransport
from agent_system.mcp.core import MCPMessage


class TestHTTPStreamingTransport:
    """Test cases for HTTPStreamingTransport"""

    def test_init(self):
        """Test transport initialization"""
        transport = HTTPStreamingTransport("http://example.com")
        assert transport.base_url == "http://example.com"
        assert transport.config == {}
        assert transport.timeout == 30.0
        assert transport.ssl_verify is True
        assert transport.session is None
        assert transport.session_id is None

    def test_init_with_config(self):
        """Test transport initialization with config"""
        config = {"key": "value", "timeout": 15}
        transport = HTTPStreamingTransport(
            "http://example.com/", 
            config=config, 
            timeout=60.0, 
            ssl_verify=False
        )
        assert transport.base_url == "http://example.com"
        assert transport.config == config
        assert transport.timeout == 60.0
        assert transport.ssl_verify is False

    def test_build_url_without_config(self):
        """Test URL building without config"""
        transport = HTTPStreamingTransport("http://example.com")
        url = transport._build_url()
        assert url == "http://example.com/mcp"

    def test_build_url_with_config(self):
        """Test URL building with config"""
        config = {"param": "value", "number": 42}
        transport = HTTPStreamingTransport("http://example.com", config=config)
        url = transport._build_url()
        
        # Extract and decode config from URL
        assert url.startswith("http://example.com/mcp?config=")
        config_b64 = url.split("config=")[1]
        decoded_config = json.loads(base64.b64decode(config_b64).decode())
        assert decoded_config == config

    @pytest.mark.asyncio
    async def test_connect_creates_session(self):
        """Test that connect creates an aiohttp session"""
        transport = HTTPStreamingTransport("http://example.com")
        
        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_session = AsyncMock()
            mock_session_class.return_value = mock_session
            
            await transport.connect()
            
            # Verify session was created with correct parameters
            mock_session_class.assert_called_once()
            call_kwargs = mock_session_class.call_args[1]
            
            assert isinstance(call_kwargs['connector'], TCPConnector)
            assert isinstance(call_kwargs['timeout'], ClientTimeout)
            assert call_kwargs['headers']['Content-Type'] == 'application/json'
            assert 'text/event-stream' in call_kwargs['headers']['Accept']
            
            assert transport.session == mock_session

    @pytest.mark.asyncio
    async def test_connect_idempotent(self):
        """Test that multiple connect calls don't create multiple sessions"""
        transport = HTTPStreamingTransport("http://example.com")
        
        with patch('aiohttp.ClientSession') as mock_session_class:
            mock_session = AsyncMock()
            mock_session_class.return_value = mock_session
            
            await transport.connect()
            first_session = transport.session
            
            await transport.connect()
            assert transport.session == first_session
            
            # Session should only be created once
            mock_session_class.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_closes_session(self):
        """Test that disconnect closes the session"""
        transport = HTTPStreamingTransport("http://example.com")
        
        # Mock session
        mock_session = AsyncMock()
        transport.session = mock_session
        transport.session_id = "test-session"
        
        await transport.disconnect()
        
        mock_session.close.assert_called_once()
        assert transport.session is None
        assert transport.session_id is None

    @pytest.mark.asyncio
    async def test_disconnect_handles_exception(self):
        """Test that disconnect handles session close exceptions gracefully"""
        transport = HTTPStreamingTransport("http://example.com")
        
        # Mock session that raises exception on close
        mock_session = AsyncMock()
        mock_session.close.side_effect = Exception("Close failed")
        transport.session = mock_session
        
        # Should not raise exception
        await transport.disconnect()
        
        assert transport.session is None
        assert transport.session_id is None

    @pytest.mark.asyncio
    async def test_receive_message_not_implemented(self):
        """Test that receive_message raises NotImplementedError"""
        transport = HTTPStreamingTransport("http://example.com")
        
        with pytest.raises(NotImplementedError):
            await transport.receive_message()

    def test_config_base64_encoding_decoding(self):
        """Test that config is properly encoded and can be decoded"""
        config = {
            "complex_data": {
                "nested": ["array", "values"],
                "unicode": "test ñ 中文",
                "numbers": 42.5
            }
        }
        
        transport = HTTPStreamingTransport("http://example.com", config=config)
        url = transport._build_url()
        
        # Extract and verify config
        config_b64 = url.split("config=")[1]
        decoded_config = json.loads(base64.b64decode(config_b64).decode())
        assert decoded_config == config

    @pytest.mark.asyncio
    async def test_ssl_verification_config(self):
        """Test transport with SSL verification configuration"""
        transport = HTTPStreamingTransport("https://example.com", ssl_verify=False)
        
        with patch('aiohttp.ClientSession') as mock_session_class:
            await transport.connect()
            
            # Verify TCPConnector was created
            call_kwargs = mock_session_class.call_args[1]
            connector = call_kwargs['connector']
            assert isinstance(connector, TCPConnector)

    @pytest.mark.asyncio
    async def test_connection_error_handling(self):
        """Test handling of connection errors"""
        transport = HTTPStreamingTransport("http://example.com")
        
        # Mock session that raises connection error
        mock_session = Mock()  # Use regular Mock, not AsyncMock
        # Mock post to raise exception when called as context manager
        mock_context = AsyncMock()
        mock_context.__aenter__.side_effect = ClientError("Connection failed")
        mock_session.post.return_value = mock_context
        transport.session = mock_session
        
        message = MCPMessage(
            jsonrpc="2.0",
            method="tools/list",
            params={},
            id=1
        )
        
        response = await transport.send_request(message)
        
        # Should return connection error
        assert response.error is not None
        assert response.error.code == -32000
        assert "Request failed" in response.error.message

    @pytest.mark.asyncio
    async def test_send_initialized_notification_no_session(self):
        """Test initialized notification when no session exists"""
        transport = HTTPStreamingTransport("http://example.com")
        
        # No session setup - should return silently without making requests
        await transport._send_initialized_notification()

    @pytest.mark.asyncio
    async def test_send_initialized_notification_handles_error(self):
        """Test initialized notification error handling"""
        transport = HTTPStreamingTransport("http://example.com")
        
        # Mock session that raises exception
        mock_session = Mock()  # Use regular Mock, not AsyncMock
        # Mock post to raise exception when entering the context manager
        mock_context = AsyncMock()
        mock_context.__aenter__.side_effect = ClientError("Connection failed")
        mock_session.post.return_value = mock_context
        transport.session = mock_session
        transport.session_id = "test-session"
        
        # Should not raise exception
        await transport._send_initialized_notification()

    def test_base_url_stripping(self):
        """Test that trailing slashes are stripped from base URL"""
        transport = HTTPStreamingTransport("http://example.com/path/")
        assert transport.base_url == "http://example.com/path"

    def test_empty_config_handling(self):
        """Test handling of None config"""
        transport = HTTPStreamingTransport("http://example.com", config=None)
        assert transport.config == {}

    @pytest.mark.asyncio
    async def test_session_none_during_disconnect(self):
        """Test disconnect when session is already None"""
        transport = HTTPStreamingTransport("http://example.com")
        
        # No session set
        await transport.disconnect()
        
        # Should not raise exception
        assert transport.session is None
        assert transport.session_id is None

    def test_url_building_edge_cases(self):
        """Test URL building with various edge cases"""
        # Empty config
        transport = HTTPStreamingTransport("http://example.com", config={})
        url = transport._build_url()
        assert url == "http://example.com/mcp"
        
        # Config with special characters
        config = {"key": "value with spaces & symbols!"}
        transport = HTTPStreamingTransport("http://example.com", config=config)
        url = transport._build_url()
        assert "config=" in url
        
        # Verify we can decode the special characters
        config_b64 = url.split("config=")[1]
        decoded_config = json.loads(base64.b64decode(config_b64).decode())
        assert decoded_config == config

    def test_various_ssl_verify_settings(self):
        """Test various SSL verification settings"""
        # SSL verification enabled (default)
        transport1 = HTTPStreamingTransport("https://example.com")
        assert transport1.ssl_verify is True
        
        # SSL verification explicitly disabled
        transport2 = HTTPStreamingTransport("https://example.com", ssl_verify=False)
        assert transport2.ssl_verify is False

    def test_timeout_settings(self):
        """Test various timeout settings"""
        # Default timeout
        transport1 = HTTPStreamingTransport("http://example.com")
        assert transport1.timeout == 30.0
        
        # Custom timeout
        transport2 = HTTPStreamingTransport("http://example.com", timeout=60.0)
        assert transport2.timeout == 60.0

    def test_complex_config_serialization(self):
        """Test complex configuration serialization"""
        config = {
            "nested": {
                "array": [1, 2, {"inner": "value"}],
                "boolean": True,
                "null": None,
                "number": 42.5
            },
            "unicode": "测试中文",
            "empty_list": [],
            "empty_dict": {}
        }
        
        transport = HTTPStreamingTransport("http://example.com", config=config)
        url = transport._build_url()
        
        # Extract and verify complex config
        config_b64 = url.split("config=")[1]
        decoded_config = json.loads(base64.b64decode(config_b64).decode())
        assert decoded_config == config