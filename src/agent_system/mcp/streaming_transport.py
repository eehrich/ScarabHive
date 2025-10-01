"""
HTTP Streaming (SSE) Transport for MCP

Implements an HTTP transport that supports Server-Sent Events (SSE)
and configuration encoded in the query string. This transport is a
streaming-capable HTTP transport suitable for MCP servers that use
SSE/text-event-stream responses and session headers.

Includes support for status event streaming via notifications.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional
import aiohttp

from .core import MCPTransport, MCPMessage, MCPError

logger = logging.getLogger(__name__)


class HTTPStreamingTransport(MCPTransport):
    """HTTP streaming (SSE) transport implementation for MCP"""

    def __init__(self, base_url: str, timeout: float = 30.0, ssl_verify: bool = True):
        """Initialize HTTP streaming transport.
        
        Args:
            base_url: Base URL of the MCP server
            timeout: Request timeout in seconds
            ssl_verify: Whether to verify SSL certificates
        """
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.ssl_verify = ssl_verify
        self.session: Optional[aiohttp.ClientSession] = None
        self.session_id: Optional[str] = None

    async def connect(self) -> None:
        """Establish HTTP session"""
        if self.session is None:
            # Newer aiohttp versions prefer `ssl=` instead of `verify_ssl`.
            # `self.ssl_verify` may be a bool or an SSLContext; pass it through as `ssl`.
            connector = aiohttp.TCPConnector(ssl=self.ssl_verify)
            # For proxy environments, separate connection and total timeouts
            # Connection timeout is for initial TCP connection (important for proxies)
            # Total timeout is for the entire request including data transfer
            timeout = aiohttp.ClientTimeout(
                total=self.timeout,  # Total request timeout
                connect=min(self.timeout * 0.5, 10.0)  # Connection timeout: 50% of total, max 10s
            )
            # Streaming HTTP servers often require headers that accept SSE
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"
            }
            # Allow aiohttp to pick up HTTP(S)_PROXY and other env vars in corporate networks
            # by enabling trust_env. This is a low-risk change that helps with Zscaler/proxy setups.
            trust_env = True
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers=headers,
                trust_env=trust_env,
            )

            # Log detected proxy environment variables to help debugging in corporate networks
            import os
            http_proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
            https_proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
            if http_proxy or https_proxy:
                logger.debug("HTTPStreamingTransport.connect(): detected proxy settings http=%s https=%s", http_proxy, https_proxy)

            logger.debug(
                "HTTPStreamingTransport.connect(): created session %s with timeout total=%s connect=%s",
                id(self.session), self.timeout, timeout.connect
            )

    async def disconnect(self) -> None:
        """Close HTTP session"""
        if self.session:
            try:
                await self.session.close()
                logger.debug("HTTPStreamingTransport.disconnect(): closed session")
            except Exception:
                pass
            self.session = None
        self.session_id = None

    def _build_url(self) -> str:
        """Build URL for MCP endpoint"""
        return f"{self.base_url}/mcp"

    async def _send_initialized_notification(self) -> None:
        """Send the initialized notification to complete the MCP handshake"""
        if not self.session or not self.session_id:
            return

        # Send initialized notification (no ID for notifications)
        initialized_payload = {
            'jsonrpc': '2.0',
            'method': 'notifications/initialized',
            'params': {}
        }

        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
            'Mcp-Session-Id': self.session_id
        }

        try:
            url = self._build_url()
            async with self.session.post(url, json=initialized_payload, headers=headers) as response:
                if response.status == 202:  # Notifications typically return 202 Accepted
                    logger.info("Successfully sent initialized notification")
                else:
                    logger.warning(f"Initialized notification returned status {response.status}")
        except Exception as e:
            logger.error(f"Failed to send initialized notification: {e}")

    async def send_notification(self, notification: MCPMessage) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self.session:
            await self.connect()

        # Convert notification to JSON-RPC 2.0 format
        payload: Dict[str, Any] = {
            "jsonrpc": notification.jsonrpc,
            "method": notification.method
        }

        if notification.params:
            payload["params"] = notification.params

        try:
            if not self.session:
                raise RuntimeError("Session not initialized")

            url = self._build_url()
            headers = {}

            # Add session ID for authenticated sessions
            if self.session_id and self.session_id != "connection-based":
                headers['Mcp-Session-Id'] = self.session_id

            async with self.session.post(url, json=payload, headers=headers or None) as response:
                if response.status == 202:  # Notifications typically return 202 Accepted
                    logger.debug(f"Successfully sent notification: {notification.method}")
                else:
                    logger.warning(f"Notification returned unexpected status {response.status}")

        except Exception as e:
            logger.error(f"Failed to send notification {notification.method}: {e}")
            raise

    async def send_message(self, message: MCPMessage) -> None:
        """Send JSON-RPC message over HTTP POST"""
        if not self.session:
            await self.connect()

        # Convert message to JSON-RPC 2.0 format
        payload: Dict[str, Any] = {
            "jsonrpc": message.jsonrpc,
            "id": message.id
        }

        if message.method:
            payload["method"] = message.method
        if message.params:
            payload["params"] = message.params
        if message.result is not None:
            payload["result"] = message.result
        if message.error:
            error_dict: Dict[str, Any] = {
                "code": message.error.code,
                "message": message.error.message,
                "data": message.error.data
            }
            payload["error"] = error_dict

        try:
            if not self.session:
                raise RuntimeError("Session not initialized")

            url = self._build_url()
            async with self.session.post(url, json=payload) as response:
                if response.status != 200:
                    logger.error(f"HTTP {response.status}: {await response.text()}")
                    raise Exception(f"HTTP error {response.status}")

        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            raise

    async def receive_message(self) -> MCPMessage:
        """Receive JSON-RPC message (for HTTP, this is handled by send_message response)"""
        # HTTP is request-response, so receiving is handled in the response of send_message
        # This method would be used for WebSocket or similar bidirectional transports
        raise NotImplementedError("HTTP transport uses request-response pattern")

    async def send_request(self, message: MCPMessage) -> MCPMessage:
        """Send request and return response for streaming HTTP"""
        if not self.session:
            await self.connect()

        # Convert message to JSON-RPC 2.0 format
        payload: Dict[str, Any] = {
            "jsonrpc": message.jsonrpc,
            "method": message.method,
            "params": message.params or {},
            "id": message.id
        }

        try:
            if not self.session:
                raise RuntimeError("Session not initialized")

            url = self._build_url()
            headers = {}

            # Add session ID to subsequent requests after initialization
            if self.session_id and message.method != "initialize":
                headers['Mcp-Session-Id'] = self.session_id

            # Debug: log initialize payload and destination when debugging 422 errors
            if message.method == "initialize":
                logger.debug(f"Sending initialize to {url} with headers={headers} payload={json.dumps(payload)}")
            async with self.session.post(url, json=payload, headers=headers or None) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"HTTP {response.status}: {error_text}")
                    return MCPMessage(
                        jsonrpc="2.0",
                        id=message.id,
                        error=MCPError(
                            code=-32000,  # Server error
                            message=f"HTTP {response.status}",
                            data={"details": error_text}
                        )
                    )

                # Streaming servers may return SSE format for some responses
                content_type = response.headers.get('content-type', '')
                if 'text/event-stream' in content_type:
                    # Parse SSE response
                    text = await response.text()
                    lines = text.strip().split('\n')
                    for line in lines:
                        if line.startswith('data: '):
                            response_data = json.loads(line[6:])  # Remove 'data: ' prefix
                            break
                    else:
                        raise ValueError("No data found in SSE response")

                    # For initialize, extract session from the response headers
                    if message.method == "initialize":
                        # Check for MCP session ID header
                        session_id = response.headers.get('mcp-session-id') or response.headers.get('Mcp-Session-Id')
                        if session_id:
                            self.session_id = session_id
                            logger.info(f"Found MCP session ID: {self.session_id}")
                        else:
                            # Check for other session headers
                            for header_name, header_value in response.headers.items():
                                if 'session' in header_name.lower():
                                    self.session_id = header_value
                                    logger.info(f"Found session ID in header {header_name}: {self.session_id}")
                                    break

                        # If no session in headers, we need to maintain session state differently
                        if not self.session_id:
                            logger.warning("No session ID found in headers")
                else:
                    response_data = await response.json()

                # Extract session ID from initialize response
                if message.method == "initialize" and "result" in response_data:
                    # Check for MCP session ID header
                    session_id = response.headers.get('mcp-session-id') or response.headers.get('Mcp-Session-Id')
                    if session_id:
                        self.session_id = session_id
                        logger.info(f"Found MCP session ID: {self.session_id}")

                        # Send the initialized notification to complete the handshake
                        await self._send_initialized_notification()
                    else:
                        # Check for other session headers
                        for header_name, header_value in response.headers.items():
                            if 'session' in header_name.lower():
                                self.session_id = header_value
                                logger.info(f"Found session ID in header {header_name}: {self.session_id}")
                                await self._send_initialized_notification()
                                break

                    # If no session in headers, mark as connection-based
                    if not self.session_id:
                        self.session_id = "connection-based"
                        logger.info("Using connection-based session for streaming")

                # Parse JSON-RPC response
                error = None
                if "error" in response_data:
                    error_data = response_data["error"]
                    error = MCPError(
                        code=error_data["code"],
                        message=error_data["message"],
                        data=error_data.get("data")
                    )

                return MCPMessage(
                    jsonrpc=response_data.get("jsonrpc", "2.0"),
                    id=response_data.get("id"),
                    result=response_data.get("result"),
                    error=error
                )

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response: {e}")
            return MCPMessage(
                jsonrpc="2.0",
                id=message.id,
                error=MCPError(
                    code=-32700,  # Parse error
                    message="Invalid JSON",
                    data={"details": str(e)}
                )
            )
        except Exception as e:
            logger.debug(f"Request failed: {e}")
            return MCPMessage(
                jsonrpc="2.0",
                id=message.id,
                error=MCPError(
                    code=-32000,  # Server error
                    message="Request failed",
                    data={"details": str(e)}
                )
            )
