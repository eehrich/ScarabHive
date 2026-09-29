"""Performance profiling utilities for async debugging.

This module provides tools to diagnose performance issues in the async FastAPI application:
- Request timing middleware
- Async task listing
- Event loop lag
- Memory and thread details

Enable via environment variable: AGENT_ENABLE_PROFILING=1
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING

from agent_system.utils.logging import loggable_path

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
        self._min_duration_by_path: dict[str, float] = {}  # path -> min_ms
        self._max_duration_by_path: dict[str, float] = {}  # path -> max_ms
    
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
    
    def end_request(self, request_id: str, status_code: int, error: Optional[str] = None,
                    stats_path: Optional[str] = None) -> Optional[RequestMetrics]:
        """Record end of a request and return metrics.

        stats_path: what the aggregated stats count the request under -- the route
        template, so /sessions/<id> does not open a new row for every id. Defaults to the path.
        """
        with self._lock:
            metrics = self._active_requests.pop(request_id, None)
            if metrics:
                metrics.end_time = time.perf_counter()
                metrics.duration_ms = (metrics.end_time - metrics.start_time) * 1000
                metrics.status_code = status_code
                metrics.error = error
                key = stats_path or metrics.path

                # Update aggregated stats
                self._request_counts[key] += 1
                self._total_duration_by_path[key] += metrics.duration_ms
                if metrics.duration_ms > self.slow_threshold * 1000:
                    self._slow_count_by_path[key] += 1
                self._min_duration_by_path[key] = min(self._min_duration_by_path.get(key, metrics.duration_ms),
                                                      metrics.duration_ms)
                self._max_duration_by_path[key] = max(self._max_duration_by_path.get(key, metrics.duration_ms),
                                                      metrics.duration_ms)
                
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
                    "min_ms": self._min_duration_by_path.get(path, 0),
                    "max_ms": self._max_duration_by_path.get(path, 0),
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
            self._min_duration_by_path.clear()
            self._max_duration_by_path.clear()
            self._completed_requests.clear()


def get_async_tasks(limit: int) -> dict[str, Any]:
    """The pending asyncio tasks of the running loop: how many, and the first ``limit`` by coroutine, then name.

    One enumeration for both, so the count and the list describe the same moment. all_tasks()
    is a set in no particular order: sorted before it is cut, the same tasks give the same list.
    """
    try:
        tasks = list(asyncio.all_tasks())  # only pending tasks: a done one is no longer listed
    except RuntimeError:  # no running loop
        tasks = []
    listed = sorted(({"name": task.get_name(), "coro": getattr(task.get_coro(), "__qualname__", str(task.get_coro()))}
                     for task in tasks), key=lambda task: (task["coro"], task["name"]))
    return {"total_count": len(tasks), "tasks": listed[:limit]}


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
            
            # Lag is how much longer than expected. A wake-up measured early (the loop's
            # clock is coarser than perf_counter, ~15 ms on Windows) is no lag, but still a
            # sample: skipped, "current" would keep showing an older one.
            lag = max((actual - expected_interval) * 1000, 0.0)
            self._lag_samples.append(lag)
            if len(self._lag_samples) > self._max_samples:
                self._lag_samples = self._lag_samples[-self._max_samples:]
            if lag > 100:
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
                # No len(gc.get_objects()): it walks every live object on the event loop,
                # and the Performance panel asks for this report every few seconds.
                "gc_counts": gc.get_count(),
            }
        except ImportError:
            return {"error": "psutil not installed"}
        except Exception as e:
            return {"error": str(e)}


# Global profiler instances
_request_profiler: Optional[RequestProfiler] = None
_loop_monitor: Optional[EventLoopMonitor] = None


def get_profiler() -> RequestProfiler:
    """Get the global request profiler."""
    global _request_profiler
    if _request_profiler is None:
        _request_profiler = RequestProfiler()
    return _request_profiler


def get_loop_monitor() -> EventLoopMonitor:
    """Get the global event loop monitor."""
    global _loop_monitor
    if _loop_monitor is None:
        _loop_monitor = EventLoopMonitor()
    return _loop_monitor


def _stats_key(scope: dict[str, Any]) -> str:
    """The row a finished request is counted under -- never its raw path, so the rows stay a bounded set.

    Routing leaves its result in the scope the middleware passed down: the matched
    route (its template, below the root path of the app it lives in), or a mount it
    entered (static files: no route, but a root path of its own). Anything else --
    a 404, a slash redirect, a request refused before routing -- shares one row.
    """
    root_path = scope.get("root_path", "")
    route = scope.get("route")
    if route is not None and getattr(route, "path_format", None):
        return f"{root_path}{route.path_format}"
    if "app_root_path" in scope and root_path != scope["app_root_path"]:
        return f"{root_path}/{{path}}"
    return "(no route)"


def add_profiling_middleware(app: "FastAPI") -> None:
    """Add profiling middleware to FastAPI app.
    
    Uses Pure ASGI to avoid BaseHTTPMiddleware overhead.
    Must be called synchronously during app setup, before startup.
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
            path = loggable_path(scope.get("path", ""))  # kept for the report and the slow-request log
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
                metrics = profiler.end_request(request_id, status_code, error, _stats_key(scope))
                if metrics and metrics.duration_ms and metrics.duration_ms > SLOW_REQUEST_THRESHOLD * 1000:
                    profiling_logger.warning(
                        f"Slow request: {method} {path} took {metrics.duration_ms:.1f}ms "
                        f"(threshold: {SLOW_REQUEST_THRESHOLD*1000:.0f}ms)"
                    )
    
    # Wrap the ASGI app - this must be done synchronously before app starts
    app.add_middleware(ProfilingMiddleware)
    logger.info("Profiling middleware added to app")


