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
        
        # Wait for the forwarding task to be ready and listening (with timeout to prevent deadlock)
        try:
            await asyncio.wait_for(self.forwarding_ready.wait(), timeout=2.0)
            await asyncio.wait_for(self.first_get_started.wait(), timeout=2.0)
            # Give the forwarding task a moment to actually reach the status_queue.get() call
            await asyncio.sleep(0.01)
            logger.debug("Status forwarding task is ready and listening")
        except asyncio.TimeoutError:
            logger.warning("Status forwarding task did not become ready in time (may have crashed)")
            # Task may have crashed, check if it's still running
            if self.forwarding_task and self.forwarding_task.done():
                try:
                    # This will re-raise the exception from the task
                    await self.forwarding_task
                except Exception as e:
                    logger.error("Status forwarding task crashed during startup: %s", e, exc_info=True)
            # Continue anyway - status events will just not be forwarded

    async def _forward_status_events(self) -> None:
        """Forward status events from status_bus to SSE stream."""
        try:
            logger.debug("Status forwarding task starting...")
            
            # Signal that we're ready FIRST (before the loop)
            if not self.forwarding_ready.is_set():
                self.forwarding_ready.set()
                logger.debug("Status forwarding task ready")
            
            # Signal that the first get() call is about to start
            if not self.first_get_started.is_set():
                self.first_get_started.set()
                logger.debug("Status forwarding task starting event loop")
            
            while not self.forwarding_done.is_set():
                try:

                    # Wait for status event (blocking, no CPU spin)
                    # This efficiently blocks until an event arrives or task is cancelled
                    status_event = await self.status_queue.get()
                    
                    # Filter events to only forward those matching our request_id
                    # We subscribed without filter to catch suffixed IDs (e.g., "abc123_001")
                    # but we need to filter here to avoid forwarding events from other requests
                    event_request_id = status_event.request_id
                    if self.request_id and event_request_id:
                        # Check if event belongs to this request (exact match or prefix match for suffixed IDs)
                        if not (event_request_id == self.request_id or 
                                event_request_id.startswith(f"{self.request_id}_")):
                            # Event doesn't belong to this request, skip it
                            logger.debug("Skipping status event for different request: %s (our request: %s)",
                                       event_request_id, self.request_id)
                            continue
                    
                    logger.debug("Forwarding status event: %s [%s]: %s", 
                               status_event.server, status_event.phase, status_event.message)
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
                    logger.debug("Status forwarding task cancelled")
                    break
        except Exception as e:
            logger.error("Fatal error in status event forwarding: %s", e, exc_info=True)
            # Set ready events to prevent deadlock in start_forwarding
            self.forwarding_ready.set()
            self.first_get_started.set()
            raise

    def get_pending_events(self) -> List[Dict[str, Any]]:
        """Get any pending status events to forward."""
        events = self.status_events_to_forward.copy()
        self.status_events_to_forward.clear()
        return events

    async def drain_pending_events(self, max_wait_ms: float = 100) -> List[Dict[str, Any]]:
        """Drain all pending events with timeout to ensure .end() events are not lost.
        
        This method polls the status queue for a short time to ensure all events
        that were published before tools completed are actually forwarded.
        Exits early if .end or .error events are found.
        
        Args:
            max_wait_ms: Maximum time to wait for events in milliseconds
            
        Returns:
            List of all pending events
        """
        all_events = self.get_pending_events()
        
        # Check if we already have terminal events (.end or .error)
        has_terminal_event = any(
            ev.get("phase") in ("end", "error") 
            for ev in all_events
        )
        if has_terminal_event:
            return all_events
        
        # Poll the queue for any remaining events with timeout
        deadline = asyncio.get_event_loop().time() + (max_wait_ms / 1000)
        idle_checks = 0
        max_idle_checks = 3  # Exit early if no events for 3 consecutive checks
        
        while asyncio.get_event_loop().time() < deadline:
            # Check if there are events in the forwarding buffer
            had_events = False
            if self.status_events_to_forward:
                new_events = self.get_pending_events()
                if new_events:
                    all_events.extend(new_events)
                    had_events = True
                    idle_checks = 0
                    
                    # Exit early if we found .end or .error
                    for ev in new_events:
                        if ev.get("phase") in ("end", "error"):
                            return all_events
            
            if not had_events:
                idle_checks += 1
                if idle_checks >= max_idle_checks:
                    # No events for several checks, exit early
                    break
            
            # Give the forwarding task a chance to process
            await asyncio.sleep(0.01)
        
        # Final check
        if self.status_events_to_forward:
            final_events = self.get_pending_events()
            if final_events:
                all_events.extend(final_events)
        
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
        
        # CRITICAL: Unsubscribe from status bus to prevent queue buildup
        if self.status_queue:
            try:
                status_bus.unsubscribe(self.status_queue)
                logger.debug("Unsubscribed from status bus")
            except Exception as e:
                logger.warning("Error unsubscribing from status bus: %s", e)
            finally:
                self.status_queue = None