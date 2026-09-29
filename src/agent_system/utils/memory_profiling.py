"""Memory profiling utilities for detecting memory leaks.

This module provides tools to diagnose memory issues:
- Object counts by type, stored as snapshots
- Growth between snapshots and against a baseline
- Tracemalloc integration for allocation tracking

Enable via environment variable: AGENT_ENABLE_MEMORY_PROFILING=1

IMPORTANT: The heavy operations (gc.get_objects, tracemalloc.take_snapshot)
run only when a snapshot or a baseline is taken, in a thread pool, never on
the event loop. memory_summary() reads what was stored and never walks the heap.

Usage:
    await start_memory_profiling()                 # baseline, first snapshot, periodic snapshots
    await get_leak_detector().take_snapshot_async()
    summary = memory_summary()
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import logging
import os
import threading
import tracemalloc
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Use dedicated profiling logger for memory profiling output
memory_profiling_logger = logging.getLogger("agent_system.profiling.memory")

# Configuration from environment
MEMORY_PROFILING_ENABLED = os.getenv("AGENT_ENABLE_MEMORY_PROFILING", "0") == "1"
TRACEMALLOC_FRAMES = int(os.getenv("AGENT_TRACEMALLOC_FRAMES", "25"))
#: How many rows of each list the summary carries.
SUMMARY_ROWS = 30

# Thread pool for heavy operations - single thread to avoid GIL contention
_profiling_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Get or create the profiling thread pool."""
    global _profiling_executor
    if _profiling_executor is None:
        _profiling_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="memory_profiler"
        )
    return _profiling_executor


# =============================================================================
# The interpreter and the OS: the only places the heap and the process are read
# =============================================================================

def count_objects() -> dict[str, int]:
    """Count all live objects by type (blocking: collects garbage and walks the whole heap)."""
    gc.collect()
    counts: dict[str, int] = defaultdict(int)
    for obj in gc.get_objects():
        try:
            type_name = type(obj).__qualname__
            module = getattr(type(obj), '__module__', '')
            if module and module not in ('builtins', '__builtin__'):
                type_name = f"{module}.{type_name}"
            counts[type_name] += 1
        except Exception:
            counts["<unknown>"] += 1
    return dict(counts)


def process_memory() -> dict[str, float]:
    """RSS, virtual size and share of physical memory of this process; empty without psutil."""
    try:
        import psutil
    except ImportError:
        return {}
    process = psutil.Process()
    info = process.memory_info()
    return {
        "rss_mb": info.rss / (1024 * 1024),
        "vms_mb": info.vms / (1024 * 1024),
        "percent": process.memory_percent(),
    }


def top_allocations(limit: int = 20) -> list[dict[str, Any]]:
    """The largest allocation sites traced so far (blocking); empty while tracemalloc is off."""
    if not tracemalloc.is_tracing():
        return []
    snapshot = tracemalloc.take_snapshot().filter_traces([
        tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
        tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
        tracemalloc.Filter(False, tracemalloc.__file__),
    ])
    return [
        {"file": str(stat.traceback), "size_kb": stat.size / 1024, "count": stat.count}
        for stat in snapshot.statistics('lineno')[:limit]
    ]


def tracing_state() -> dict[str, Any]:
    """Whether tracemalloc runs, and the memory it has traced (cheap)."""
    if not tracemalloc.is_tracing():
        return {"active": False, "current_mb": None, "peak_mb": None}
    current, peak = tracemalloc.get_traced_memory()
    return {"active": True, "current_mb": current / (1024 * 1024), "peak_mb": peak / (1024 * 1024)}


def start_tracing(nframes: int) -> bool:
    """Start tracemalloc with nframes frames per trace; False if it already runs."""
    if tracemalloc.is_tracing():
        return False
    tracemalloc.start(nframes)
    return True


def stop_tracing() -> Optional[dict[str, Any]]:
    """Stop tracemalloc and drop its traces; returns the last traced memory, None if it was not running."""
    if not tracemalloc.is_tracing():
        return None
    state = tracing_state()
    tracemalloc.stop()
    return state


# =============================================================================
# Snapshots and baseline
# =============================================================================

@dataclass
class MemorySnapshot:
    """Point-in-time memory snapshot."""
    timestamp: datetime
    total_mb: float
    object_counts: dict[str, int]  # type -> count
    top_allocations: list[dict[str, Any]]  # tracemalloc top stats
    gc_stats: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "total_mb": self.total_mb,
            "object_counts": self.object_counts,
            "top_allocations": self.top_allocations,
            "gc_stats": self.gc_stats
        }