def get_profiling_report() -> dict[str, Any]:
    """Generate a comprehensive profiling report."""
    profiler = get_profiler()
    loop_monitor = get_loop_monitor()
    
    return {
        "enabled": PROFILING_ENABLED,
        "timestamp": datetime.now().isoformat(),
        "thresholds": {
            "slow_request_ms": SLOW_REQUEST_THRESHOLD * 1000,
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
        "async_tasks": get_async_tasks(limit=20),
        "event_loop": loop_monitor.get_lag_stats(),
        "memory": MemoryMonitor.get_memory_details(),
        "threads": _get_thread_details()
    }


#: Where a waiting thread's innermost Python frame sits: (file name, function name).
#: Only the innermost frame counts -- every executor thread has thread.py:_worker
#: somewhere below it, the busy ones too. A wait inside C (time.sleep, a socket
#: read) leaves the caller as innermost frame: such a thread shows as active.
_IDLE_FRAMES = frozenset({
    ("threading.py", "wait"),          # Event.wait(), Condition.wait(), queue.Queue.get()
    ("selectors.py", "select"),        # an event loop waiting for I/O
    ("windows_events.py", "_poll"),    # the same on the Windows proactor loop
    ("thread.py", "_worker"),          # ThreadPoolExecutor worker waiting on its SimpleQueue
})


def _get_thread_details() -> dict[str, Any]:
    """Every thread with whether it waits (idle) or runs Python code (active)."""
    import sys

    frames = sys._current_frames()
    threads_info = []
    for thread in threading.enumerate():
        frame = frames.get(thread.ident)
        where = (os.path.basename(frame.f_code.co_filename), frame.f_code.co_name) if frame else None
        is_idle = where in _IDLE_FRAMES
        threads_info.append({
            "name": thread.name,
            "ident": thread.ident,
            "daemon": thread.daemon,
            "alive": thread.is_alive(),
            "idle": is_idle,
            "idle_reason": f"{where[0]}:{where[1]}" if is_idle else None,
        })

    # Sort: active first, then by name
    threads_info.sort(key=lambda t: (t["idle"], t["name"]))

    return {
        "count": len(threads_info),
        "active_count": sum(1 for t in threads_info if not t["idle"]),
        "idle_count": sum(1 for t in threads_info if t["idle"]),
        "threads": threads_info
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
    
    loop_monitor = get_loop_monitor()
    await loop_monitor.start()
    
    profiling_logger.info("Performance profiling started")
    logger.info("Performance profiling started (logs in logs/profiling.log)")


async def stop_profiling() -> None:
    """Stop all profiling components."""
    loop_monitor = get_loop_monitor()
    await loop_monitor.stop()
    
    profiling_logger.info("Performance profiling stopped")
    profiling_logger.info("=" * 60)
    logger.info("Performance profiling stopped")
