"""Debug API endpoints: the data of the Performance and Memory Profile panels, and /debug/health.

The profiling endpoints exist only while their feature is on (AGENT_ENABLE_PROFILING=1,
AGENT_ENABLE_MEMORY_PROFILING=1) and answer administrators only.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..utils import profiling
from ..utils.profiling import (
    PROFILING_ENABLED,
    get_profiler,
    get_profiling_report,
    MemoryMonitor,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])


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
# Memory Profiling Endpoints -- the data of the Memory Profile panel
# =============================================================================

async def require_admin_viewer(request: Request) -> None:
    """Administrators only, checked here and not only by the route rules in config.yaml: 401 or 403."""
    from ..ui.routes import viewer_role

    if await viewer_role(request) != "admin":
        raise HTTPException(status_code=403, detail="Administrators only")


def _check_memory_profiling_enabled():
    """Raise 404 if memory profiling is not enabled."""
    from ..utils.memory_profiling import MEMORY_PROFILING_ENABLED
    if not MEMORY_PROFILING_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Memory profiling not enabled. Set AGENT_ENABLE_MEMORY_PROFILING=1 to enable."
        )


async def require_memory_profile_access(request: Request) -> None:
    """Who may open the Memory Profile panel and read its data: an administrator, while the feature is on."""
    await require_admin_viewer(request)
    _check_memory_profiling_enabled()


memory_router = APIRouter(prefix="/memory", dependencies=[Depends(require_admin_viewer)])


@memory_router.get("")
async def get_memory_summary() -> dict[str, Any]:
    """Process memory, GC and tracemalloc state, the latest snapshot, baseline growth and trend.

    Read-only and cheap enough for an auto refresh: it reads what snapshots
    stored and never walks the heap.
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import memory_summary
    return await asyncio.to_thread(memory_summary)


@memory_router.post("/snapshot")
async def take_memory_snapshot() -> dict[str, Any]:
    """Count all objects by type (and the top allocations while tracemalloc runs) and store it as a snapshot.

    Walks the whole heap in the profiling thread; the oldest of ten snapshots is dropped.
    """
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_leak_detector
    snapshot = await get_leak_detector().take_snapshot_async(include_allocations=True)
    return {
        "taken_at": snapshot.timestamp.isoformat(),
        "rss_mb": snapshot.total_mb,
        "objects": sum(snapshot.object_counts.values()),
    }


@memory_router.post("/baseline")
async def set_memory_baseline() -> dict[str, Any]:
    """Replace the baseline with the current object counts (walks the heap in the profiling thread)."""
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import get_leak_detector
    tracker = get_leak_detector()._object_tracker
    await tracker.set_baseline_async()
    return {"set_at": tracker.baseline()[1].isoformat()}


@memory_router.post("/tracemalloc/start")
async def start_tracemalloc(nframes: int = Query(10, ge=1, le=100)) -> dict[str, Any]:
    """Start tracemalloc for detailed allocation tracking.

    WARNING: tracemalloc causes significant overhead and memory growth
    from FrameSummary accumulation. Use only for short debugging sessions.
    """
    # Same gate as every other /debug/memory endpoint. Without it the trace
    # could be STARTED (with all its overhead) while nothing can read it --
    # profiling running forever with no consumer.
    # stop_tracemalloc stays ungated on purpose: it is the mitigation
    # path for a trace that outlived the flag (e.g. AGENT_START_TRACEMALLOC).
    _check_memory_profiling_enabled()
    from ..utils.memory_profiling import start_tracing
    return {"status": "started" if start_tracing(nframes) else "already_running", "nframes": nframes}


@memory_router.post("/tracemalloc/stop")
async def stop_tracemalloc() -> dict[str, Any]:
    """Stop tracemalloc and drop its traces."""
    from ..utils.memory_profiling import stop_tracing
    final = stop_tracing()
    if final is None:
        return {"status": "not_running"}
    return {"status": "stopped", "final_current_mb": final["current_mb"], "final_peak_mb": final["peak_mb"]}


router.include_router(memory_router)


# =============================================================================
# Performance Profiling Endpoints -- the data of the Performance panel
# =============================================================================

async def require_performance_access(request: Request) -> None:
    """Who may open the Performance panel and read its data: an administrator, while profiling is on."""
    await require_admin_viewer(request)
    if not profiling.PROFILING_ENABLED:  # read at call time, like the catalogue reads it
        raise HTTPException(status_code=404, detail="Profiling not enabled. Set AGENT_ENABLE_PROFILING=1 to enable.")


profile_router = APIRouter(prefix="/profile", dependencies=[Depends(require_performance_access)])


@profile_router.get("")
async def get_profile() -> dict[str, Any]:
    """Active and slowest recent requests, stats per route, async tasks, event loop lag, memory, threads."""
    return get_profiling_report()


@profile_router.post("/gc")
async def trigger_gc() -> dict[str, Any]:
    """Run a full garbage collection and say what it collected and how the process size changed."""
    before = MemoryMonitor.get_memory_mb()
    collected = gc.collect()
    after = MemoryMonitor.get_memory_mb()
    return {"collected_objects": collected, "memory_before_mb": before, "memory_after_mb": after,
            "freed_mb": before - after}


@profile_router.post("/reset")
async def reset_profile_stats() -> dict[str, str]:
    """Forget the request stats and the slow request history; running requests stay."""
    get_profiler().reset_stats()
    return {"status": "reset", "timestamp": datetime.now().isoformat()}


router.include_router(profile_router)