@dataclass
class MemoryDiff:
    """Difference between two memory snapshots."""
    time_delta_seconds: float
    memory_delta_mb: float
    object_count_changes: dict[str, int]  # type -> change
    new_types: list[str]
    removed_types: list[str]
    top_growth: list[tuple[str, int]]  # (type, growth_count)
    allocation_diff: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_delta_seconds": self.time_delta_seconds,
            "memory_delta_mb": self.memory_delta_mb,
            "object_count_changes": self.object_count_changes,
            "new_types": self.new_types,
            "removed_types": self.removed_types,
            "top_growth": self.top_growth,
            "allocation_diff": self.allocation_diff
        }


class ObjectTracker:
    """Holds the object counts growth is measured against."""

    def __init__(self):
        self._baseline: dict[str, int] = {}
        self._baseline_at: Optional[datetime] = None
        self._lock = threading.Lock()

    def set_baseline(self) -> None:
        """Set current object counts as baseline (blocking)."""
        counts = count_objects()
        with self._lock:
            self._baseline, self._baseline_at = counts, datetime.now().astimezone()
        memory_profiling_logger.info(f"Memory baseline set: {sum(counts.values())} objects")

    async def set_baseline_async(self) -> None:
        """Set current object counts as baseline (in the profiling thread)."""
        await asyncio.get_running_loop().run_in_executor(_get_executor(), self.set_baseline)

    def baseline(self) -> tuple[dict[str, int], Optional[datetime]]:
        """The baseline counts and when they were taken; ({}, None) before the first."""
        with self._lock:
            return self._baseline, self._baseline_at


