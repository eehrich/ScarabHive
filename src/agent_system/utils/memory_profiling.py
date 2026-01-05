"""Memory profiling utilities for detecting memory leaks.

This module provides tools to diagnose memory issues:
- Object tracking by type
- Growth detection between snapshots
- Reference cycle detection
- Top memory consumers
- Tracemalloc integration for allocation tracking

Enable via environment variable: AGENT_ENABLE_MEMORY_PROFILING=1

IMPORTANT: All heavy operations (gc.get_objects, tracemalloc.take_snapshot)
are run in a thread pool to avoid blocking the async event loop.

Usage:
    # Start tracking
    await start_memory_profiling()
    
    # Take snapshots periodically
    snapshot1 = take_memory_snapshot()
    # ... run code ...
    snapshot2 = take_memory_snapshot()
    
    # Compare to find leaks
    diff = compare_snapshots(snapshot1, snapshot2)
    
    # Get report
    report = await get_memory_report_async()
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import logging
import os
import threading
import tracemalloc
import weakref
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Use dedicated profiling logger for memory profiling output
memory_profiling_logger = logging.getLogger("agent_system.profiling.memory")

# Configuration from environment
MEMORY_PROFILING_ENABLED = os.getenv("AGENT_ENABLE_MEMORY_PROFILING", "0") == "1"
TRACEMALLOC_FRAMES = int(os.getenv("AGENT_TRACEMALLOC_FRAMES", "25"))

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
    """Tracks object counts by type."""
    
    def __init__(self):
        self._baseline: dict[str, int] = {}
        self._lock = threading.Lock()
    
    def _get_object_counts_sync(self) -> dict[str, int]:
        """Count all objects by type (synchronous, blocking)."""
        gc.collect()  # Ensure garbage is collected first
        
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
    
    def get_object_counts(self) -> dict[str, int]:
        """Count all objects by type (blocking - use async version when possible)."""
        return self._get_object_counts_sync()
    
    async def get_object_counts_async(self) -> dict[str, int]:
        """Count all objects by type (non-blocking)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _get_executor(),
            self._get_object_counts_sync
        )
    
    def set_baseline(self) -> None:
        """Set current object counts as baseline (blocking)."""
        with self._lock:
            self._baseline = self._get_object_counts_sync()
        memory_profiling_logger.info(f"Memory baseline set: {sum(self._baseline.values())} objects")
    
    async def set_baseline_async(self) -> None:
        """Set current object counts as baseline (non-blocking)."""
        counts = await self.get_object_counts_async()
        with self._lock:
            self._baseline = counts
        memory_profiling_logger.info(f"Memory baseline set: {sum(self._baseline.values())} objects")
    
    def get_changes_from_baseline(self) -> dict[str, int]:
        """Get object count changes from baseline (blocking)."""
        with self._lock:
            if not self._baseline:
                return {}
            
            current = self._get_object_counts_sync()
            changes = {}
            
            all_types = set(current.keys()) | set(self._baseline.keys())
            for type_name in all_types:
                curr_count = current.get(type_name, 0)
                base_count = self._baseline.get(type_name, 0)
                if curr_count != base_count:
                    changes[type_name] = curr_count - base_count
            
            return changes
    
    async def get_changes_from_baseline_async(self) -> dict[str, int]:
        """Get object count changes from baseline (non-blocking)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_get_executor(), self.get_changes_from_baseline)


class MemoryLeakDetector:
    """Detects potential memory leaks by tracking growth patterns."""
    
    def __init__(self, min_growth_threshold: int = 100):
        self.min_growth_threshold = min_growth_threshold
        self._snapshots: list[MemorySnapshot] = []
        self._max_snapshots = 10
        self._lock = threading.Lock()
        self._object_tracker = ObjectTracker()
        self._include_tracemalloc = False  # Only include tracemalloc on demand
    
    def _take_snapshot_sync(self, include_tracemalloc: bool = False) -> MemorySnapshot:
        """Take a memory snapshot (synchronous, blocking).
        
        Args:
            include_tracemalloc: If True, include tracemalloc stats (expensive!)
        """
        gc.collect()
        
        # Get object counts
        object_counts = self._object_tracker._get_object_counts_sync()
        
        # Get tracemalloc stats only if explicitly requested (expensive!)
        top_allocations: list[dict[str, Any]] = []
        if include_tracemalloc and tracemalloc.is_tracing():
            tm_snapshot = tracemalloc.take_snapshot()
            # Filter out tracemalloc's own allocations
            tm_snapshot = tm_snapshot.filter_traces([
                tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
                tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
                tracemalloc.Filter(False, tracemalloc.__file__),
            ])
            top_stats = tm_snapshot.statistics('lineno')[:20]
            for stat in top_stats:
                top_allocations.append({
                    "file": str(stat.traceback),
                    "size_kb": stat.size / 1024,
                    "count": stat.count
                })
        
        # Get memory info
        total_mb = self._get_memory_mb()
        
        # GC stats - avoid calling gc.get_objects() again
        gc_stats = {
            "counts": gc.get_count(),
            "threshold": gc.get_threshold(),
            "objects": sum(object_counts.values()),  # Use already collected data
            "garbage": len(gc.garbage)
        }
        
        mem_snapshot = MemorySnapshot(
            timestamp=datetime.now(),
            total_mb=total_mb,
            object_counts=object_counts,
            top_allocations=top_allocations,
            gc_stats=gc_stats
        )
        
        with self._lock:
            self._snapshots.append(mem_snapshot)
            if len(self._snapshots) > self._max_snapshots:
                self._snapshots = self._snapshots[-self._max_snapshots:]
        
        return mem_snapshot
    
    def take_snapshot(self, include_tracemalloc: bool = False) -> MemorySnapshot:
        """Take a memory snapshot (blocking - use async version when possible)."""
        return self._take_snapshot_sync(include_tracemalloc=include_tracemalloc)
    
    async def take_snapshot_async(self, include_tracemalloc: bool = False) -> MemorySnapshot:
        """Take a memory snapshot (non-blocking)."""
        loop = asyncio.get_event_loop()
        from functools import partial
        return await loop.run_in_executor(
            _get_executor(), 
            partial(self._take_snapshot_sync, include_tracemalloc=include_tracemalloc)
        )
    
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
    
    def _get_memory_mb(self) -> float:
        """Get current process memory in MB."""
        try:
            import psutil
            return psutil.Process().memory_info().rss / (1024 * 1024)
        except ImportError:
            return 0.0


class ReferenceTracker:
    """Tracks references to specific objects to find what's holding them."""
    
    def __init__(self):
        self._tracked: dict[int, weakref.ref] = {}
        self._names: dict[int, str] = {}
    
    def track(self, obj: Any, name: str = "") -> None:
        """Start tracking an object."""
        obj_id = id(obj)
        try:
            self._tracked[obj_id] = weakref.ref(obj)
            self._names[obj_id] = name or f"object_{obj_id}"
            memory_profiling_logger.debug(f"Tracking object: {self._names[obj_id]}")
        except TypeError:
            # Can't create weakref for this type
            memory_profiling_logger.warning(f"Cannot track {type(obj).__name__} - weakref not supported")
    
    def get_referrers(self, obj: Any, depth: int = 2) -> list[dict[str, Any]]:
        """Get objects that reference this object."""
        referrers = gc.get_referrers(obj)
        
        result = []
        for ref in referrers[:20]:  # Limit to prevent huge output
            ref_type = type(ref).__name__
            ref_info: dict[str, Any] = {
                "type": ref_type,
                "id": id(ref)
            }
            
            # Try to get more info
            if isinstance(ref, dict):
                # Find the key(s) that reference our object
                keys = [k for k, v in ref.items() if v is obj]
                ref_info["dict_keys"] = str(keys[:5])
            elif isinstance(ref, (list, tuple)):
                ref_info["length"] = len(ref)
            elif hasattr(ref, '__dict__'):
                ref_info["attrs"] = list(ref.__dict__.keys())[:5]
            
            result.append(ref_info)
        
        return result
    
    def check_alive(self) -> dict[str, bool]:
        """Check which tracked objects are still alive."""
        result = {}
        for obj_id, ref in list(self._tracked.items()):
            name = self._names.get(obj_id, str(obj_id))
            obj = ref()
            result[name] = obj is not None
            if obj is None:
                # Clean up dead references
                del self._tracked[obj_id]
                del self._names[obj_id]
        return result


