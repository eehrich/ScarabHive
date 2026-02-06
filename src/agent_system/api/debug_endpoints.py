"""Debug API endpoints for performance profiling.

These endpoints are only available when AGENT_ENABLE_PROFILING=1.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from ..utils.profiling import (
    PROFILING_ENABLED,
    get_profiler,
    get_task_monitor,
    get_loop_monitor,
    get_profiling_report,
    MemoryMonitor,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])

# Template directory
TEMPLATES_DIR = Path(__file__).parent / "templates"


def _load_template(name: str) -> str:
    """Load an HTML template from the templates directory."""
    template_path = TEMPLATES_DIR / name
    if not template_path.exists():
        raise HTTPException(status_code=500, detail=f"Template {name} not found")
    return template_path.read_text(encoding="utf-8")


def _check_profiling_enabled():
    """Raise 404 if profiling is not enabled."""
    if not PROFILING_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Profiling not enabled. Set AGENT_ENABLE_PROFILING=1 to enable."
        )


@router.get("/profile")
async def get_profile() -> dict[str, Any]:
    """Get comprehensive profiling report.
    
    Returns detailed information about:
    - Active and slow requests
    - Async task states
    - Event loop lag
    - Memory usage
    - Thread counts
    """
    _check_profiling_enabled()
    return get_profiling_report()


@router.get("/profile/requests")
async def get_request_stats() -> dict[str, Any]:
    """Get request statistics by endpoint."""
    _check_profiling_enabled()
    profiler = get_profiler()
    return {
        "active": [
            {
                "id": r.request_id,
                "path": r.path,
                "method": r.method,
                "duration_ms": r.duration_ms
            }
            for r in profiler.get_active_requests()
        ],
        "stats_by_path": profiler.get_stats()
    }


@router.get("/profile/tasks")
async def get_async_tasks(include_stack: bool = False) -> dict[str, Any]:
    """Get information about all async tasks.
    
    Useful for identifying:
    - Tasks that are stuck
    - Task names and coroutine info
    - Potential deadlocks
    
    Args:
        include_stack: Include stack traces (expensive, use sparingly)
    """
    _check_profiling_enabled()
    task_monitor = get_task_monitor()
    all_tasks = task_monitor.get_all_tasks_info(include_stack=include_stack)
    
    return {
        "total_count": len(all_tasks),
        "tasks": all_tasks,
        "slow_callbacks": task_monitor.get_slow_callbacks()
    }


@router.get("/profile/loop")
async def get_event_loop_stats() -> dict[str, Any]:
    """Get event loop health statistics.
    
    High lag values indicate blocking operations in the event loop.
    """
    _check_profiling_enabled()
    loop_monitor = get_loop_monitor()
    
    # Also include current loop info
    loop = asyncio.get_event_loop()
    
    return {
        "lag": loop_monitor.get_lag_stats(),
        "loop_info": {
            "running": loop.is_running(),
            "closed": loop.is_closed(),
            "debug": loop.get_debug() if hasattr(loop, 'get_debug') else None
        }
    }


@router.get("/profile/memory")
async def get_memory_stats() -> dict[str, Any]:
    """Get detailed memory statistics."""
    _check_profiling_enabled()
    return {
        "current": MemoryMonitor.get_memory_details(),
        "gc": {
            "counts": gc.get_count(),
            "thresholds": gc.get_threshold(),
            "is_enabled": gc.isenabled()
        }
    }


@router.post("/profile/gc")
async def trigger_gc() -> dict[str, Any]:
    """Trigger garbage collection and return stats."""
    _check_profiling_enabled()
    
    before = MemoryMonitor.get_memory_details()
    collected = gc.collect()
    after = MemoryMonitor.get_memory_details()
    
    return {
        "collected_objects": collected,
        "memory_before_mb": before.get("rss_mb", 0),
        "memory_after_mb": after.get("rss_mb", 0),
        "freed_mb": before.get("rss_mb", 0) - after.get("rss_mb", 0)
    }


@router.post("/profile/reset")
async def reset_profile_stats() -> dict[str, str]:
    """Reset all profiling statistics."""
    _check_profiling_enabled()
    profiler = get_profiler()
    profiler.reset_stats()
    return {"status": "reset", "timestamp": datetime.now().isoformat()}


@router.get("/profile/dashboard", response_class=HTMLResponse)
async def profile_dashboard() -> HTMLResponse:
    """Interactive HTML dashboard for profiling data."""
    _check_profiling_enabled()
    html = _load_template("profiling_dashboard.html")
    return HTMLResponse(
        content=html,
        headers={
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": "frame-ancestors 'self'"
        }
    )


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Basic health check with minimal profiling info.
    
    Available even when profiling is disabled.
    """
    try:
        all_tasks = asyncio.all_tasks()
        task_count = len(all_tasks)
    except RuntimeError:
        task_count = 0
    
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "profiling_enabled": PROFILING_ENABLED,
        "async_tasks": task_count,
        "threads": __import__('threading').active_count(),
        "memory_mb": MemoryMonitor.get_memory_mb()
    }


# =============================================================================
# Memory Profiling Endpoints
# =============================================================================

def _check_memory_profiling_enabled():
    """Raise 404 if memory profiling is not enabled."""
    from ..utils.memory_profiling import MEMORY_PROFILING_ENABLED
    if not MEMORY_PROFILING_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Memory profiling not enabled. Set AGENT_ENABLE_MEMORY_PROFILING=1 to enable."
        )