class MemoryLeakDetector:
    """Detects potential memory leaks by tracking growth patterns."""

    def __init__(self, min_growth_threshold: int = 100):
        self.min_growth_threshold = min_growth_threshold
        self._snapshots: list[MemorySnapshot] = []
        self._max_snapshots = 10
        # The newest snapshot that recorded allocations, apart from the ring:
        # only a snapshot asked for records them, and ten periodic ones after
        # it (under an hour at the default interval) pushed it out of the ring
        # -- the panel then said none were recorded.
        self._allocations: Optional[MemorySnapshot] = None
        self._lock = threading.Lock()
        self._object_tracker = ObjectTracker()

    def take_snapshot(self, include_allocations: bool = False) -> MemorySnapshot:
        """Take a memory snapshot (blocking - use the async version on the event loop).

        Args:
            include_allocations: also record the largest allocation sites while tracemalloc runs.
                Their statistics take seconds to a minute on a large trace, so only a snapshot
                asked for records them, never the periodic one.
        """
        object_counts = count_objects()
        allocations = top_allocations() if include_allocations else []
        mem_snapshot = MemorySnapshot(
            timestamp=datetime.now().astimezone(),
            total_mb=process_memory().get("rss_mb", 0.0),
            object_counts=object_counts,
            top_allocations=allocations,
            gc_stats={
                "counts": gc.get_count(),
                "threshold": gc.get_threshold(),
                "objects": sum(object_counts.values()),
                "garbage": len(gc.garbage)
            }
        )

        with self._lock:
            self._snapshots.append(mem_snapshot)
            if len(self._snapshots) > self._max_snapshots:
                self._snapshots = self._snapshots[-self._max_snapshots:]
            if mem_snapshot.top_allocations:
                self._allocations = mem_snapshot

        return mem_snapshot

    async def take_snapshot_async(self, include_allocations: bool = False) -> MemorySnapshot:
        """Take a memory snapshot (in the profiling thread)."""
        return await asyncio.get_running_loop().run_in_executor(
            _get_executor(), partial(self.take_snapshot, include_allocations=include_allocations))

    def snapshots(self) -> list[MemorySnapshot]:
        """The stored snapshots, oldest first."""
        with self._lock:
            return list(self._snapshots)

    def allocations_snapshot(self) -> Optional[MemorySnapshot]:
        """The newest snapshot that recorded allocations, however many
        periodic ones came after it; None if none did."""
        with self._lock:
            return self._allocations

    def compare_snapshots(
        self,
        old: MemorySnapshot,
        new: MemorySnapshot
    ) -> MemoryDiff:
        """Compare two snapshots to detect growth."""
        time_delta = (new.timestamp - old.timestamp).total_seconds()
        memory_delta = new.total_mb - old.total_mb

        # Object count changes
        changes = {}
        all_types = set(new.object_counts.keys()) | set(old.object_counts.keys())
        new_types = []
        removed_types = []

        for type_name in all_types:
            new_count = new.object_counts.get(type_name, 0)
            old_count = old.object_counts.get(type_name, 0)
            delta = new_count - old_count

            if delta != 0:
                changes[type_name] = delta

            if old_count == 0 and new_count > 0:
                new_types.append(type_name)
            elif new_count == 0 and old_count > 0:
                removed_types.append(type_name)

        # Top growth (sorted by absolute growth)
        top_growth = sorted(
            [(t, c) for t, c in changes.items() if c > self.min_growth_threshold],
            key=lambda x: x[1],
            reverse=True
        )[:20]

        # Tracemalloc diff
        allocation_diff = []
        if tracemalloc.is_tracing() and old.top_allocations and new.top_allocations:
            # Simple comparison - show new top allocations
            allocation_diff = new.top_allocations[:10]

        return MemoryDiff(
            time_delta_seconds=time_delta,
            memory_delta_mb=memory_delta,
            object_count_changes=changes,
            new_types=new_types[:20],
            removed_types=removed_types[:20],
            top_growth=top_growth,
            allocation_diff=allocation_diff
        )

    def get_latest_diff(self) -> Optional[MemoryDiff]:
        """Get diff between most recent snapshots."""
        with self._lock:
            if len(self._snapshots) < 2:
                return None
            return self.compare_snapshots(self._snapshots[-2], self._snapshots[-1])

    def get_trend(self) -> dict[str, Any]:
        """Analyze memory trend over all snapshots."""
        with self._lock:
            if len(self._snapshots) < 2:
                return {"status": "insufficient_data", "snapshots": len(self._snapshots)}

            # Memory trend
            memory_values = [s.total_mb for s in self._snapshots]
            memory_growth = memory_values[-1] - memory_values[0]
            time_span = (self._snapshots[-1].timestamp - self._snapshots[0].timestamp).total_seconds()

            # Consistent growers (objects that grew in every snapshot)
            consistent_growers = {}
            for i in range(1, len(self._snapshots)):
                prev = self._snapshots[i-1]
                curr = self._snapshots[i]

                for type_name in curr.object_counts:
                    delta = curr.object_counts.get(type_name, 0) - prev.object_counts.get(type_name, 0)
                    if delta > 0:
                        if type_name not in consistent_growers:
                            consistent_growers[type_name] = {"count": 0, "total_growth": 0}
                        consistent_growers[type_name]["count"] += 1
                        consistent_growers[type_name]["total_growth"] += delta

            # Filter to types that grew consistently
            snapshot_count = len(self._snapshots) - 1
            likely_leaks: list[dict[str, Any]] = [
                {"type": t, **info}
                for t, info in consistent_growers.items()
                if info["count"] >= snapshot_count * 0.7  # Grew in 70%+ of intervals
                and info["total_growth"] >= self.min_growth_threshold
            ]
            likely_leaks.sort(key=lambda x: int(x["total_growth"]), reverse=True)

            return {
                "status": "analyzed",
                "snapshots": len(self._snapshots),
                "time_span_seconds": time_span,
                "memory_mb": {
                    "start": memory_values[0],
                    "end": memory_values[-1],
                    "growth": memory_growth,
                    "growth_rate_mb_per_hour": (memory_growth / time_span * 3600) if time_span > 0 else 0
                },
                "likely_leaks": likely_leaks[:20]
            }


# Global instances
_leak_detector: Optional[MemoryLeakDetector] = None
_snapshot_task: Optional[Any] = None


def get_leak_detector() -> MemoryLeakDetector:
    """Get the global leak detector."""
    global _leak_detector
    if _leak_detector is None:
        _leak_detector = MemoryLeakDetector()
    return _leak_detector


