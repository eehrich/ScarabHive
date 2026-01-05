"""Performance profiling utilities for async debugging.

This module provides tools to diagnose performance issues in the async FastAPI application:
- Request timing middleware
- Async task monitoring (detect blocking operations)
- Active session/task tracking
- Slow callback detection
- Memory usage tracking

Enable via environment variable: AGENT_ENABLE_PROFILING=1
"""

from __future__ import annotations

import asyncio
import functools
import gc
import logging
import os
import threading
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Dedicated profiling logger - writes to separate file
profiling_logger = logging.getLogger("agent_system.profiling")


def setup_profiling_logger() -> None:
    """Setup dedicated file logging for profiling output.
    
    Profiling logs go to logs/profiling.log instead of api.log
    to avoid cluttering the main log file.
    """
    from pathlib import Path
    
    # Create logs directory if needed
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    log_file = log_dir / "profiling.log"
    
    # Remove existing handlers to prevent duplicates
    for handler in profiling_logger.handlers[:]:
        profiling_logger.removeHandler(handler)
    
    # File handler for profiling logs
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    
    profiling_logger.addHandler(file_handler)
    profiling_logger.setLevel(logging.DEBUG)
    # Don't propagate to root logger (avoids duplicate output in api.log)
    profiling_logger.propagate = False
    
    # Also redirect asyncio logger to profiling log (for slow callback warnings from loop.set_debug(True))
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.addHandler(file_handler)
    asyncio_logger.propagate = False
    
    profiling_logger.info("=" * 60)
    profiling_logger.info("Profiling log started")
    profiling_logger.info("=" * 60)


# Configuration from environment
PROFILING_ENABLED = os.getenv("AGENT_ENABLE_PROFILING", "0") == "1"
SLOW_CALLBACK_THRESHOLD = float(os.getenv("AGENT_SLOW_CALLBACK_MS", "100")) / 1000  # Convert ms to seconds
SLOW_REQUEST_THRESHOLD = float(os.getenv("AGENT_SLOW_REQUEST_MS", "1000")) / 1000


@dataclass
class RequestMetrics:
    """Metrics for a single request."""
    request_id: str
    path: str
    method: str
    start_time: float
    end_time: Optional[float] = None
    duration_ms: Optional[float] = None
    status_code: Optional[int] = None
    error: Optional[str] = None


@dataclass
class AsyncTaskInfo:
    """Information about an async task."""
    task_id: str
    name: str
    created_at: float
    coro_name: str
    stack_summary: str
    state: str = "pending"


@dataclass
class ProfilingSnapshot:
    """Point-in-time snapshot of system state."""
    timestamp: datetime
    active_requests: int
    active_tasks: int
    slow_requests: list[RequestMetrics]
    blocked_tasks: list[AsyncTaskInfo]
    event_loop_lag_ms: float
    memory_mb: float
    thread_count: int