def find_reference_cycles() -> list[dict[str, Any]]:
    """Find reference cycles that may cause memory leaks."""
    gc.collect()
    
    # Enable cycle detection
    gc.set_debug(gc.DEBUG_SAVEALL)
    gc.collect()
    cycles = gc.garbage[:]
    gc.set_debug(0)
    gc.garbage.clear()
    
    # Group cycles by type
    cycle_info: list[dict[str, Any]] = []
    for obj in cycles[:50]:  # Limit output
        try:
            cycle_info.append({
                "type": type(obj).__name__,
                "id": id(obj),
                "repr": repr(obj)[:100]
            })
        except Exception:
            cycle_info.append({"type": "unknown", "id": id(obj)})
    
    return cycle_info


# Global instances
_leak_detector: Optional[MemoryLeakDetector] = None
_reference_tracker: Optional[ReferenceTracker] = None
_snapshot_task: Optional[Any] = None


def get_leak_detector() -> MemoryLeakDetector:
    """Get the global leak detector."""
    global _leak_detector
    if _leak_detector is None:
        _leak_detector = MemoryLeakDetector()
    return _leak_detector


def get_reference_tracker() -> ReferenceTracker:
    """Get the global reference tracker."""
    global _reference_tracker
    if _reference_tracker is None:
        _reference_tracker = ReferenceTracker()
    return _reference_tracker


