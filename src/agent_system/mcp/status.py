"""
Status System - Guaranteed message delivery without timing dependencies
======================================================================

This module provides a robust status messaging system that eliminates
asyncio.sleep(0) anti-patterns through guaranteed delivery mechanisms.
"""

import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import asyncio
from contextvars import ContextVar

from ..utils.tree_hierarchy import get_tree_builder

logger = logging.getLogger(__name__)


class StatusPhase(Enum):
    START = "start"
    PROGRESS = "progress" 
    END = "end"
    ERROR = "error"


@dataclass
class StatusEvent:
    server: str
    request_id: Optional[str]
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    phase: StatusPhase = StatusPhase.PROGRESS
    sequence: int = 0  # Will be assigned by bus if 0
    level: str = "info"  # info, warning, error
    meta: Optional[Dict[str, Any]] = None
    # Tree hierarchy metadata
    parent_id: Optional[str] = None
    depth_level: int = 0
    child_count: int = 0
    is_leaf: bool = True

    def to_dict(self) -> dict:
        return {
            "server": self.server,
            "request_id": self.request_id,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "phase": self.phase.value,  # Convert enum to string
            "level": self.level,
            "sequence": self.sequence,
            "meta": self.meta,
            "tree": {
                "parent_id": self.parent_id,
                "depth_level": self.depth_level,
                "child_count": self.child_count,
                "is_leaf": self.is_leaf
            }
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
                "meta": event.meta,
                "tree": {
                    "parent_id": event.parent_id,
                    "depth_level": event.depth_level,
                    "child_count": event.child_count,
                    "is_leaf": event.is_leaf
                }
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


class FilteredQueueStatusHandler(QueueStatusHandler):
    """Queue handler with optional server and request_id filtering"""
    
    def __init__(self, queue: asyncio.Queue, server_filter: Optional[str] = None, 
                 request_id_filter: Optional[str] = None):
        super().__init__(queue)
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


class StatusBus:
    """Central status event bus with guaranteed delivery"""
    
    def __init__(self):
        self.handlers: List[StatusHandler] = []
        self.sequence_counter = 0
        self._lock = asyncio.Lock()
        # Track handlers by queue for unsubscribe support
        self._queue_handlers: Dict[asyncio.Queue, StatusHandler] = {}
        
        # Metrics tracking
        self.publish_attempted = 0
        self.delivered = 0
    
    def add_handler(self, handler: StatusHandler) -> None:
        """Add a status handler"""
        self.handlers.append(handler)
        
    def remove_handler(self, handler: StatusHandler) -> None:
        """Remove a status handler"""
        if handler in self.handlers:
            self.handlers.remove(handler)
    
    async def publish(self, event: StatusEvent) -> None:
        """Publish a status event with guaranteed delivery to all handlers"""
        async with self._lock:
            # Track metrics
            self.publish_attempted += 1
            
            # Update sequence if not set
            if event.sequence == 0:
                self.sequence_counter += 1
                event.sequence = self.sequence_counter
            
            # Compute tree hierarchy metadata if request_id is available
            if event.request_id:
                tree_builder = get_tree_builder()
                node = tree_builder._ensure_node_exists(event.request_id)
                event.parent_id = node.parent_id
                event.depth_level = node.depth
                event.child_count = len(node.children)
                event.is_leaf = len(node.children) == 0
            
            # Deliver to all handlers - guaranteed processing
            for handler in self.handlers:
                try:
                    await handler.process(event)
                    self.delivered += 1
                except Exception as e:
                    logger.error(f"Handler {handler.__class__.__name__} failed: {e}")
    
    def get_status_metrics(self) -> dict:
        """Get status bus metrics"""
        return {
            "handlers_count": len(self.handlers),
            "sequence_counter": self.sequence_counter,
            "handler_types": [h.__class__.__name__ for h in self.handlers],
            "subscribers": len([h for h in self.handlers if isinstance(h, (QueueStatusHandler, FilteredQueueStatusHandler))]),
            "publish_attempted": self.publish_attempted,
            "delivered": self.delivered,
        }
    
    async def subscribe(self, server: Optional[str] = None, 
                  request_id: Optional[str] = None) -> asyncio.Queue:
        """Subscribe to status events with optional filtering
        
        Args:
            server: Only receive events from this server (optional)
            request_id: Only receive events with this request_id (optional)
            
        Returns:
            Queue that will receive filtered StatusEvent objects
        """
        queue: asyncio.Queue = asyncio.Queue()
        handler = FilteredQueueStatusHandler(queue, server, request_id)
        self.add_handler(handler)
        # Track the handler for unsubscribe
        self._queue_handlers[queue] = handler
        return queue
    
    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Unsubscribe from status events by removing the queue's handler"""
        if queue in self._queue_handlers:
            handler = self._queue_handlers[queue]
            self.remove_handler(handler)
            del self._queue_handlers[queue]


# Global status bus instance
status_bus = StatusBus()

# ContextVar to hold the current request id for automatic propagation
current_request_id: ContextVar[Optional[str]] = ContextVar('current_request_id', default=None)


def get_status_bus() -> StatusBus:
    """Get the global status bus instance"""
    return status_bus


def get_status_metrics() -> Dict[str, Any]:
    """Get metrics about the status system"""
    return status_bus.get_status_metrics()


def _calculate_tree_metadata(request_id: Optional[str]) -> tuple[Optional[str], int]:
    """Calculate parent_id and depth_level from request_id.
    Returns (parent_id, depth_level)."""
    parent_id = None
    depth_level = 0
    if request_id:
        from ..utils.tree_hierarchy import parse_request_id_hierarchy
        try:
            hierarchy = parse_request_id_hierarchy(request_id)
            parent_id = hierarchy.get("parent_id")
            depth_level = hierarchy.get("depth", 0)
        except Exception:
            # If parsing fails, fall back to defaults
            pass
    return parent_id, depth_level


async def publish_status(server: str, message: str, request_id: Optional[str] = None, 
                        phase: StatusPhase = StatusPhase.PROGRESS, level: str = "info",
                        meta: Optional[Dict[str, Any]] = None) -> None:
    """Publish a status event with guaranteed delivery.
    Automatically calculates tree hierarchy metadata from request_id."""
    # Auto-escalate level based on phase
    if phase == StatusPhase.ERROR and level == "info":
        level = "error"
    
    # If no request_id provided, try to read from ContextVar for implicit propagation
    if not request_id:
        try:
            rid = current_request_id.get()
            if rid:
                request_id = rid
        except Exception:
            # ignore context var errors
            pass

    # Calculate tree hierarchy metadata from request_id
    parent_id, depth_level = _calculate_tree_metadata(request_id)

    # Create StatusEvent and let bus.publish handle sequencing
    event = StatusEvent(
        server=server,
        request_id=request_id,
        message=message,
        phase=phase,
        sequence=0,  # Bus will assign sequence
        level=level,
        meta=meta,
        parent_id=parent_id,
        depth_level=depth_level
    )
    await status_bus.publish(event)


class StatusScope:
    """Context manager for automatic START/END status pairing with step and progress tracking"""
    
    def __init__(self, bus: StatusBus, server: str, request_id: Optional[str] = None, start_msg: Optional[str] = None, end_msg: Optional[str] = None):
        self.bus = bus
        self.server = server
        # If no request_id provided, try to read from ContextVar for implicit propagation
        if not request_id:
            try:
                rid = current_request_id.get()
                if rid:
                    request_id = rid
            except Exception:
                # ignore context var errors
                pass
        self.request_id = request_id
        self.ended = False
        self.start_msg = start_msg or "started"
        self.end_msg = end_msg or "completed"
        
    async def __aenter__(self):
        # Send START message with error handling
        try:
            parent_id, depth_level = _calculate_tree_metadata(self.request_id)
            await self.bus.publish(StatusEvent(
                server=self.server,
                request_id=self.request_id,
                message=self.start_msg,
                phase=StatusPhase.START,
                parent_id=parent_id,
                depth_level=depth_level
            ))
        except Exception as e:
            # Gracefully handle status publishing failures
            # Don't let status publishing failures break the actual operation
            import logging
            logging.getLogger(__name__).warning(f"Failed to publish status START event: {e}")
        self.ended = False
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if not self.ended:
            self.ended = True
            try:
                parent_id, depth_level = _calculate_tree_metadata(self.request_id)
                if exc_type is not None:
                    # Error occurred - ensure we have a meaningful error message
                    error_msg = str(exc_val) if exc_val else "Unknown error"
                    if not error_msg or error_msg.strip() == "":
                        error_msg = f"{exc_type.__name__} occurred"
                    
                    # Build meta with error information
                    error_meta = {
                        "error_type": exc_type.__name__
                    }
                    
                    await self.bus.publish(StatusEvent(
                        server=self.server,
                        request_id=self.request_id,
                        message=f"failed: {error_msg}",
                        phase=StatusPhase.ERROR,
                        level="error",
                        meta=error_meta,
                        parent_id=parent_id,
                        depth_level=depth_level
                    ))
                else:
                    # Success
                    await self.bus.publish(StatusEvent(
                        server=self.server,
                        request_id=self.request_id,
                        message=self.end_msg,
                        phase=StatusPhase.END,
                        parent_id=parent_id,
                        depth_level=depth_level
                    ))
            except Exception as e:
                # Gracefully handle status publishing failures
                # Don't let status publishing failures break the actual operation
                import logging
                logging.getLogger(__name__).warning(f"Failed to publish status END event: {e}")
    
    async def progress(self, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
        """Report a step or progress in the process"""
        try:
            parent_id, depth_level = _calculate_tree_metadata(self.request_id)
            await self.bus.publish(StatusEvent(
                server=self.server,
                request_id=self.request_id,
                message=message,
                phase=StatusPhase.PROGRESS,
                meta=meta,
                parent_id=parent_id,
                depth_level=depth_level
            ))
        except Exception:
            # Gracefully handle status publishing failures
            pass
    
    async def end(self, message: str = "completed", meta: Optional[Dict[str, Any]] = None) -> None:
        """Manually end the scope with a custom message"""
        if not self.ended:
            self.ended = True
            try:
                parent_id, depth_level = _calculate_tree_metadata(self.request_id)
                await self.bus.publish(StatusEvent(
                    server=self.server,
                    request_id=self.request_id,
                    message=message,
                    phase=StatusPhase.END,
                    meta=meta,
                    parent_id=parent_id,
                    depth_level=depth_level
                ))
            except Exception:
                # Gracefully handle status publishing failures
                pass
        
    async def error(self, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
        """Report an error in the process"""
        self.ended = True
        parent_id, depth_level = _calculate_tree_metadata(self.request_id)
        await self.bus.publish(StatusEvent(
            server=self.server,
            request_id=self.request_id,
            message=message,
            phase=StatusPhase.ERROR,
            level="error",
            meta=meta,
            parent_id=parent_id,
            depth_level=depth_level
        ))



@asynccontextmanager
async def status_scope(bus: StatusBus, name: str, request_id: Optional[str] = None, start_msg: Optional[str] = None, end_msg: Optional[str] = None):
    """Status scope context manager for automatic START/END pairing
    
    Args:
        bus: StatusBus instance to publish events to
        name: Name for the status scope (used as server name)
        request_id: Optional request ID for correlation
        start_msg: Optional custom start message
        end_msg: Optional custom end message
    Usage:
        async with status_scope(status_bus, "my_agent", request_id=req_id) as status:
            await status.progress("Processing data", meta={"step": 1})
            await status.progress("50% complete", meta={"progress": 0.5})
            # Optional explicit control:
            # await status.end("Custom completion message")
            # await status.error("Something went wrong")
    """
    scope = StatusScope(bus, name, request_id, start_msg=start_msg, end_msg=end_msg)
    async with scope:
        yield scope