class RequestProfiler:
    """Tracks request timing and identifies slow requests."""
    
    def __init__(self, slow_threshold: float = SLOW_REQUEST_THRESHOLD):
        self.slow_threshold = slow_threshold
        self._active_requests: dict[str, RequestMetrics] = {}
        self._completed_requests: list[RequestMetrics] = []
        self._max_history = 1000
        self._lock = threading.Lock()
        
        # Aggregated stats
        self._request_counts: dict[str, int] = defaultdict(int)  # path -> count
        self._total_duration_by_path: dict[str, float] = defaultdict(float)  # path -> total_ms
        self._slow_count_by_path: dict[str, int] = defaultdict(int)  # path -> slow_count
    
    def start_request(self, request_id: str, path: str, method: str) -> None:
        """Record start of a request."""
        metrics = RequestMetrics(
            request_id=request_id,
            path=path,
            method=method,
            start_time=time.perf_counter()
        )
        with self._lock:
            self._active_requests[request_id] = metrics
    
    def end_request(self, request_id: str, status_code: int, error: Optional[str] = None) -> Optional[RequestMetrics]:
        """Record end of a request and return metrics."""
        with self._lock:
            metrics = self._active_requests.pop(request_id, None)
            if metrics:
                metrics.end_time = time.perf_counter()
                metrics.duration_ms = (metrics.end_time - metrics.start_time) * 1000
                metrics.status_code = status_code
                metrics.error = error
                
                # Update aggregated stats
                self._request_counts[metrics.path] += 1
                self._total_duration_by_path[metrics.path] += metrics.duration_ms
                if metrics.duration_ms > self.slow_threshold * 1000:
                    self._slow_count_by_path[metrics.path] += 1
                
                # Keep history
                self._completed_requests.append(metrics)
                if len(self._completed_requests) > self._max_history:
                    self._completed_requests = self._completed_requests[-self._max_history:]
                
                return metrics
        return None
    
    def get_active_requests(self) -> list[RequestMetrics]:
        """Get currently active requests."""
        with self._lock:
            now = time.perf_counter()
            result = []
            for metrics in self._active_requests.values():
                # Create a copy with current duration
                m = RequestMetrics(
                    request_id=metrics.request_id,
                    path=metrics.path,
                    method=metrics.method,
                    start_time=metrics.start_time,
                    duration_ms=(now - metrics.start_time) * 1000
                )
                result.append(m)
            return result
    
    def get_slow_requests(self, limit: int = 20) -> list[RequestMetrics]:
        """Get recent slow requests."""
        with self._lock:
            slow = [r for r in self._completed_requests if r.duration_ms and r.duration_ms > self.slow_threshold * 1000]
            return sorted(slow, key=lambda r: r.duration_ms or 0, reverse=True)[:limit]
    
    def get_stats(self) -> dict[str, Any]:
        """Get aggregated statistics."""
        with self._lock:
            stats = {}
            for path in self._request_counts:
                count = self._request_counts[path]
                total_ms = self._total_duration_by_path[path]
                slow_count = self._slow_count_by_path[path]
                stats[path] = {
                    "count": count,
                    "avg_ms": total_ms / count if count > 0 else 0,
                    "slow_count": slow_count,
                    "slow_pct": (slow_count / count * 100) if count > 0 else 0
                }
            return stats
    
    def reset_stats(self) -> None:
        """Reset all statistics."""
        with self._lock:
            self._request_counts.clear()
            self._total_duration_by_path.clear()
            self._slow_count_by_path.clear()
            self._completed_requests.clear()


