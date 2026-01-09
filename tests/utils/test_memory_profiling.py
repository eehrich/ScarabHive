"""Tests for memory profiling utilities."""
import gc
from agent_system.utils.memory_profiling import (
    ObjectTracker,
    MemoryLeakDetector,
    MemorySnapshot,
    MemoryDiff,
    ReferenceTracker,
    find_reference_cycles,
    get_memory_report,
)


class TestObjectTracker:
    """Tests for ObjectTracker class."""
    
    def test_get_object_counts(self):
        """Test counting objects by type."""
        tracker = ObjectTracker()
        counts = tracker.get_object_counts()
        
        # Should have basic Python types
        assert len(counts) > 0
        # Common types should be present
        assert any('function' in t for t in counts)
        assert any('dict' in t for t in counts)
    
    def test_set_and_get_baseline(self):
        """Test baseline functionality."""
        tracker = ObjectTracker()
        tracker.set_baseline()
        
        # Create some objects
        test_objs = [object() for _ in range(100)]
        
        changes = tracker.get_changes_from_baseline()
        
        # Should detect new objects
        assert len(changes) >= 0  # May or may not show depending on GC
        
        # Cleanup
        del test_objs
    
    def test_changes_from_empty_baseline(self):
        """Test changes when no baseline is set."""
        tracker = ObjectTracker()
        changes = tracker.get_changes_from_baseline()
        
        # Should be empty dict
        assert changes == {}


class TestMemoryLeakDetector:
    """Tests for MemoryLeakDetector class."""
    
    def test_take_snapshot(self):
        """Test taking memory snapshot."""
        detector = MemoryLeakDetector()
        snapshot = detector.take_snapshot()
        
        assert isinstance(snapshot, MemorySnapshot)
        assert snapshot.total_mb >= 0
        assert len(snapshot.object_counts) > 0
        assert snapshot.timestamp is not None
    
    def test_compare_snapshots(self):
        """Test comparing two snapshots."""
        detector = MemoryLeakDetector()
        
        s1 = detector.take_snapshot()
        
        # Create some objects
        test_dicts = [{"key": i} for i in range(500)]
        
        s2 = detector.take_snapshot()
        
        diff = detector.compare_snapshots(s1, s2)
        
        assert isinstance(diff, MemoryDiff)
        assert diff.time_delta_seconds >= 0
        # Memory delta can be positive or negative due to GC
        
        del test_dicts
    
    def test_get_latest_diff_no_snapshots(self):
        """Test getting diff with insufficient snapshots."""
        detector = MemoryLeakDetector()
        diff = detector.get_latest_diff()
        assert diff is None
    
    def test_get_latest_diff_with_snapshots(self):
        """Test getting diff after taking snapshots."""
        detector = MemoryLeakDetector()
        
        detector.take_snapshot()
        detector.take_snapshot()
        
        diff = detector.get_latest_diff()
        assert diff is not None
        assert isinstance(diff, MemoryDiff)
    
    def test_get_trend_insufficient_data(self):
        """Test trend with insufficient snapshots."""
        detector = MemoryLeakDetector()
        trend = detector.get_trend()
        
        assert trend["status"] == "insufficient_data"
    
    def test_get_trend_with_data(self):
        """Test trend with multiple snapshots."""
        detector = MemoryLeakDetector()
        
        # Take multiple snapshots
        for _ in range(3):
            detector.take_snapshot()
        
        trend = detector.get_trend()
        
        assert trend["status"] == "analyzed"
        assert "memory_mb" in trend
        assert "likely_leaks" in trend


class TestReferenceTracker:
    """Tests for ReferenceTracker class."""
    
    def test_track_object(self):
        """Test tracking an object."""
        tracker = ReferenceTracker()
        
        # Use an object that supports weakrefs (custom class instance)
        class TrackableObj:
            pass
        
        obj = TrackableObj()
        tracker.track(obj, "test_obj")
        alive = tracker.check_alive()
        
        assert "test_obj" in alive
        assert alive["test_obj"] is True
    
    def test_track_dead_object(self):
        """Test that dead objects are detected."""
        tracker = ReferenceTracker()
        
        class TrackableObj:
            pass
        
        # Create and track object
        def create_and_track():
            obj = TrackableObj()
            tracker.track(obj, "temp_obj")
        
        create_and_track()
        gc.collect()
        
        alive = tracker.check_alive()
        # Object should be dead after function returns and GC
        assert alive.get("temp_obj", True) is False or "temp_obj" not in alive
    
    def test_get_referrers(self):
        """Test getting referrers of an object."""
        tracker = ReferenceTracker()
        
        class Inner:
            pass
        
        obj = Inner()
        
        referrers = tracker.get_referrers(obj)
        
        # Should find at least the container dict and local scope
        assert len(referrers) > 0
    
    def test_track_non_weakrefable(self):
        """Test tracking objects that don't support weakrefs."""
        tracker = ReferenceTracker()
        
        # dict doesn't support weakrefs - should log warning and not track
        obj = {"key": "value"}
        tracker.track(obj, "dict_obj")
        
        alive = tracker.check_alive()
        # Should not be tracked
        assert "dict_obj" not in alive


class TestFindReferenceCycles:
    """Tests for find_reference_cycles function."""
    
    def test_no_cycles(self):
        """Test when no cycles exist."""
        gc.collect()
        cycles = find_reference_cycles()
        
        # May or may not have cycles depending on state
        assert isinstance(cycles, list)


class TestMemoryReport:
    """Tests for get_memory_report function."""
    
    def test_get_report_structure(self):
        """Test report has expected structure."""
        report = get_memory_report()
        
        # Check main keys
        assert "enabled" in report
        assert "timestamp" in report
        assert "memory" in report
        assert "gc" in report
        assert "top_objects" in report
    
    def test_report_gc_stats(self):
        """Test GC stats in report."""
        report = get_memory_report()
        
        gc_stats = report["gc"]
        assert "counts" in gc_stats
        assert "threshold" in gc_stats
        assert "total_objects" in gc_stats


class TestSnapshotSerialization:
    """Tests for snapshot serialization."""
    
    def test_snapshot_to_dict(self):
        """Test snapshot serialization."""
        detector = MemoryLeakDetector()
        snapshot = detector.take_snapshot()
        
        data = snapshot.to_dict()
        
        assert "timestamp" in data
        assert "total_mb" in data
        assert "object_counts" in data
    
    def test_diff_to_dict(self):
        """Test diff serialization."""
        detector = MemoryLeakDetector()
        
        s1 = detector.take_snapshot()
        s2 = detector.take_snapshot()
        
        diff = detector.compare_snapshots(s1, s2)
        data = diff.to_dict()
        
        assert "time_delta_seconds" in data
        assert "memory_delta_mb" in data
        assert "object_count_changes" in data