async def start_memory_profiling() -> None:
    """Start memory profiling."""
    global _snapshot_task
    
    if not MEMORY_PROFILING_ENABLED:
        logger.info("Memory profiling disabled (set AGENT_ENABLE_MEMORY_PROFILING=1 to enable)")
        return
    
    # memory_profiling_logger inherits from profiling_logger setup
    memory_profiling_logger.info("Starting memory profiling...")
    
    # NOTE: tracemalloc is NOT started by default anymore because it causes
    # memory leaks itself (FrameSummary objects accumulate). Use the
    # /debug/memory/tracemalloc endpoint to get tracemalloc data on-demand.
    # If you need tracemalloc, set AGENT_START_TRACEMALLOC=1
    if os.getenv("AGENT_START_TRACEMALLOC", "0") == "1":
        if not tracemalloc.is_tracing():
            tracemalloc.start(TRACEMALLOC_FRAMES)
            memory_profiling_logger.info(f"Tracemalloc started with {TRACEMALLOC_FRAMES} frames")
    else:
        memory_profiling_logger.info("Tracemalloc disabled (set AGENT_START_TRACEMALLOC=1 to enable)")
    
    # Set baseline (async to avoid blocking)
    detector = get_leak_detector()
    await detector._object_tracker.set_baseline_async()
    
    # Take initial snapshot (async to avoid blocking) - no tracemalloc
    await detector.take_snapshot_async(include_tracemalloc=False)
    
    # Start periodic snapshot task with longer default interval
    async def periodic_snapshots():
        # Default to 5 minutes to reduce overhead
        interval = int(os.getenv("AGENT_MEMORY_SNAPSHOT_INTERVAL", "300"))
        memory_profiling_logger.info(f"Memory snapshot interval: {interval}s")
        while True:
            await asyncio.sleep(interval)
            try:
                # Use async version to avoid blocking event loop
                # Don't include tracemalloc in periodic snapshots (too expensive)
                await detector.take_snapshot_async(include_tracemalloc=False)
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
            except Exception as e:
                memory_profiling_logger.error(f"Error taking memory snapshot: {e}")
    
    _snapshot_task = asyncio.create_task(periodic_snapshots())
    memory_profiling_logger.info("Memory profiling started")
    logger.info("Memory profiling started (logs in logs/profiling.log)")


