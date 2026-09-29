"""Tests for memory profiling utilities."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_system.utils import memory_profiling
from agent_system.utils.memory_profiling import (
    MemoryDiff,
    MemoryLeakDetector,
    MemorySnapshot,
    count_objects,
    memory_summary,
)


class TestCountObjects:

    def test_counts_live_objects_by_type(self):
        counts = count_objects()

        assert counts.get("function", 0) > 0
        assert counts.get("dict", 0) > 0


class TestMemoryLeakDetector:
    """Tests for MemoryLeakDetector class."""

    def test_take_snapshot(self):
        detector = MemoryLeakDetector()
        snapshot = detector.take_snapshot()

        assert isinstance(snapshot, MemorySnapshot)
        assert snapshot.total_mb >= 0
        assert len(snapshot.object_counts) > 0
        assert snapshot.timestamp is not None

    def test_compare_snapshots(self):
        detector = MemoryLeakDetector()
        s1 = detector.take_snapshot()
        s2 = detector.take_snapshot()

        diff = detector.compare_snapshots(s1, s2)

        assert isinstance(diff, MemoryDiff)
        assert diff.time_delta_seconds >= 0

    def test_get_latest_diff_no_snapshots(self):
        assert MemoryLeakDetector().get_latest_diff() is None

    def test_get_latest_diff_with_snapshots(self):
        detector = MemoryLeakDetector()
        detector.take_snapshot()
        detector.take_snapshot()

        assert isinstance(detector.get_latest_diff(), MemoryDiff)

    def test_get_trend_insufficient_data(self):
        assert MemoryLeakDetector().get_trend()["status"] == "insufficient_data"

    def test_get_trend_with_data(self):
        detector = MemoryLeakDetector()
        for _ in range(3):
            detector.take_snapshot()

        trend = detector.get_trend()

        assert trend["status"] == "analyzed"
        assert "memory_mb" in trend
        assert "likely_leaks" in trend

    def test_only_a_snapshot_asked_for_records_allocations_and_they_outlast_the_periodic_ones(self, heap, monkeypatch):
        """Their statistics cost seconds to a minute on a large trace: the periodic task must not pay that."""
        sites = [{"file": "x.py:1", "size_kb": 1.0, "count": 1}]
        monkeypatch.setattr(memory_profiling, "tracemalloc", SimpleNamespace(
            is_tracing=lambda: True, stop=lambda: None, get_traced_memory=lambda: (0, 0),
            take_snapshot=lambda: SimpleNamespace(filter_traces=lambda filters: SimpleNamespace(
                statistics=lambda key: [SimpleNamespace(traceback="x.py:1", size=1024, count=1)])),
            Filter=lambda *args: None, __file__="tracemalloc.py"))
        monkeypatch.setattr(memory_profiling, "MEMORY_PROFILING_ENABLED", True)
        monkeypatch.setenv("AGENT_MEMORY_SNAPSHOT_INTERVAL", "0")
        detector = memory_profiling.get_leak_detector()

        async def asked_then_periodic():
            asked = await detector.take_snapshot_async(include_allocations=True)
            await memory_profiling.start_memory_profiling()  # its first snapshot and the periodic ones
            try:
                while len(detector.snapshots()) < 4:
                    await asyncio.sleep(0.01)
            finally:
                await memory_profiling.stop_memory_profiling()
            return asked

        asked = asyncio.run(asked_then_periodic())
        summary = memory_summary()

        assert asked.top_allocations == sites
        assert [s.top_allocations for s in detector.snapshots()[1:]] == [[]] * (len(detector.snapshots()) - 1)
        assert summary["allocations"] == {"recorded_at": asked.timestamp.isoformat(), "sites": sites}
        assert summary["snapshot"]["taken_at"] != asked.timestamp.isoformat()

    def test_allocations_asked_for_outlast_a_full_ring_of_periodic_snapshots(self, heap, monkeypatch):
        """The detector keeps ten snapshots; ten periodic ones after the one
        asked for pushed it out, and the panel said none were recorded."""
        sites = [{"file": "x.py:1", "size_kb": 1.0, "count": 1}]
        monkeypatch.setattr(memory_profiling, "top_allocations", lambda: sites)
        detector = memory_profiling.get_leak_detector()

        asked = detector.take_snapshot(include_allocations=True)
        for _ in range(12):
            detector.take_snapshot()

        assert asked not in detector.snapshots(), "fixture: the ring still holds the snapshot asked for"
        assert memory_summary()["allocations"] == {"recorded_at": asked.timestamp.isoformat(), "sites": sites}

    def test_no_snapshot_with_allocations_means_none(self, heap):
        memory_profiling.get_leak_detector().take_snapshot(include_allocations=True)  # tracemalloc is off here

        assert memory_summary()["allocations"] is None


class TestSnapshotSerialization:

    def test_snapshot_to_dict(self):
        data = MemoryLeakDetector().take_snapshot().to_dict()

        assert {"timestamp", "total_mb", "object_counts"} <= set(data)

    def test_diff_to_dict(self):
        detector = MemoryLeakDetector()
        data = detector.compare_snapshots(detector.take_snapshot(), detector.take_snapshot()).to_dict()

        assert {"time_delta_seconds", "memory_delta_mb", "object_count_changes"} <= set(data)


@pytest.fixture
def heap(monkeypatch):
    """A heap the profiler counts, a clock one second per reading, and a fresh global detector."""
    counts = {"dict": 100, "list": 50, "gone": 5, "tuple": 7}
    start = datetime(2026, 9, 15, 10, 0)  # naive, as datetime.now() reads
    readings = iter(start + timedelta(seconds=i) for i in range(100000))
    monkeypatch.setattr(memory_profiling, "count_objects", lambda: dict(counts))
    monkeypatch.setattr(memory_profiling, "process_memory", lambda: {"rss_mb": 10.0, "vms_mb": 20.0, "percent": 1.0})
    monkeypatch.setattr(memory_profiling, "datetime", SimpleNamespace(now=lambda: next(readings)))
    monkeypatch.setattr(memory_profiling, "_leak_detector", MemoryLeakDetector())
    return counts


class TestMemorySummary:

    def test_reading_the_summary_walks_no_heap_and_changes_nothing(self, heap, monkeypatch):
        detector = memory_profiling.get_leak_detector()
        detector._object_tracker.set_baseline()
        detector.take_snapshot()
        snapshots, baseline = detector.snapshots(), detector._object_tracker.baseline()

        def forbidden(*args, **kwargs):
            raise AssertionError("the summary walked the heap or collected")

        # the real gc and tracemalloc, minus everything that collects, walks, snapshots or reconfigures
        monkeypatch.setattr(memory_profiling, "count_objects", forbidden)
        monkeypatch.setattr(memory_profiling, "top_allocations", forbidden)
        monkeypatch.setattr(memory_profiling, "gc", SimpleNamespace(
            get_count=lambda: (1, 2, 3), get_threshold=lambda: (700, 10, 10), garbage=["kept"],
            collect=forbidden, get_objects=forbidden, set_debug=forbidden))
        monkeypatch.setattr(memory_profiling, "tracemalloc", SimpleNamespace(
            is_tracing=lambda: True, get_traced_memory=lambda: (1024 * 1024, 2 * 1024 * 1024),
            take_snapshot=forbidden, start=forbidden, stop=forbidden))

        summary = memory_summary()

        assert summary["gc"] == {"counts": [1, 2, 3], "thresholds": [700, 10, 10], "garbage": 1}
        assert summary["tracemalloc"] == {"active": True, "current_mb": 1.0, "peak_mb": 2.0}
        assert detector.snapshots() == snapshots
        assert detector._object_tracker.baseline() == baseline

    def test_growth_is_measured_on_the_latest_snapshot_taken_after_the_baseline(self, heap):
        detector = memory_profiling.get_leak_detector()
        detector.take_snapshot()
        detector._object_tracker.set_baseline()

        before = memory_summary()
        heap["dict"] += 30
        heap["list"] -= 40
        del heap["gone"]
        detector.take_snapshot()
        after = memory_summary()

        assert before["baseline"]["changes"] is None  # the only snapshot is older than the baseline
        assert after["baseline"]["changes"] == [
            {"type": "list", "change": -40}, {"type": "dict", "change": 30}, {"type": "gone", "change": -5}]
        assert datetime.fromisoformat(after["baseline"]["set_at"]).utcoffset() is not None
        assert after["snapshot"]["objects"] == 147
        assert after["snapshot"]["top_objects"] == [{"type": "dict", "count": 130}, {"type": "list", "count": 10}, {"type": "tuple", "count": 7}]

    def test_without_baseline_or_snapshot_the_summary_says_so(self, heap):
        summary = memory_summary()

        assert summary["baseline"] is None
        assert summary["snapshot"] is None
        assert summary["trend"]["status"] == "insufficient_data"


class TestTracing:

    def test_start_and_stop_report_what_they_did(self, monkeypatch):
        state = {"on": False}
        monkeypatch.setattr(memory_profiling, "tracemalloc", SimpleNamespace(
            is_tracing=lambda: state["on"], start=lambda n: state.update(on=True, n=n),
            stop=lambda: state.update(on=False), get_traced_memory=lambda: (0, 1024 * 1024)))

        assert memory_profiling.stop_tracing() is None
        assert memory_profiling.start_tracing(7) is True and state["n"] == 7
        assert memory_profiling.start_tracing(9) is False and state["n"] == 7
        assert memory_profiling.stop_tracing() == {"active": True, "current_mb": 0.0, "peak_mb": 1.0}
        assert state["on"] is False