@router.get("/memory")
async def get_memory_report() -> dict[str, Any]:
    """Get comprehensive memory profiling report.
    
    Returns detailed information about:
    - Current memory usage
    - Object counts by type
    - Tracemalloc allocation tracking
    - Memory trend analysis
    - Potential memory leaks
    - Reference cycles
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_memory_report_async
    return await get_memory_report_async()


@router.post("/memory/snapshot")
async def take_memory_snapshot() -> dict[str, Any]:
    """Take a memory snapshot for comparison.
    
    Snapshots are stored internally and used for:
    - Comparing memory growth over time
    - Identifying consistently growing object types
    - Detecting memory leaks
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import take_memory_snapshot_async
    snapshot = await take_memory_snapshot_async()
    return {
        "status": "snapshot_taken",
        "timestamp": snapshot.timestamp.isoformat(),
        "total_mb": snapshot.total_mb,
        "object_count": sum(snapshot.object_counts.values())
    }


@router.get("/memory/diff")
async def get_memory_diff() -> dict[str, Any]:
    """Get difference between last two memory snapshots.
    
    Shows:
    - Memory growth in MB
    - Object count changes by type
    - Top growing object types (potential leaks)
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_leak_detector
    
    detector = get_leak_detector()
    diff = detector.get_latest_diff()
    
    if diff is None:
        return {"status": "insufficient_snapshots", "message": "Need at least 2 snapshots"}
    
    return diff.to_dict()


@router.get("/memory/trend")
async def get_memory_trend() -> dict[str, Any]:
    """Analyze memory trend over all snapshots.
    
    Identifies:
    - Overall memory growth rate
    - Consistently growing object types (likely leaks)
    - Memory growth per hour
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_leak_detector
    
    detector = get_leak_detector()
    return detector.get_trend()


@router.get("/memory/objects")
async def get_object_growth() -> dict[str, Any]:
    """Get object count changes from baseline.
    
    Shows which object types have grown since profiling started.
    Useful for identifying accumulating objects.
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_object_growth_async
    
    growth = await get_object_growth_async()
    # Sort by absolute growth
    sorted_growth = sorted(growth.items(), key=lambda x: abs(x[1]), reverse=True)
    
    return {
        "total_types_changed": len(growth),
        "growth": dict(sorted_growth[:50])  # Top 50
    }


@router.post("/memory/baseline")
async def set_memory_baseline() -> dict[str, Any]:
    """Set current object counts as baseline.
    
    Future calls to /memory/objects will show changes from this point.
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_leak_detector
    
    detector = get_leak_detector()
    await detector._object_tracker.set_baseline_async()
    
    return {
        "status": "baseline_set",
        "timestamp": datetime.now().isoformat()
    }


@router.get("/memory/tracemalloc")
async def get_tracemalloc_stats() -> dict[str, Any]:
    """Get tracemalloc allocation statistics.
    
    Shows top memory allocations with file:line information.
    Requires AGENT_ENABLE_MEMORY_PROFILING=1.
    
    Note: tracemalloc is not started by default (causes memory overhead).
    Use POST /debug/memory/tracemalloc/start to enable it temporarily.
    """
    _check_memory_profiling_enabled()
    import tracemalloc
    
    if not tracemalloc.is_tracing():
        return {
            "status": "not_tracing", 
            "message": "tracemalloc not started. Use POST /debug/memory/tracemalloc/start to enable."
        }
    
    snapshot = tracemalloc.take_snapshot()
    # Filter out tracemalloc's own allocations
    snapshot = snapshot.filter_traces([
        tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
        tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
    ])
    
    # Get stats by line
    top_by_line = []
    for stat in snapshot.statistics('lineno')[:30]:
        top_by_line.append({
            "file": str(stat.traceback),
            "size_kb": stat.size / 1024,
            "count": stat.count
        })
    
    # Get stats by file
    top_by_file = []
    for stat in snapshot.statistics('filename')[:20]:
        top_by_file.append({
            "file": str(stat.traceback),
            "size_kb": stat.size / 1024,
            "count": stat.count
        })
    
    current, peak = tracemalloc.get_traced_memory()
    
    return {
        "status": "tracing",
        "current_mb": current / (1024 * 1024),
        "peak_mb": peak / (1024 * 1024),
        "top_by_line": top_by_line,
        "top_by_file": top_by_file
    }


@router.post("/memory/tracemalloc/start")
async def start_tracemalloc(nframes: int = 10) -> dict[str, Any]:
    """Start tracemalloc for detailed allocation tracking.
    
    WARNING: tracemalloc causes significant overhead and memory growth
    from FrameSummary accumulation. Use only for short debugging sessions.
    """
    import tracemalloc
    
    if tracemalloc.is_tracing():
        return {
            "status": "already_running",
            "message": "tracemalloc is already active"
        }
    
    tracemalloc.start(nframes)
    return {
        "status": "started",
        "nframes": nframes,
        "message": "tracemalloc started - remember to stop it after debugging"
    }


@router.post("/memory/tracemalloc/stop")
async def stop_tracemalloc() -> dict[str, Any]:
    """Stop tracemalloc and release memory."""
    import tracemalloc
    
    if not tracemalloc.is_tracing():
        return {
            "status": "not_running",
            "message": "tracemalloc is not active"
        }
    
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    return {
        "status": "stopped",
        "final_current_mb": current / (1024 * 1024),
        "final_peak_mb": peak / (1024 * 1024),
        "message": "tracemalloc stopped, memory released"
    }


@router.get("/memory/dashboard", response_class=HTMLResponse)
async def memory_dashboard() -> HTMLResponse:
    """Interactive HTML dashboard for memory profiling."""
    _check_memory_profiling_enabled()
    html = _load_template("memory_dashboard.html")
    return HTMLResponse(
        content=html,
        headers={
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": "frame-ancestors 'self'"
        }
    )