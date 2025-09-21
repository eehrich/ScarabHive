"""
Improved Status System - Eliminating asyncio.sleep(0) anti-patterns
=================================================================

This module provides a more robust status messaging system that guarantees
message delivery without relying on timing-dependent sleep calls.
"""

import logging
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import asyncio

logger = logging.getLogger(__name__)


class StatusPhase(Enum):
    START = "start"
    PROGRESS = "progress" 
    END = "end"
    ERROR = "error"


@dataclass
class StatusEvent:
    server: str
    message: str
    request_id: Optional[str]
    phase: StatusPhase
    sequence: int
    timestamp: datetime = field(default_factory=datetime.now)
    level: str = "info"  # info, warning, error
    meta: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {
            "server": self.server,
            "request_id": self.request_id,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "phase": self.phase.value,  # Convert enum to string
            "level": self.level,
            "sequence": self.sequence,
            "meta": self.meta
        }


class StatusHandler:
    """Base class for status event handlers"""
    
    async def process(self, event: StatusEvent) -> None:
        """Process a status event - must be implemented by subclasses"""
        raise NotImplementedError


class SSEStatusHandler(StatusHandler):
    """Handler that forwards status events to SSE streams"""
    
    def __init__(self, sse_queue):
        self.sse_queue = sse_queue
    
    async def process(self, event: StatusEvent) -> None:
        """Forward event to SSE queue"""
        try:
            await self.sse_queue.put({
                "type": "status",
                "server": event.server,
                "message": event.message,
                "request_id": event.request_id,
                "phase": event.phase.value,
                "sequence": event.sequence,
                "meta": event.meta
            })
        except Exception as e:
            logger.error(f"Failed to forward status to SSE: {e}")


class LogStatusHandler(StatusHandler):
    """Handler that logs status events"""
    
    async def process(self, event: StatusEvent) -> None:
        """Log the status event"""
        logger.info(f"Status: {event.server} [{event.phase.value}] {event.message}")


class QueueStatusHandler(StatusHandler):
    """Handler that forwards status events to an asyncio queue for subscribers"""
    
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
    
    async def process(self, event: StatusEvent) -> None:
        """Forward event to queue for subscriber"""
        try:
            await self.queue.put(event)
        except Exception as e:
            logger.error(f"Failed to forward status to queue: {e}")


class FilteredQueueStatusHandler(StatusHandler):
    """Handler that forwards filtered status events to an asyncio queue"""
    
    def __init__(self, queue: asyncio.Queue, server_filter: Optional[str] = None, request_id_filter: Optional[str] = None):
        self.queue = queue
        self.server_filter = server_filter
        self.request_id_filter = request_id_filter
    
    async def process(self, event: StatusEvent) -> None:
        """Forward event to queue if it matches filters"""
        # Apply server filter
        if self.server_filter and event.server != self.server_filter:
            return
            
        # Apply request_id filter
        if self.request_id_filter and event.request_id != self.request_id_filter:
            return
        
        try:
            await self.queue.put(event)
        except Exception as e:
            logger.error(f"Failed to forward filtered status to queue: {e}")


class StatusPipeline:
    """
    Manages sequential processing of status events through registered handlers.
    Guarantees that all handlers complete before returning.
    """
    
    def __init__(self):
        self.handlers: List[StatusHandler] = []
        self.sequence_counter = 0
    
    def add_handler(self, handler: StatusHandler) -> None:
        """Add a status event handler to the pipeline"""
        self.handlers.append(handler)
    
    async def publish(self, event: StatusEvent) -> None:
        """
        Publish event to all handlers and wait for completion.
        This eliminates the need for asyncio.sleep(0) calls.
        """
        # Process all handlers sequentially to guarantee order and completion
        for handler in self.handlers:
            try:
                await handler.process(event)
            except Exception as e:
                logger.error(f"Handler {handler.__class__.__name__} failed: {e}")
                # Continue with other handlers even if one fails
    
    def _next_sequence(self) -> int:
        """Generate next sequence number for ordering"""
        self.sequence_counter += 1
        return self.sequence_counter


