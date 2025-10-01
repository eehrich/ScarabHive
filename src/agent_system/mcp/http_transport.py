"""
HTTP Transport for MCP using JSON-RPC 2.0

Implements HTTP-based transport for MCP communication.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional
import aiohttp

from .core import MCPTransport, MCPMessage, MCPError

logger = logging.getLogger(__name__)


class HTTPTransport(MCPTransport):
    """HTTP transport implementation for MCP"""

    def __init__(self, base_url: str, timeout: float = 30.0, ssl_verify: bool = True):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.ssl_verify = ssl_verify
        self.session: Optional[aiohttp.ClientSession] = None

    async def connect(self) -> None:
        """Establish HTTP session"""
        if self.session is None:
            connector = aiohttp.TCPConnector(ssl=self.ssl_verify)
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={"Content-Type": "application/json"}
            )

    async def disconnect(self) -> None:
        """Close HTTP session"""
        if self.session:
            await self.session.close()
            self.session = None

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
            async with self.session.post(f"{self.base_url}/mcp", json=payload) as response:
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
        """Send request and return response for HTTP"""
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
            async with self.session.post(f"{self.base_url}/mcp", json=payload) as response:
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

                response_data = await response.json()

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