"""
Status Event Forwarding for Agent Server

Simplified architecture: Direct synchronous handler without background task.
Events are pushed directly to the list by the StatusBus handler - no queue, no task, no race conditions.
"""
import logging
from typing import List, Dict, Any, Optional

from ....tools.status import status_bus, StatusHandler, StatusEvent

logger = logging.getLogger(__name__)


class DirectStatusHandler(StatusHandler):
    """Handler that directly appends events to a list (no queue, no task)."""
    
    def __init__(self, request_id: str, events_list: List[Dict[str, Any]]):
        self.request_id = request_id
        self.events_list = events_list
    
    async def process(self, event: StatusEvent) -> None:
        """Directly append matching events to the list."""
        # Filter: only events for this request (exact match or prefix for sub-requests)
        event_request_id = event.request_id
        if self.request_id and event_request_id:
            if not (event_request_id == self.request_id or 
                    event_request_id.startswith(f"{self.request_id}_")):
                return  # Not our event
        
        # Convert to SSE format and append directly
        status_sse_event = {
            "type": "status",
            "server": event.server,
            "request_id": event.request_id,
            "message": event.message,
            "phase": event.phase.value,
            "level": event.level,
            "timestamp": event.timestamp.isoformat(),
            "meta": event.meta or {},
            # The same tree SSEStatusHandler sends. Without it the page never learns
            # that one operation ran under another: a sub-agent's lines sat flat
            # between the parent's own, and the whole nesting half of the status
            # display (depth, connectors, collapsing a sub-tree) was inert, because
            # this is the handler the run's own stream goes through.
            "tree": {
                "parent_id": event.parent_id,
                "depth_level": event.depth_level,
                "child_count": event.child_count,
                "is_leaf": event.is_leaf,
            },
        }
        self.events_list.append(status_sse_event)

class StatusEventForwarder:
    """Manages forwarding of status events to SSE streams.
    
    Simplified design: Uses a direct handler that synchronously appends to the events list.
    No background task, no queue, no race conditions.
    """

    def __init__(self):
        self.request_id: Optional[str] = None
        self.status_events_to_forward: List[Dict[str, Any]] = []
        self._handler: Optional[DirectStatusHandler] = None

    async def start_forwarding(self, request_id: str) -> None:
        """Start forwarding status events for a specific request."""
        self.request_id = request_id
        self.status_events_to_forward.clear()
        
        # Create and register direct handler
        self._handler = DirectStatusHandler(request_id, self.status_events_to_forward)
        status_bus.add_handler(self._handler)
        logger.debug("Request %s registered direct status handler", request_id)

    async def stop_forwarding(self) -> None:
        """Stop forwarding and cleanup."""
        if self._handler:
            status_bus.remove_handler(self._handler)
            logger.debug("Request %s removed direct status handler", self.request_id)
            self._handler = None

    def get_pending_events(self) -> List[Dict[str, Any]]:
        """Get and clear all pending status events.
        
        This is now reliable - events are added synchronously by the handler
        during StatusBus.publish(), so they're immediately available.
        """
        events = self.status_events_to_forward.copy()
        self.status_events_to_forward.clear()
        return events

    async def drain_pending_events(self, max_wait_ms: float = 100) -> List[Dict[str, Any]]:
        """Drain all pending events.

        With the direct handler design, events are immediately available.
        This method is kept for API compatibility but simply returns get_pending_events().
        """
        # Small yield to let any in-flight publish() calls complete
        import asyncio
        await asyncio.sleep(0.001)
        return self.get_pending_events()