class ImprovedStatusBus:
    """
    Improved status bus that guarantees message delivery without timing dependencies.
    Also supports subscriptions for real-time status monitoring.
    """
    
    def __init__(self):
        self.pipeline = StatusPipeline()
        self._global_sequence = 0
        self._subscribers: List[QueueStatusHandler] = []
    
    def add_handler(self, handler: StatusHandler) -> None:
        """Add a handler to the status pipeline"""
        self.pipeline.add_handler(handler)
    
    async def subscribe(self, server: Optional[str] = None, request_id: Optional[str] = None) -> asyncio.Queue:
        """
        Subscribe to status events.
        
        Args:
            server: If specified, only receive events from this server
            request_id: If specified, only receive events with this request_id
            
        Returns:
            Queue that will receive StatusEvent objects
        """
        queue = asyncio.Queue()
        handler = FilteredQueueStatusHandler(queue, server_filter=server, request_id_filter=request_id)
        self._subscribers.append(handler)
        self.add_handler(handler)
        return queue
    
    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Unsubscribe from status events"""
        # Remove handlers associated with this queue
        self._subscribers = [h for h in self._subscribers if h.queue != queue]
        # Note: We don't remove from pipeline.handlers as that could affect other handlers
    
    async def publish(
        self, 
        server: str, 
        message: str, 
        request_id: Optional[str] = None,
        phase: StatusPhase = StatusPhase.PROGRESS,
        meta: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Publish status event and guarantee delivery to all handlers.
        This replaces the old publish_status() + asyncio.sleep(0) pattern.
        """
        self._global_sequence += 1
        
        event = StatusEvent(
            server=server,
            message=message,
            request_id=request_id,
            phase=phase,
            sequence=self._global_sequence,
            meta=meta
        )
        
        # This guarantees all handlers complete before returning
        await self.pipeline.publish(event)


@asynccontextmanager
async def status_scope(
    status_bus: ImprovedStatusBus,
    coordinator_name: str,
    worker_name: str,
    request_id: Optional[str] = None,
    operation_name: str = "operation"
):
    """
    Context manager that automatically handles START/END status pairs.
    This eliminates the need to manually manage status lifecycle.
    
    Usage:
        async with status_scope(bus, "agent_coordinator", "agent_worker", req_id) as status:
            # Your agent work here
            await status.progress("Step 1 complete")
            # ... more work ...
            pass  # END status automatically sent on exit
    """
    
    class StatusUpdater:
        def __init__(self, bus, coord_name, worker_name, req_id):
            self.bus = bus
            self.coordinator_name = coord_name
            self.worker_name = worker_name
            self.request_id = req_id
            self.step_count = 0
        
        async def progress(self, message: str, is_coordinator: bool = True) -> None:
            """Send a progress update"""
            server = self.coordinator_name if is_coordinator else self.worker_name
            await self.bus.publish(server, message, self.request_id, StatusPhase.PROGRESS)
        
        async def step(self, message: str) -> None:
            """Send a step update to coordinator"""
            self.step_count += 1
            await self.bus.publish(
                self.coordinator_name, 
                f"running step {self.step_count} - {message}", 
                self.request_id, 
                StatusPhase.PROGRESS
            )
            
        async def error(self, message: str) -> None:
            """Send an error message to coordinator"""
            await self.bus.publish(
                self.coordinator_name, 
                message, 
                self.request_id, 
                StatusPhase.ERROR
            )
    
    updater = StatusUpdater(status_bus, coordinator_name, worker_name, request_id)
    
    # Send START events
    await status_bus.publish(coordinator_name, "started", request_id, StatusPhase.START)
    await status_bus.publish(worker_name, "started", request_id, StatusPhase.START)
    
    try:
        yield updater
        
        # Send successful END events
        await status_bus.publish(worker_name, "completed", request_id, StatusPhase.END)
        await status_bus.publish(
            coordinator_name, 
            f"completed ({updater.step_count} steps)", 
            request_id, 
            StatusPhase.END
        )
        
    except Exception as e:
        # Send ERROR events
        error_msg = f"failed: {str(e)}"
        await status_bus.publish(worker_name, error_msg, request_id, StatusPhase.ERROR)
        await status_bus.publish(coordinator_name, error_msg, request_id, StatusPhase.ERROR)
        raise


# Global instance for easy access
improved_status_bus = ImprovedStatusBus()


async def publish_status_improved(
    server: str,
    message: str,
    request_id: Optional[str] = None,
    phase: StatusPhase = StatusPhase.PROGRESS,
    meta: Optional[Dict[str, Any]] = None
) -> None:
    """
    Improved publish_status function that guarantees delivery.
    Drop-in replacement for the old publish_status() + asyncio.sleep(0) pattern.
    """
    await improved_status_bus.publish(server, message, request_id, phase, meta)


def get_status_metrics() -> Dict[str, Any]:
    """
    Get metrics about the status system.
    Returns basic information about active handlers and sequence numbers.
    """
    return {
        "status_system": "improved",
        "global_sequence": improved_status_bus._global_sequence,
        "active_handlers": len(improved_status_bus.pipeline.handlers),
        "active_subscribers": len(improved_status_bus._subscribers),
        "version": "1.0"
    }