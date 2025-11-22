"""
Status Event Forwarding for Agent Server
Handles background forwarding of status events to SSE streams.
"""
import asyncio
import logging
from typing import List, Dict, Any

from ....mcp.status import status_bus

logger = logging.getLogger(__name__)


class StatusEventForwarder:
    """Manages forwarding of status events to SSE streams."""

    def __init__(self):
        self.request_id = None
        self.status_queue = None
        self.forwarding_task = None
        self.forwarding_done = asyncio.Event()
        self.forwarding_ready = asyncio.Event()
        self.first_get_started = asyncio.Event()
        self.status_events_to_forward = []

    async def start_forwarding(self, request_id: str) -> None:
        """Start forwarding status events for a specific request"""
        self.request_id = request_id
        
        # Reset all state for new request (critical for reuse)
        self.forwarding_done.clear()
        self.forwarding_ready.clear()
        self.first_get_started.clear()
        self.status_events_to_forward.clear()
        
        # Subscribe WITHOUT request_id filter to catch tool-specific suffixed IDs
        # (e.g., "abc123_001", "abc123_002" when base request_id is "abc123")
        self.status_queue = await status_bus.subscribe()
        
        # Start the background task to forward events
        self.forwarding_task = asyncio.create_task(self._forward_status_events())
        
        # Wait for the forwarding task to be ready and listening
        await self.forwarding_ready.wait()
        await self.first_get_started.wait()
        # Give the forwarding task a moment to actually reach the status_queue.get() call
        await asyncio.sleep(0.01)
        logger.debug("Status forwarding task is ready and listening")

    async def _forward_status_events(self) -> None:
        """Forward status events from status_bus to SSE stream."""
        try:
            logger.debug("Status forwarding task starting...")
            while not self.forwarding_done.is_set():
                try:
                    # Signal that we're ready to receive events
                    if not self.forwarding_ready.is_set():
                        self.forwarding_ready.set()
                        logger.debug("Status forwarding task ready, waiting for events...")

                    # Signal that the first get() call is about to start
                    if not self.first_get_started.is_set():
                        self.first_get_started.set()

                    # Wait for status event (blocking, no CPU spin)
                    # This efficiently blocks until an event arrives or task is cancelled
                    status_event = await self.status_queue.get()
                    logger.debug("Forwarding received status event: %s [%s]: %s (seq: %s)",
                               status_event.server, status_event.phase, status_event.message,
                               status_event.meta.get('_seq') if status_event.meta else 'no-seq')
                    logger.debug("Forwarding status event: %s [%s]: %s", status_event.server, status_event.phase, status_event.message)
                    # Convert status event to SSE format
                    status_sse_event = {
                        "type": "status",
                        "server": status_event.server,
                        "request_id": status_event.request_id,
                        "message": status_event.message,
                        "phase": status_event.phase.value,  # Convert enum to string
                        "level": status_event.level,
                        "timestamp": status_event.timestamp.isoformat(),
                        "meta": status_event.meta or {}
                    }
                    self.status_events_to_forward.append(status_sse_event)
                except asyncio.CancelledError:
                    break
        except Exception as e:
            logger.debug("Error in status event forwarding: %s", e)

    def get_pending_events(self) -> List[Dict[str, Any]]:
        """Get any pending status events to forward."""
        events = self.status_events_to_forward.copy()
        self.status_events_to_forward.clear()
        return events

    async def drain_pending_events(self, max_wait_ms: float = 100) -> List[Dict[str, Any]]:
        """Drain all pending events with timeout to ensure .end() events are not lost.
        
        This method polls the status queue for a short time to ensure all events
        that were published before tools completed are actually forwarded.
        
        Args:
            max_wait_ms: Maximum time to wait for events in milliseconds
            
        Returns:
            List of all pending events
        """
        all_events = self.get_pending_events()
        
        # Poll the queue for any remaining events with timeout
        deadline = asyncio.get_event_loop().time() + (max_wait_ms / 1000)
        while asyncio.get_event_loop().time() < deadline:
            # Check if there are events in the forwarding buffer
            if self.status_events_to_forward:
                all_events.extend(self.get_pending_events())
            
            # Give the forwarding task a chance to process
            await asyncio.sleep(0.01)
            
            # Check again
            if self.status_events_to_forward:
                all_events.extend(self.get_pending_events())
        
        # Final check
        if self.status_events_to_forward:
            all_events.extend(self.get_pending_events())
        
        return all_events

    async def stop_forwarding(self) -> None:
        """Stop the status event forwarding task."""
        if self.forwarding_task and self.forwarding_done:
            try:
                self.forwarding_done.set()
                self.forwarding_task.cancel()
                try:
                    await self.forwarding_task
                except asyncio.CancelledError:
                    pass
                logger.debug("Cleaned up status forwarding task")
            except Exception as e:
                logger.debug("Error cleaning up status forwarding task: %s", e)