from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import asyncio
import logging
import os

logger = logging.getLogger(__name__)


PHASE_START = "start"
PHASE_PROGRESS = "progress"
PHASE_END = "end"
PHASE_ERROR = "error"
VALID_PHASES = {PHASE_START, PHASE_PROGRESS, PHASE_END, PHASE_ERROR}


@dataclass
class StatusEvent:
        """Represents a status update event from an MCP server.

        Unified schema (Task 0186):
            - server: plugin / server name (str)
            - request_id: optional correlation id (str|None)
            - phase: one of start|progress|end|error (str)
            - message: human-readable short status (str)
            - level: info|warning|error (str) (orthogonal to phase)
            - timestamp: ISO8601 moment of emission (datetime)
            - meta: optional structured details (dict|None)
        """
        server: str
        request_id: Optional[str]
        message: str
        timestamp: datetime
        phase: str = PHASE_PROGRESS
        level: str = "info"  # info, warning, error
        meta: Optional[dict] = field(default=None)

        def to_dict(self) -> dict:
                return {
                        "server": self.server,
                        "request_id": self.request_id,
                        "message": self.message,
                        "timestamp": self.timestamp.isoformat(),
                        "phase": self.phase,
                        "level": self.level,
                        "meta": self.meta,
                }


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
        queue: asyncio.Queue[StatusEvent] = asyncio.Queue()
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
    level: str = "info",
    phase: str = PHASE_PROGRESS,
    meta: Optional[dict] = None,
) -> None:
    """Convenience function to publish a status event.

    Args:
        server: Name of the MCP server
        message: Status message
        request_id: Optional request identifier
        level: Log level (info, warning, error)
    """
    # Normalize / validate phase & level graciously (do not raise to avoid breaking user flows)
    if phase not in VALID_PHASES:
        logger.debug("Invalid phase '%s' provided; defaulting to 'progress'", phase)
        phase = PHASE_PROGRESS
    if level not in ("info", "warning", "error"):
        logger.debug("Invalid level '%s' provided; defaulting to 'info'", level)
        level = "info"
    if phase == PHASE_ERROR and level == "info":
        level = "error"  # escalate sensible default

    event = StatusEvent(
        server=server,
        request_id=request_id,
        message=message,
        timestamp=datetime.now(),
        phase=phase,
        level=level,
        meta=meta,
    )
    # Publish locally first
    await status_bus.publish(event)

    # Optionally forward to a remote SSE broker via HTTP POST when configured.
    # Use env var AGENT_STATUS_SSE_PUSH_URL to specify the broker publish endpoint
    # (e.g. http://127.0.0.1:8765/status/publish).
    push_url = os.environ.get("AGENT_STATUS_SSE_PUSH_URL")
    if not push_url:
        return

    payload = {
        "server": event.server,
        "request_id": event.request_id,
        "message": event.message,
        "level": event.level,
        "timestamp": event.timestamp.isoformat(),
        "phase": event.phase,
        "meta": event.meta,
    }

    # Fire-and-forget POST to the broker
    async def _post():
        try:
            try:
                import aiohttp
            except Exception:
                logger.debug("aiohttp not available; cannot push status to SSE broker")
                return

            async with aiohttp.ClientSession() as sess:
                async with sess.post(push_url, json=payload, timeout=5) as resp:
                    if resp.status >= 400:
                        logger.debug("Failed to push status to broker %s: %s", push_url, resp.status)
        except Exception as e:
            logger.debug("Exception while pushing status to broker: %s", e)

    try:
        asyncio.create_task(_post())
    except RuntimeError:
        # if there's no running loop, run in new loop in background thread
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_post())
            loop.close()
        except Exception:
            logger.debug("Failed to push status to broker in fallback path")