class AsyncTaskMonitor:
    """Monitors async tasks and detects blocking operations."""
    
    def __init__(self, slow_threshold: float = SLOW_CALLBACK_THRESHOLD):
        self.slow_threshold = slow_threshold
        self._tracked_tasks: dict[int, AsyncTaskInfo] = {}
        self._slow_callbacks: list[dict[str, Any]] = []
        self._max_slow_history = 100
        self._lock = threading.Lock()
        self._monitoring = False
        self._original_time_func: Optional[Callable] = None
        self._last_check_time: float = 0
    
    def start_monitoring(self) -> None:
        """Start monitoring async tasks and event loop."""
        if self._monitoring:
            return
        
        self._monitoring = True
        loop = asyncio.get_event_loop()
        
        # Enable slow callback warnings
        loop.slow_callback_duration = self.slow_threshold
        
        # Set debug mode to catch slow callbacks
        if hasattr(loop, 'set_debug'):
            loop.set_debug(True)
        
        profiling_logger.info(f"Async task monitoring started (slow_threshold={self.slow_threshold*1000:.0f}ms)")
    
    def stop_monitoring(self) -> None:
        """Stop monitoring."""
        self._monitoring = False
        try:
            loop = asyncio.get_event_loop()
            if hasattr(loop, 'set_debug'):
                loop.set_debug(False)
        except RuntimeError:
            pass
        profiling_logger.info("Async task monitoring stopped")
    
    def track_task(self, task: asyncio.Task) -> None:
        """Add a task to tracking."""
        task_id = id(task)
        coro = task.get_coro()
        coro_name = getattr(coro, '__qualname__', str(coro))
        
        # Get stack info
        try:
            stack_frames = traceback.extract_stack(limit=10)
            stack_summary = '\n'.join(traceback.format_list(stack_frames[-5:]))
        except Exception:
            stack_summary = "Unable to capture stack"
        
        info = AsyncTaskInfo(
            task_id=str(task_id),
            name=task.get_name(),
            created_at=time.perf_counter(),
            coro_name=coro_name,
            stack_summary=stack_summary
        )
        
        with self._lock:
            self._tracked_tasks[task_id] = info
        
        # Add done callback to clean up
        task.add_done_callback(lambda t: self._on_task_done(task_id))
    
    def _on_task_done(self, task_id: int) -> None:
        """Called when a tracked task completes."""
        with self._lock:
            self._tracked_tasks.pop(task_id, None)
    
    def get_active_tasks(self) -> list[AsyncTaskInfo]:
        """Get all active tracked tasks."""
        with self._lock:
            return list(self._tracked_tasks.values())
    
    def get_all_tasks_info(self, include_stack: bool = False) -> list[dict[str, Any]]:
        """Get info about ALL asyncio tasks (not just tracked ones).
        
        Args:
            include_stack: If True, capture stack traces (expensive, creates FrameSummary objects).
                          Default False to avoid memory accumulation.
        """
        try:
            all_tasks = asyncio.all_tasks()
        except RuntimeError:
            return []
        
        result = []
        for task in all_tasks:
            coro = task.get_coro()
            coro_name = getattr(coro, '__qualname__', str(coro))
            
            task_info: dict[str, Any] = {
                "name": task.get_name(),
                "coro": coro_name,
                "done": task.done(),
                "cancelled": task.cancelled(),
            }
            
            # Only capture stack if explicitly requested (expensive operation)
            if include_stack:
                try:
                    stack = task.get_stack(limit=3)
                    if stack:
                        # Format directly to string without keeping FrameSummary objects
                        task_info["stack"] = '\n'.join(
                            traceback.format_list(traceback.extract_stack(stack[0], limit=3))
                        )
                    else:
                        task_info["stack"] = "No stack available"
                except Exception:
                    task_info["stack"] = "Unable to capture stack"
            
            result.append(task_info)
        
        return result
    
    def record_slow_callback(self, duration: float, callback_info: str) -> None:
        """Record a slow callback event."""
        with self._lock:
            self._slow_callbacks.append({
                "timestamp": datetime.now().isoformat(),
                "duration_ms": duration * 1000,
                "callback": callback_info
            })
            if len(self._slow_callbacks) > self._max_slow_history:
                self._slow_callbacks = self._slow_callbacks[-self._max_slow_history:]
    
    def get_slow_callbacks(self) -> list[dict[str, Any]]:
        """Get recorded slow callbacks."""
        with self._lock:
            return list(self._slow_callbacks)


