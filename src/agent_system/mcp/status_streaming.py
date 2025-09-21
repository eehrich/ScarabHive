"""
Status Event Streaming for MCP Transport

Integrates the existing status event system with MCP streaming transport
to provide real-time status updates to MCP clients via SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional, Dict, Any, Set
from datetime import datetime

from .core import MCPMessage
from .streaming_transport import HTTPStreamingTransport
from .status import status_bus, StatusEvent

logger = logging.getLogger(__name__)


class MCPStatusStreamingTransport(HTTPStreamingTransport):
    """Extended streaming transport that publishes status events as MCP notifications."""

    def __init__(self, base_url: str, config: Optional[Dict[str, Any]] = None, **kwargs):
        super().__init__(base_url, config, **kwargs)
        self._status_subscription: Optional[asyncio.Queue] = None
        self._status_task: Optional[asyncio.Task] = None
        self._server_filter: Optional[str] = config.get("status_server_filter") if config else None
        self._request_id_filter: Optional[str] = config.get("status_request_id_filter") if config else None

    async def connect(self) -> None:
        """Establish HTTP session and start status event streaming."""
        await super().connect()
        
        # Subscribe to status events
        self._status_subscription = await status_bus.subscribe(
            server=self._server_filter,
            request_id=self._request_id_filter
        )
        
        # Start background task to forward status events
        self._status_task = asyncio.create_task(self._stream_status_events())
        logger.info("Started status event streaming for MCP transport")

    async def disconnect(self) -> None:
        """Close HTTP session and stop status event streaming."""
        # Stop status streaming
        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except asyncio.CancelledError:
                pass
            self._status_task = None

        # Unsubscribe from status events
        if self._status_subscription:
            status_bus.unsubscribe(self._status_subscription)
            self._status_subscription = None

        await super().disconnect()
        logger.info("Stopped status event streaming for MCP transport")

    async def _stream_status_events(self) -> None:
        """Background task that forwards status events as MCP notifications."""
        if not self._status_subscription:
            return

        try:
            while True:
                # Wait for status event
                event: StatusEvent = await self._status_subscription.get()
                
                # Convert to MCP notification
                notification = self._create_status_notification(event)
                
                # Send as notification (no response expected)
                await self._send_status_notification(notification)
                
        except asyncio.CancelledError:
            logger.debug("Status event streaming task cancelled")
        except Exception as e:
            logger.error(f"Error in status event streaming: {e}")

    def _create_status_notification(self, event: StatusEvent) -> MCPMessage:
        """Convert StatusEvent to MCP notification message."""
        return MCPMessage(
            jsonrpc="2.0",
            method="notifications/status",
            params={
                "server": event.server,
                "request_id": event.request_id,
                "message": event.message,
                "timestamp": event.timestamp.isoformat(),
                "phase": event.phase,
                "level": event.level,
                "meta": event.meta
            }
        )

    async def _send_status_notification(self, notification: MCPMessage) -> None:
        """Send status notification over the HTTP transport."""
        try:
            # Convert to JSON-RPC notification format
            payload = {
                "jsonrpc": notification.jsonrpc,
                "method": notification.method,
                "params": notification.params
            }
            
            # Log the notification
            logger.debug(f"Status notification: {json.dumps(payload)}")
            
            # Send over HTTP if session is available
            if self.session:
                async with self.session.post(
                    f"{self.base_url}/notifications",
                    json=payload
                ) as response:
                    if response.status != 200:
                        logger.warning(f"Status notification failed: {response.status}")
                    else:
                        logger.debug("Status notification sent successfully")
                        
        except Exception:
            logger.exception("Failed to send status notification")


class MCPStatusNotificationHandler:
    """Handler for MCP clients to process incoming status notifications."""

    def __init__(self):
        self._status_handlers: Set[Any] = set()

    def add_status_handler(self, handler: Any) -> None:
        """Add a handler function for status notifications.
        
        Handler should accept (server, request_id, message, timestamp, phase, level, meta).
        """
        self._status_handlers.add(handler)

    def remove_status_handler(self, handler: Any) -> None:
        """Remove a status handler."""
        self._status_handlers.discard(handler)

    async def handle_status_notification(self, params: Dict[str, Any]) -> None:
        """Process incoming status notification from MCP server."""
        try:
            # Extract status event data
            server = params.get("server")
            request_id = params.get("request_id")
            message = params.get("message")
            timestamp_str = params.get("timestamp")
            phase = params.get("phase")
            level = params.get("level")
            meta = params.get("meta")

            # Parse timestamp
            timestamp = datetime.fromisoformat(timestamp_str) if timestamp_str else datetime.now()

            # Call all registered handlers
            for handler in self._status_handlers:
                try:
                    await handler(server, request_id, message, timestamp, phase, level, meta)
                except Exception as e:
                    logger.error(f"Error in status handler {handler}: {e}")

        except Exception as e:
            logger.error(f"Error processing status notification: {e}")


# Example usage for MCP server implementing status streaming
async def create_status_streaming_server(base_url: str, server_filter: Optional[str] = None) -> MCPStatusStreamingTransport:
    """Create an MCP transport with status event streaming enabled."""
    config = {}
    if server_filter:
        config["status_server_filter"] = server_filter

    transport = MCPStatusStreamingTransport(base_url, config)
    await transport.connect()
    return transport


# Example usage for MCP client consuming status events
async def setup_status_client_handler() -> MCPStatusNotificationHandler:
    """Set up a client-side handler for status notifications."""
    handler = MCPStatusNotificationHandler()
    
    # Example status handler
    async def log_status(server, request_id, message, timestamp, phase, level, meta):
        print(f"[{timestamp}] {server}: {message} (phase={phase}, level={level})")
    
    handler.add_status_handler(log_status)
    return handler