async def stop_memory_profiling() -> None:
    """Stop memory profiling."""
    global _snapshot_task, _profiling_executor
    
    if _snapshot_task:
        _snapshot_task.cancel()
        try:
            await _snapshot_task
        except Exception:
            pass
        _snapshot_task = None
    
    if tracemalloc.is_tracing():
        tracemalloc.stop()
    
    # Shutdown executor
    if _profiling_executor:
        _profiling_executor.shutdown(wait=False)
        _profiling_executor = None
    
    memory_profiling_logger.info("Memory profiling stopped")
    logger.info("Memory profiling stopped")


def take_memory_snapshot() -> MemorySnapshot:
    """Take a memory snapshot (blocking convenience function)."""
    return get_leak_detector().take_snapshot()


async def take_memory_snapshot_async() -> MemorySnapshot:
    """Take a memory snapshot (non-blocking)."""
    return await get_leak_detector().take_snapshot_async()


def _get_memory_report_sync() -> dict[str, Any]:
    """Generate memory report (synchronous, blocking)."""
    detector = get_leak_detector()
    tracker = get_reference_tracker()
    
    # Force GC
    gc.collect()
    
    # Get current memory
    try:
        import psutil
        process = psutil.Process()
        mem_info = process.memory_info()
        memory_current = {
            "rss_mb": mem_info.rss / (1024 * 1024),
            "vms_mb": mem_info.vms / (1024 * 1024),
            "percent": process.memory_percent()
        }
    except ImportError:
        memory_current = {"error": "psutil not installed"}
    
    # Get tracemalloc top
    tracemalloc_top = []
    if tracemalloc.is_tracing():
        snapshot = tracemalloc.take_snapshot()
        top = snapshot.statistics('lineno')[:20]
        for stat in top:
            tracemalloc_top.append({
                "location": str(stat.traceback),
                "size_kb": stat.size / 1024,
                "count": stat.count
            })
    
    # Get trend analysis
    trend = detector.get_trend()
    
    # Get object counts (top 30 by count) - use already collected counts if available
    with detector._lock:
        if detector._snapshots:
            object_counts = detector._snapshots[-1].object_counts
        else:
            object_counts = detector._object_tracker._get_object_counts_sync()
    top_objects = sorted(object_counts.items(), key=lambda x: x[1], reverse=True)[:30]
    
    # Check for reference cycles
    cycles = find_reference_cycles()
    
    # Get changes from baseline (if baseline was set)
    baseline_changes = detector._object_tracker.get_changes_from_baseline()
    top_changes = sorted(
        [(t, c) for t, c in baseline_changes.items() if c != 0],
        key=lambda x: abs(x[1]),
        reverse=True
    )[:30]
    
    return {
        "enabled": MEMORY_PROFILING_ENABLED,
        "timestamp": datetime.now().isoformat(),
        "tracemalloc_active": tracemalloc.is_tracing(),
        "memory": memory_current,
        "gc": {
            "counts": gc.get_count(),
            "threshold": gc.get_threshold(),
            "total_objects": sum(object_counts.values()),
            "garbage_count": len(gc.garbage)
        },
        "top_objects": [{"type": t, "count": c} for t, c in top_objects],
        "baseline_changes": [{"type": t, "change": c} for t, c in top_changes],
        "has_baseline": bool(detector._object_tracker._baseline),
        "tracemalloc_top": tracemalloc_top,
        "trend_analysis": trend,
        "reference_cycles": cycles[:10],
        "tracked_objects": tracker.check_alive()
    }


def get_memory_report() -> dict[str, Any]:
    """Generate comprehensive memory report (blocking)."""
    return _get_memory_report_sync()


async def get_memory_report_async() -> dict[str, Any]:
    """Generate comprehensive memory report (non-blocking)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_get_executor(), _get_memory_report_sync)


def get_object_growth() -> dict[str, int]:
    """Get object count changes from baseline (blocking)."""
    return get_leak_detector()._object_tracker.get_changes_from_baseline()


async def get_object_growth_async() -> dict[str, int]:
    """Get object count changes from baseline (non-blocking)."""
    return await get_leak_detector()._object_tracker.get_changes_from_baseline_async()
