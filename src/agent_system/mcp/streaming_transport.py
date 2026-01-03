"""
MCP Streamable HTTP Transport Implementation

Implements the MCP Streamable HTTP transport protocol as specified in:
https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http

Key Protocol Features:
- Every client message is sent as a separate HTTP POST request
- Server can respond with either:
  1. Content-Type: application/json (immediate single response)
  2. Content-Type: text/event-stream (SSE stream with multiple messages)
- Optional: Client can open standalone SSE stream via HTTP GET for server-initiated messages
- Session management via Mcp-Session-Id header

This transport does NOT use persistent SSE streams that remain open across multiple requests.
Each POST is independent and may open its own short-lived SSE stream for the response.

COMPLIANCE STATUS:
✅ REQUIRED: POST requests for all messages
✅ REQUIRED: Accept header with application/json, text/event-stream
✅ REQUIRED: Handle 202 Accepted for notifications-only
✅ REQUIRED: Handle JSON and SSE responses for requests
✅ REQUIRED: Session management with Mcp-Session-Id header
✅ REQUIRED: Standalone SSE stream support (GET requests)
✅ REQUIRED: Proper SSE stream consumption
❌ OPTIONAL: Message batching (arrays of messages)
❌ OPTIONAL: SSE resumability (event IDs, Last-Event-ID header)
❌ OPTIONAL: Server-initiated messages in request SSE streams
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import aiohttp

from .core import MCPTransport, MCPMessage, MCPError

logger = logging.getLogger(__name__)


class HTTPStreamingTransport(MCPTransport):
    """
    MCP Streamable HTTP transport implementation.
    
    Protocol flow:
    1. POST initialize request → server returns Mcp-Session-Id header
    2. All subsequent POSTs include Mcp-Session-Id header
    3. Each POST can return either:
       - JSON response (simple case)
       - SSE stream with response(s)
    4. Optional: GET request opens standalone SSE stream for server messages
    """

    def __init__(self, url: Optional[str] = None, base_url: Optional[str] = None, timeout: float = 30.0, ssl_verify: bool = True, use_sse: bool = True,
                 connection_limit: int = 10, connection_limit_per_host: int = 5):
        """
        Initialize Streamable HTTP transport.
        
        Args:
            url: MCP endpoint URL (supports both POST and GET) - preferred parameter
            base_url: Alias for url (for backward compatibility)
            timeout: Request timeout in seconds
            ssl_verify: Whether to verify SSL certificates
            use_sse: Ignored (kept for backward compatibility) - SSE mode is auto-detected per response
            connection_limit: Total HTTP connection limit for connection pooling
            connection_limit_per_host: HTTP connection limit per host
        """
        # Accept both url and base_url for backward compatibility
        if url is None and base_url is None:
            raise ValueError("Either url or base_url must be provided")
        self.url: str = url or base_url  # type: ignore[assignment]  # Either url or base_url is guaranteed non-None
        self.timeout = timeout
        self.ssl_verify = ssl_verify
        self.connection_limit = connection_limit
        self.connection_limit_per_host = connection_limit_per_host
        self.session_id: Optional[str] = None
        self._request_counter = 0
        self._standalone_sse_task: Optional[asyncio.Task] = None
        self._connected = False

    async def connect(self) -> None:
        """
        Mark transport as connected.
        
        Note: We don't create a persistent session here because send_request()
        creates fresh sessions for each request to avoid connection pool issues
        with SSE streams. This approach prevents resource leaks and ensures
        clean HTTP connection management.
        """
        self._connected = True
        logger.info(f"HTTP streaming transport connected to {self.url}")

    async def disconnect(self) -> None:
        """Close transport and any open SSE streams"""
        self._connected = False
        
        # Cancel standalone SSE stream if running
        if self._standalone_sse_task and not self._standalone_sse_task.done():
            self._standalone_sse_task.cancel()
            try:
                await self._standalone_sse_task
            except asyncio.CancelledError:
                pass
        
        logger.info("HTTP streaming transport disconnected")

    async def close(self) -> None:
        """Alias for disconnect() - close the transport"""
        await self.disconnect()

    async def send_message(self, message: MCPMessage) -> None:
        """
        Send a message without expecting a response (notifications).
        
        Per spec: If input is solely notifications, server returns 202 Accepted.
        """
        if not self._connected:
            raise Exception("Not connected - call connect() first")
        
        # Create fresh session for this notification
        connector = aiohttp.TCPConnector(ssl=self.ssl_verify, limit=self.connection_limit, limit_per_host=self.connection_limit_per_host)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            headers = self._build_headers(include_session=True)
            payload = self._message_to_dict(message)
            
            async with session.post(self.url, json=payload, headers=headers) as response:
                if response.status == 202:
                    # Accepted - notification received
                    logger.debug(f"Notification accepted: {message.method}")
                    return
                elif response.status >= 400:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")

    async def receive_message(self) -> MCPMessage:
        """
        Receive a message (not applicable for Streamable HTTP).
        
        Streamable HTTP uses request/response model, not bidirectional streaming.
        Server-initiated messages come via standalone SSE stream.
        """
        raise NotImplementedError(
            "Streamable HTTP uses send_request() for request/response. "
            "Server-initiated messages require opening standalone SSE stream via start_standalone_sse()"
        )

    async def send_request(self, request: MCPMessage) -> MCPMessage:
        """
        Send a request and wait for response.
        
        Per spec:
        1. POST the request to MCP endpoint
        2. Server responds with either:
           a) Content-Type: application/json → immediate response
           b) Content-Type: text/event-stream → SSE stream with response(s)
        
        Note: Create a fresh session for each request to avoid connection reuse issues
        with aiohttp when consuming SSE streams. This prevents "unclosed client session"
        warnings and ensures proper resource cleanup.
        """
        if not self._connected:
            raise Exception("Not connected - call connect() first")
        
        # Create fresh session for this request to avoid connection pool issues
        connector = aiohttp.TCPConnector(ssl=self.ssl_verify, limit=self.connection_limit, limit_per_host=self.connection_limit_per_host)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            headers = self._build_headers(include_session=True)
            payload = self._message_to_dict(request)
            
            logger.debug(f"POST {request.method} (id={request.id}) to {self.url}")
            
            async with session.post(self.url, json=payload, headers=headers) as response:
                # Check for session ID in response (initialize response)
                if 'mcp-session-id' in response.headers:
                    new_session_id = response.headers['mcp-session-id']
                    if not self.session_id:
                        self.session_id = new_session_id
                        logger.debug(f"Got session ID: {self.session_id}")
                
                content_type = response.headers.get('Content-Type', '')
                
                if response.status == 400:
                    error_text = await response.text()
                    raise Exception(f"Bad Request (400): {error_text}")
                elif response.status == 404:
                    raise Exception("Not Found (404): Session may have expired")
                elif response.status == 405:
                    raise Exception("Method Not Allowed (405): Server doesn't support POST")
                elif response.status >= 400:
                    error_text = await response.text()
                    raise Exception(f"HTTP {response.status}: {error_text}")
                
                # Handle JSON response (immediate)
                if 'application/json' in content_type:
                    response_data = await response.json()
                    logger.debug(f"Got JSON response for request {request.id}")
                    return self._parse_json_response(response_data)
                
                # Handle SSE stream response
                elif 'text/event-stream' in content_type:
                    logger.debug(f"Got SSE stream for request {request.id}")
                    return await self._read_sse_response(response, request.id)
                
                else:
                    raise Exception(f"Unexpected Content-Type: {content_type}")

    async def _read_sse_response(self, response: aiohttp.ClientResponse, request_id: Any) -> MCPMessage:
        """
        Read SSE stream and extract the response for our request.
        
        The stream may contain multiple events, but we're looking for the one
        with matching ID (the JSON-RPC response to our request).
        
        Important: We must fully consume the stream to avoid leaving the HTTP
        connection in a bad state. Even after finding our response, we continue
        reading until the stream ends to prevent connection pool issues.
        
        SSE Format:
        - event: <type> (optional event type)
        - data: <json> (the actual JSON-RPC message)
        """
        response_message = None
        
        async for line in response.content:
            decoded = line.decode('utf-8').strip()
            if not decoded:
                continue
            
            # Parse SSE format
            if decoded.startswith('event:'):
                # Event type - currently not used but part of SSE spec
                pass
            elif decoded.startswith('data:'):
                data_str = decoded[5:].strip()
                
                try:
                    event_data = json.loads(data_str)
                    
                    # Check if this is the response to our request
                    if event_data.get('id') == request_id:
                        response_message = self._parse_json_response(event_data)
                        # Don't break - we need to consume the rest of the stream
                        # to avoid leaving the connection in a bad state
                    else:
                        # Other message (notification, server request, etc.)
                        logger.debug(f"Received other SSE message: {event_data.get('method', 'unknown')}")
                
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse SSE data as JSON: {e}")
                    continue
        
        if response_message:
            return response_message
        else:
            raise Exception(f"No response received for request {request_id} in SSE stream")

    async def start_standalone_sse(self, message_handler) -> None:
        """
        Open standalone SSE stream for server-initiated messages.
        
        Optional feature: Client can issue GET request to open an SSE stream
        for receiving server notifications and requests unrelated to client requests.
        """
        if self._standalone_sse_task and not self._standalone_sse_task.done():
            logger.warning("Standalone SSE stream already running")
            return
        
        self._standalone_sse_task = asyncio.create_task(
            self._run_standalone_sse(message_handler)
        )

    async def _run_standalone_sse(self, message_handler) -> None:
        """
        Run standalone SSE stream reader.
        
        Creates a dedicated session for the long-lived SSE connection.
        """
        if not self._connected:
            raise Exception("Not connected")
        
        # Create dedicated session for long-lived SSE stream
        connector = aiohttp.TCPConnector(ssl=self.ssl_verify, limit=self.connection_limit, limit_per_host=self.connection_limit_per_host)
        timeout = aiohttp.ClientTimeout(total=None)  # No timeout for SSE stream
        
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                headers = self._build_headers(include_session=True)
                headers['Accept'] = 'text/event-stream'
                
                async with session.get(self.url, headers=headers) as response:
                    if response.status == 405:
                        logger.info("Server does not support standalone SSE (405)")
                        return
                    elif response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Failed to open standalone SSE: HTTP {response.status}: {error_text}")
                        return
                    
                    logger.info("Standalone SSE stream opened")
                    
                    async for line in response.content:
                        decoded = line.decode('utf-8').strip()
                        if not decoded or not decoded.startswith('data:'):
                            continue
                        
                        data_str = decoded[5:].strip()
                        
                        try:
                            event_data = json.loads(data_str)
                            message = self._parse_json_response(event_data)
                            await message_handler(message)
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse SSE data: {e}")
                        except Exception as e:
                            logger.error(f"Error handling SSE message: {e}")
        
        except asyncio.CancelledError:
            logger.debug("Standalone SSE stream cancelled")
            raise
        except Exception as e:
            logger.error(f"Standalone SSE stream error: {e}")

    def _build_headers(self, include_session: bool = False) -> Dict[str, str]:
        """Build HTTP headers for request"""
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream'
        }
        
        if include_session and self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        
        return headers

    def _message_to_dict(self, message: MCPMessage) -> Dict[str, Any]:
        """Convert MCPMessage to JSON-RPC dict"""
        payload: Dict[str, Any] = {
            'jsonrpc': message.jsonrpc or '2.0'
        }
        
        if message.id is not None:
            payload['id'] = message.id
        if message.method:
            payload['method'] = message.method
        if message.params is not None:
            payload['params'] = message.params
        if message.result is not None:
            payload['result'] = message.result
        if message.error:
            error_dict: Dict[str, Any] = {
                'code': message.error.code,
                'message': message.error.message
            }
            if message.error.data:
                error_dict['data'] = message.error.data
            payload['error'] = error_dict
        
        return payload

    def _parse_json_response(self, data: Dict[str, Any]) -> MCPMessage:
        """Parse JSON-RPC response into MCPMessage"""
        msg = MCPMessage(
            jsonrpc=data.get('jsonrpc', '2.0'),
            id=data.get('id')
        )
        
        if 'method' in data:
            msg.method = data['method']
        if 'params' in data:
            msg.params = data['params']
        if 'result' in data:
            msg.result = data['result']
        if 'error' in data:
            error_data = data['error']
            msg.error = MCPError(
                code=error_data.get('code', -32000),
                message=error_data.get('message', 'Unknown error'),
                data=error_data.get('data')
            )
        
        return msg

    def _next_request_id(self) -> int:
        """Generate next request ID"""
        self._request_counter += 1
        return self._request_counter
