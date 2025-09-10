from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import asyncio
import logging

logger = logging.getLogger(__name__)


@dataclass
class StatusEvent:
    """Represents a status update event from an MCP server."""
    server: str
    request_id: Optional[str]
    message: str
    timestamp: datetime
    level: str = "info"  # info, warning, error


class StatusBus:
    """Lightweight async pub/sub bus for MCP status events.

    Allows subscribers to filter events by server and/or request_id.
    """

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []
        self._filters: list[dict] = []

    async def subscribe(
        self,
        server: Optional[str] = None,
        request_id: Optional[str] = None
    ) -> asyncio.Queue:
        """Subscribe to status events with optional filtering.

        Args:
            server: Filter events to specific server (None for all)
            request_id: Filter events to specific request (None for all)

        Returns:
            AsyncQueue that will receive matching StatusEvents
        """
        queue = asyncio.Queue()
        self._subscribers.append(queue)
        self._filters.append({"server": server, "request_id": request_id})
        logger.debug(f"New subscriber added. Total subscribers: {len(self._subscribers)}")
        return queue

    async def publish(self, event: StatusEvent) -> None:
        """Publish a status event to all matching subscribers.

        Args:
            event: The status event to publish
        """
        published_count = 0
        for i, queue in enumerate(self._subscribers):
            filter_ = self._filters[i]
            if filter_["server"] and event.server != filter_["server"]:
                continue
            if filter_["request_id"] and event.request_id != filter_["request_id"]:
                continue

            try:
                await queue.put(event)
                published_count += 1
            except Exception as e:
                logger.warning(f"Failed to publish event to subscriber {i}: {e}")

        logger.debug(f"Published status event to {published_count} subscribers")

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber.

        Args:
            queue: The queue returned by subscribe()
        """
        if queue in self._subscribers:
            idx = self._subscribers.index(queue)
            self._subscribers.pop(idx)
            self._filters.pop(idx)
            logger.debug(f"Subscriber removed. Total subscribers: {len(self._subscribers)}")

    def get_subscriber_count(self) -> int:
        """Get the number of active subscribers."""
        return len(self._subscribers)


# Global status bus instance
status_bus = StatusBus()


async def publish_status(
    server: str,
    message: str,
    request_id: Optional[str] = None,
    level: str = "info"
) -> None:
    """Convenience function to publish a status event.

    Args:
        server: Name of the MCP server
        message: Status message
        request_id: Optional request identifier
        level: Log level (info, warning, error)
    """
    event = StatusEvent(
        server=server,
        request_id=request_id,
        message=message,
        timestamp=datetime.now(),
        level=level
    )
    await status_bus.publish(event)
