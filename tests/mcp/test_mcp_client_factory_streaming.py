"""
Tests for MCP Client Factory streaming client creation
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from agent_system.mcp.client import MCPClientFactory


class TestMCPClientFactoryStreaming:
    """Test streaming client creation (always uses SSE)"""

    @pytest.mark.asyncio
    async def test_create_streaming_client(self):
        """Test streaming client creation with SSE transport"""
        with patch('agent_system.mcp.client.HTTPStreamingTransport') as mock_transport_class, \
             patch('agent_system.mcp.client.StandardMCPClient') as mock_client_class:

            mock_transport = MagicMock()
            mock_transport_class.return_value = mock_transport

            mock_client = MagicMock()
            mock_client.connect = AsyncMock()
            mock_client.initialize = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await MCPClientFactory.create_streaming_client("http://example.com")

            # Should create transport with use_sse=True (always for streaming)
            mock_transport_class.assert_called_once_with(
                base_url="http://example.com",
                timeout=30.0,
                ssl_verify=True,
                use_sse=True
            )

            # Should create and connect client successfully
            mock_client_class.assert_called_once_with(mock_transport, "AgentSystem", None)
            mock_client.connect.assert_called_once()
            mock_client.initialize.assert_called_once()
            assert result == mock_client