async def start_memory_profiling() -> None:
    """Start memory profiling."""
    global _snapshot_task

    if not MEMORY_PROFILING_ENABLED:
        logger.info("Memory profiling disabled (set AGENT_ENABLE_MEMORY_PROFILING=1 to enable)")
        return

    # memory_profiling_logger inherits from profiling_logger setup
    memory_profiling_logger.info("Starting memory profiling...")

    # NOTE: tracemalloc is NOT started by default because it costs memory
    # itself (FrameSummary objects accumulate). The Memory Profile panel starts
    # and stops it on demand; AGENT_START_TRACEMALLOC=1 starts it with the server.
    if os.getenv("AGENT_START_TRACEMALLOC", "0") == "1":
        if start_tracing(TRACEMALLOC_FRAMES):
            memory_profiling_logger.info(f"Tracemalloc started with {TRACEMALLOC_FRAMES} frames")
    else:
        memory_profiling_logger.info("Tracemalloc disabled (set AGENT_START_TRACEMALLOC=1 to enable)")

    # Set baseline (async to avoid blocking)
    detector = get_leak_detector()
    await detector._object_tracker.set_baseline_async()

    # Take initial snapshot (async to avoid blocking)
    await detector.take_snapshot_async()

    # Start periodic snapshot task with longer default interval
    async def periodic_snapshots():
        # Default to 5 minutes to reduce overhead
        interval = int(os.getenv("AGENT_MEMORY_SNAPSHOT_INTERVAL", "300"))
        memory_profiling_logger.info(f"Memory snapshot interval: {interval}s")
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    # Use async version to avoid blocking event loop
                    await detector.take_snapshot_async()
                    diff = detector.get_latest_diff()
                    if diff and diff.memory_delta_mb > 50:  # 50MB growth
                        memory_profiling_logger.warning(
                            f"Significant memory growth: {diff.memory_delta_mb:.1f}MB "
                            f"in {diff.time_delta_seconds:.0f}s"
                        )
                        if diff.top_growth:
                            memory_profiling_logger.warning(f"Top growers: {diff.top_growth[:5]}")
                    else:
                        memory_profiling_logger.debug(
                            f"Memory snapshot: delta={diff.memory_delta_mb:.1f}MB" if diff else "No diff"
                        )
                except asyncio.CancelledError:
                    raise  # Re-raise to exit the loop
                except Exception as e:
                    memory_profiling_logger.error(f"Error taking memory snapshot: {e}")
        except asyncio.CancelledError:
            memory_profiling_logger.debug("Memory profiling task cancelled (shutdown)")
            # Don't re-raise - graceful exit

    _snapshot_task = asyncio.create_task(periodic_snapshots())
    memory_profiling_logger.info("Memory profiling started")
    logger.info("Memory profiling started (logs in logs/profiling.log)")


async def stop_memory_profiling() -> None:
    """Stop memory profiling gracefully."""
    global _snapshot_task, _profiling_executor

    if _snapshot_task:
        _snapshot_task.cancel()
        try:
            await _snapshot_task
        except asyncio.CancelledError:
            pass  # Expected during shutdown
        except Exception:
            pass
        _snapshot_task = None

    stop_tracing()

    # Shutdown executor
    if _profiling_executor:
        _profiling_executor.shutdown(wait=False)
        _profiling_executor = None

    memory_profiling_logger.info("Memory profiling stopped")
    logger.info("Memory profiling stopped")


def _top(counts: dict[str, int], key) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=key, reverse=True)[:SUMMARY_ROWS]


def memory_summary() -> dict[str, Any]:
    """What the Memory Profile panel shows, read from what is stored.

    Read-only and without a heap walk: no gc.collect, no gc.get_objects, no
    tracemalloc snapshot, nothing stored or cleared. Object counts are those of
    the latest snapshot; growth against the baseline is measured on that
    snapshot, and only if it was taken after the baseline. Allocations come
    from the newest snapshot that recorded them, with its time: the periodic
    snapshots taken since record none.
    """
    detector = get_leak_detector()
    snapshots = detector.snapshots()
    latest = snapshots[-1] if snapshots else None
    baseline, baseline_at = detector._object_tracker.baseline()

    snapshot = None
    if latest is not None:
        snapshot = {
            "taken_at": latest.timestamp.isoformat(),
            "rss_mb": latest.total_mb,
            "objects": sum(latest.object_counts.values()),
            "top_objects": [{"type": t, "count": c} for t, c in _top(latest.object_counts, lambda item: item[1])],
        }

    recorded = detector.allocations_snapshot()
    allocations = None
    if recorded is not None:
        allocations = {"recorded_at": recorded.timestamp.isoformat(), "sites": recorded.top_allocations[:SUMMARY_ROWS]}

    baseline_view = None
    if baseline_at is not None:
        changes = None
        if latest is not None and latest.timestamp >= baseline_at:
            delta = {t: latest.object_counts.get(t, 0) - baseline.get(t, 0)
                     for t in latest.object_counts.keys() | baseline.keys()}
            changes = [{"type": t, "change": c}
                       for t, c in _top({t: c for t, c in delta.items() if c}, lambda item: abs(item[1]))]
        baseline_view = {"set_at": baseline_at.isoformat(), "changes": changes}

    return {
        "memory": process_memory(),
        "gc": {"counts": list(gc.get_count()), "thresholds": list(gc.get_threshold()), "garbage": len(gc.garbage)},
        "tracemalloc": tracing_state(),
        "snapshot": snapshot,
        "allocations": allocations,
        "baseline": baseline_view,
        "trend": detector.get_trend(),
    }