class EventLoopMonitor:
    """Monitors event loop health and detects blocking."""
    
    def __init__(self):
        self._last_tick: float = 0
        self._lag_samples: list[float] = []
        self._max_samples = 100
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def start(self) -> None:
        """Start the event loop monitor."""
        if self._running:
            return
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        profiling_logger.info("Event loop monitor started")
    
    async def stop(self) -> None:
        """Stop the event loop monitor."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        profiling_logger.info("Event loop monitor stopped")
    
    async def _monitor_loop(self) -> None:
        """Background task that measures event loop responsiveness."""
        expected_interval = 0.1  # 100ms check interval
        
        while self._running:
            start = time.perf_counter()
            await asyncio.sleep(expected_interval)
            actual = time.perf_counter() - start
            
            # Lag is how much longer than expected
            lag = (actual - expected_interval) * 1000  # Convert to ms
            if lag > 0:
                self._lag_samples.append(lag)
                if len(self._lag_samples) > self._max_samples:
                    self._lag_samples = self._lag_samples[-self._max_samples:]
                
                # Warn on significant lag
                if lag > 100:  # More than 100ms lag
                    profiling_logger.warning(f"Event loop lag detected: {lag:.1f}ms")
    
    def get_lag_stats(self) -> dict[str, float]:
        """Get event loop lag statistics."""
        if not self._lag_samples:
            return {"current_ms": 0, "avg_ms": 0, "max_ms": 0, "samples": 0}
        
        return {
            "current_ms": self._lag_samples[-1] if self._lag_samples else 0,
            "avg_ms": sum(self._lag_samples) / len(self._lag_samples),
            "max_ms": max(self._lag_samples),
            "samples": len(self._lag_samples)
        }


class MemoryMonitor:
    """Tracks memory usage."""
    
    @staticmethod
    def get_memory_mb() -> float:
        """Get current process memory usage in MB."""
        try:
            import psutil
            process = psutil.Process()
            return process.memory_info().rss / (1024 * 1024)
        except ImportError:
            # Fallback without psutil - only works on Unix
            try:
                import resource  # type: ignore[import-not-found]
                return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # type: ignore[attr-defined]
            except (ImportError, AttributeError):
                return 0.0
        except Exception:
            return 0.0
    
    @staticmethod
    def get_memory_details() -> dict[str, Any]:
        """Get detailed memory information."""
        try:
            import psutil
            process = psutil.Process()
            mem = process.memory_info()
            return {
                "rss_mb": mem.rss / (1024 * 1024),
                "vms_mb": mem.vms / (1024 * 1024),
                "percent": process.memory_percent(),
                "num_fds": process.num_fds() if hasattr(process, 'num_fds') else None,
                "num_threads": process.num_threads(),
                "gc_counts": gc.get_count(),
                "gc_objects": len(gc.get_objects())
            }
        except ImportError:
            return {"error": "psutil not installed"}
        except Exception as e:
            return {"error": str(e)}


# Global profiler instances
_request_profiler: Optional[RequestProfiler] = None
_task_monitor: Optional[AsyncTaskMonitor] = None
_loop_monitor: Optional[EventLoopMonitor] = None


def get_profiler() -> RequestProfiler:
    """Get the global request profiler."""
    global _request_profiler
    if _request_profiler is None:
        _request_profiler = RequestProfiler()
    return _request_profiler


def get_task_monitor() -> AsyncTaskMonitor:
    """Get the global task monitor."""
    global _task_monitor
    if _task_monitor is None:
        _task_monitor = AsyncTaskMonitor()
    return _task_monitor


def get_loop_monitor() -> EventLoopMonitor:
    """Get the global event loop monitor."""
    global _loop_monitor
    if _loop_monitor is None:
        _loop_monitor = EventLoopMonitor()
    return _loop_monitor


async def create_profiling_middleware(app: "FastAPI") -> None:
    """Add profiling middleware to FastAPI app.
    
    Uses Pure ASGI to avoid BaseHTTPMiddleware overhead.
    """
    import uuid
    from starlette.types import ASGIApp, Receive, Scope, Send
    
    profiler = get_profiler()
    
    class ProfilingMiddleware:
        """Pure ASGI profiling middleware."""
        
        def __init__(self, app: ASGIApp):
            self.app = app
        
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http" or not PROFILING_ENABLED:
                await self.app(scope, receive, send)
                return
            
            request_id = str(uuid.uuid4())[:8]
            path = scope.get("path", "")
            method = scope.get("method", "")
            
            profiler.start_request(request_id, path, method)
            
            status_code = 500
            error = None
            
            async def send_with_profiling(message):
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = message.get("status", 500)
                await send(message)
            
            try:
                await self.app(scope, receive, send_with_profiling)
            except Exception as e:
                error = str(e)
                raise
            finally:
                metrics = profiler.end_request(request_id, status_code, error)
                if metrics and metrics.duration_ms and metrics.duration_ms > SLOW_REQUEST_THRESHOLD * 1000:
                    profiling_logger.warning(
                        f"Slow request: {method} {path} took {metrics.duration_ms:.1f}ms "
                        f"(threshold: {SLOW_REQUEST_THRESHOLD*1000:.0f}ms)"
                    )
    
    # Wrap the ASGI app
    app.add_middleware(ProfilingMiddleware)
    profiling_logger.info("Profiling middleware installed")


def profile_async(name: Optional[str] = None):
    """Decorator to profile async functions."""
    def decorator(func: Callable) -> Callable:
        func_name = name or func.__qualname__
        
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if not PROFILING_ENABLED:
                return await func(*args, **kwargs)
            
            start = time.perf_counter()
            try:
                return await func(*args, **kwargs)
            finally:
                duration = (time.perf_counter() - start) * 1000
                if duration > SLOW_CALLBACK_THRESHOLD * 1000:
                    profiling_logger.warning(f"Slow async function: {func_name} took {duration:.1f}ms")
        
        return wrapper
    return decorator


def get_profiling_snapshot() -> ProfilingSnapshot:
    """Get a complete profiling snapshot."""
    profiler = get_profiler()
    task_monitor = get_task_monitor()
    loop_monitor = get_loop_monitor()
    
    active_requests = profiler.get_active_requests()
    slow_requests = profiler.get_slow_requests()
    all_tasks = task_monitor.get_all_tasks_info()
    lag_stats = loop_monitor.get_lag_stats()
    
    return ProfilingSnapshot(
        timestamp=datetime.now(),
        active_requests=len(active_requests),
        active_tasks=len(all_tasks),
        slow_requests=slow_requests,
        blocked_tasks=task_monitor.get_active_tasks(),
        event_loop_lag_ms=lag_stats.get("current_ms", 0),
        memory_mb=MemoryMonitor.get_memory_mb(),
        thread_count=threading.active_count()
    )


def get_profiling_report() -> dict[str, Any]:
    """Generate a comprehensive profiling report."""
    profiler = get_profiler()
    task_monitor = get_task_monitor()
    loop_monitor = get_loop_monitor()
    
    return {
        "enabled": PROFILING_ENABLED,
        "timestamp": datetime.now().isoformat(),
        "thresholds": {
            "slow_request_ms": SLOW_REQUEST_THRESHOLD * 1000,
            "slow_callback_ms": SLOW_CALLBACK_THRESHOLD * 1000
        },
        "requests": {
            "active": [
                {
                    "id": r.request_id,
                    "path": r.path,
                    "method": r.method,
                    "duration_ms": r.duration_ms
                }
                for r in profiler.get_active_requests()
            ],
            "slow_recent": [
                {
                    "id": r.request_id,
                    "path": r.path,
                    "method": r.method,
                    "duration_ms": r.duration_ms,
                    "status": r.status_code
                }
                for r in profiler.get_slow_requests(10)
            ],
            "stats_by_path": profiler.get_stats()
        },
        "async_tasks": {
            "total_count": len(task_monitor.get_all_tasks_info()),
            "tasks": task_monitor.get_all_tasks_info()[:20],  # Limit to 20
            "slow_callbacks": task_monitor.get_slow_callbacks()[-10:]
        },
        "event_loop": loop_monitor.get_lag_stats(),
        "memory": MemoryMonitor.get_memory_details(),
        "threads": {
            "count": threading.active_count(),
            "names": [t.name for t in threading.enumerate()]
        }
    }


async def start_profiling() -> None:
    """Start all profiling components."""
    if not PROFILING_ENABLED:
        logger.info("Profiling disabled (set AGENT_ENABLE_PROFILING=1 to enable)")
        return
    
    # Setup dedicated profiling logger
    setup_profiling_logger()
    
    profiling_logger.info("Starting performance profiling...")
    profiling_logger.info(f"Slow request threshold: {SLOW_REQUEST_THRESHOLD * 1000}ms")
    profiling_logger.info(f"Slow callback threshold: {SLOW_CALLBACK_THRESHOLD * 1000}ms")
    
    # Start monitors
    task_monitor = get_task_monitor()
    task_monitor.start_monitoring()
    
    loop_monitor = get_loop_monitor()
    await loop_monitor.start()
    
    profiling_logger.info("Performance profiling started")
    logger.info("Performance profiling started (logs in logs/profiling.log)")


async def stop_profiling() -> None:
    """Stop all profiling components."""
    task_monitor = get_task_monitor()
    task_monitor.stop_monitoring()
    
    loop_monitor = get_loop_monitor()
    await loop_monitor.stop()
    
    profiling_logger.info("Performance profiling stopped")
    profiling_logger.info("=" * 60)
    logger.info("Performance profiling stopped")
