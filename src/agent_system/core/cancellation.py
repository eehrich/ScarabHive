"""
Cancellation System for Agent Tools
Provides graceful and forced cancellation for tool execution.
"""
import asyncio
import inspect
import logging
import time
from typing import Dict, Set, Optional, Callable
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class CancellationToken:
    """Token that tools can check for cancellation signals."""
    
    def __init__(self, request_id: str, cleanup_timeout: float = 10.0):
        self.request_id = request_id
        self.cleanup_timeout = cleanup_timeout
        self._cancelled = asyncio.Event()
        self._forced = asyncio.Event()
        self._cleanup_callbacks: Set[Callable] = set()
        self._cancel_time: Optional[float] = None
    
    @property
    def is_cancelled(self) -> bool:
        """Check if cancellation was requested."""
        return self._cancelled.is_set()
    
    @property
    def is_forced(self) -> bool:
        """Check if forced termination is active."""
        return self._forced.is_set()
    
    def cancel(self) -> None:
        """Request graceful cancellation."""
        if not self._cancelled.is_set():
            self._cancel_time = time.time()
            self._cancelled.set()
            logger.info("Graceful cancellation requested for %s", self.request_id)
        else:
            logger.info("Cancellation already requested for %s", self.request_id)
    
    def force(self) -> None:
        """Force immediate termination."""
        self._forced.set()
        logger.warning("Forced termination for %s", self.request_id)
    
    async def wait_for_cancellation(self) -> None:
        """Wait for cancellation signal."""
        await self._cancelled.wait()
    
    def add_cleanup_callback(self, callback: Callable) -> None:
        """Add cleanup function to call on cancellation."""
        self._cleanup_callbacks.add(callback)
    
    def remove_cleanup_callback(self, callback: Callable) -> None:
        """Remove cleanup function."""
        self._cleanup_callbacks.discard(callback)
    
    async def cleanup(self) -> None:
        """Execute all cleanup callbacks."""
        for callback in self._cleanup_callbacks:
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                logger.error("Cleanup callback failed: %s", e)
    
    def should_force(self) -> bool:
        """Check if cleanup timeout exceeded and forced termination needed."""
        if not self._cancel_time:
            return False
        return (time.time() - self._cancel_time) > self.cleanup_timeout


class CancellationManager:
    """Manages cancellation tokens and enforces timeouts."""
    
    def __init__(self, default_cleanup_timeout: float = 10.0, monitor_interval: float = 1.0):
        self.default_cleanup_timeout = default_cleanup_timeout
        self.monitor_interval = monitor_interval
        self._tokens: Dict[str, CancellationToken] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._protected: Set[str] = set()  # cleanup requests no cascade cancels (protect)
        self._monitor_task: Optional[asyncio.Task] = None
        self._shutdown = asyncio.Event()
    
    def create_token(self, request_id: str, cleanup_timeout: Optional[float] = None) -> CancellationToken:
        """Create a new cancellation token."""
        timeout = cleanup_timeout or self.default_cleanup_timeout
        token = CancellationToken(request_id, timeout)
        self._tokens[request_id] = token
        
        # Start monitor if not running
        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_timeouts())
        
        return token
    
    def get_token(self, request_id: str) -> Optional[CancellationToken]:
        """Get existing token."""
        return self._tokens.get(request_id)
    
    def cancel_request(self, request_id: str) -> bool:
        """Request cancellation of a specific request and all its sub-requests."""
        cancelled_count = 0
        
        # First, try exact match
        token = self._tokens.get(request_id)
        if token and not self._is_protected(request_id):
            token.cancel()
            cancelled_count += 1
        
        # Then, find all tokens with request_id as prefix (for tool-specific request IDs like "abc123_001")
        cancelled_count += self.cancel_sub_requests(request_id)
        
        if cancelled_count > 0:
            logger.info("Cancelled %d request(s) with ID prefix '%s'", cancelled_count, request_id)
            return True
        
        logger.warning("No cancellation tokens found for request ID '%s'", request_id)
        return False
    
    def protect(self, request_id: str) -> None:
        """Keep ``request_id`` and its sub-requests out of every cascade -- cancel_request of an id above it,
        cancel_sub_requests: cleanup work that must run to its end while the requests around it are cancelled.
        The timeout monitor's forced cancel of a cancelled prefix still reaches it."""
        self._protected.add(request_id)

    def unprotect(self, request_id: str) -> None:
        self._protected.discard(request_id)

    def _is_protected(self, token_id: str) -> bool:
        return any(token_id == kept or token_id.startswith(kept + "_") for kept in self._protected)

    def cancel_sub_requests(self, request_id: str) -> int:
        """Cancel the sub-requests of a request (ids ``<request_id>_...``), not the request itself.

        Its own token stays uncancelled, so the timeout monitor never force-cancels its whole prefix --
        sub-requests it starts afterwards (cleanup work) are not cut. Nor are protected ones (``protect``).
        """
        cancelled_count = 0
        for token_id, token in list(self._tokens.items()):
            if token_id.startswith(request_id + "_") and not self._is_protected(token_id):
                token.cancel()
                cancelled_count += 1
        return cancelled_count

    def register_task(self, request_id: str, task: asyncio.Task) -> None:
        """Register a task for potential forced cancellation."""
        self._tasks[request_id] = task
    
    def cancel_tasks_by_prefix(self, request_id_prefix: str) -> int:
        """Cancel all tasks with the given request ID prefix."""
        cancelled_count = 0
        
        for task_id, task in list(self._tasks.items()):
            if task_id == request_id_prefix or task_id.startswith(request_id_prefix + "_"):
                if not task.done():
                    task.cancel()
                    cancelled_count += 1
                    logger.info("Force cancelled task for %s", task_id)
        
        return cancelled_count
    
    def unregister_request(self, request_id: str) -> None:
        """Clean up after request completion."""
        self._tokens.pop(request_id, None)
        self._tasks.pop(request_id, None)
    
    async def _monitor_timeouts(self) -> None:
        """Monitor for cleanup timeouts and force cancellation."""
        while not self._shutdown.is_set():
            try:
                for request_id, token in list(self._tokens.items()):
                    if token.should_force() and not token.is_forced:
                        token.force()
                        
                        # Force cancel all tasks with this prefix (main and tool-specific)
                        cancelled_tasks = []
                        for task_id, task in list(self._tasks.items()):
                            if (task_id == request_id or task_id.startswith(request_id + "_")) and not task.done():
                                task.cancel()
                                cancelled_tasks.append(task_id)
                        
                        if cancelled_tasks:
                            logger.warning("Force cancelled %d task(s) for %s: %s", 
                                         len(cancelled_tasks), request_id, cancelled_tasks)
                
                # Check at configured interval
                await asyncio.sleep(self.monitor_interval)
            except Exception as e:
                logger.error("Error in cancellation monitor: %s", e)
                await asyncio.sleep(self.monitor_interval)
    
    async def shutdown(self) -> None:
        """Shutdown the cancellation manager."""
        self._shutdown.set()
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass


# Global cancellation manager instance
_cancellation_manager: Optional[CancellationManager] = None


def get_cancellation_manager() -> CancellationManager:
    """Get the global cancellation manager."""
    global _cancellation_manager
    if _cancellation_manager is None:
        _cancellation_manager = CancellationManager()
    return _cancellation_manager


def configure_cancellation_manager(cleanup_timeout: float = 10.0, monitor_interval: float = 1.0) -> None:
    """Configure the global cancellation manager with custom timeouts.

    Call once at process bootstrap (bootstrap_servers). Idempotent for
    unchanged values: replacing the manager orphans every token/task
    registered in the old instance (their cancellation silently stops
    working), so a re-call with identical settings must NOT swap it —
    historically this ran in every Agent.__init__ and did exactly that.
    """
    global _cancellation_manager

    if _cancellation_manager is not None:
        # Same settings → keep the live manager and all its registered tokens.
        if (_cancellation_manager.default_cleanup_timeout == cleanup_timeout
                and _cancellation_manager.monitor_interval == monitor_interval):
            return
        # Changed settings → shut the old manager down gracefully first.
        # Loud warning when live tokens exist: replacing the manager orphans
        # them (their cancellation silently stops firing) — if this appears in
        # logs outside process bootstrap, something reconfigures too late.
        live_tokens = len(getattr(_cancellation_manager, "_tokens", {}))
        if live_tokens:
            logger.warning(
                "Replacing cancellation manager with %d live token(s) — their "
                "cancellation will no longer fire (reconfigure after bootstrap?)",
                live_tokens)
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule shutdown for later if we're in an event loop
                loop.create_task(_cancellation_manager.shutdown())
        except Exception:
            pass  # Ignore shutdown errors during reconfiguration

    # Create new manager with updated configuration
    _cancellation_manager = CancellationManager(
        default_cleanup_timeout=cleanup_timeout,
        monitor_interval=monitor_interval
    )
    logger.info("Configured cancellation manager: cleanup_timeout=%.1fs, monitor_interval=%.1fs",
                cleanup_timeout, monitor_interval)


@asynccontextmanager
async def cancellable_operation(request_id: str, cleanup_timeout: float = 10.0):
    """Context manager for cancellable operations."""
    manager = get_cancellation_manager()
    token = manager.create_token(request_id, cleanup_timeout)
    
    try:
        yield token
    finally:
        # Run cleanup if cancelled
        if token.is_cancelled:
            await token.cleanup()
        manager.unregister_request(request_id)


class CancellationError(Exception):
    """Raised when operation is cancelled."""
    
    def __init__(self, request_id: str, forced: bool = False):
        self.request_id = request_id
        self.forced = forced
        msg = f"Operation {request_id} was {'force ' if forced else ''}cancelled"
        super().__init__(msg)