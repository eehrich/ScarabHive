"""
Test cross-session deadlock prevention (StatusBus and queue handling).
"""
import asyncio
import pytest
from agent_system.tools.status import (
    StatusBus, StatusEvent, StatusPhase, 
    QueueStatusHandler, FilteredQueueStatusHandler
)


class TestStatusBusLockBehavior:
    """Test that StatusBus doesn't hold lock during handler execution."""

    @pytest.mark.asyncio
    async def test_status_bus_handler_execution_outside_lock(self):
        """Test that handlers are executed outside the lock."""
        bus = StatusBus()
        
        # Track when lock is held
        lock_held_during_handler = False
        
        class TestHandler:
            async def process(self, event):
                # Try to acquire lock - should succeed if not held
                nonlocal lock_held_during_handler
                lock_held_during_handler = bus._lock.locked()
                await asyncio.sleep(0.01)  # Simulate I/O
        
        bus.add_handler(TestHandler())
        
        event = StatusEvent(
            server="test",
            message="test",
            request_id="req_001",
            phase=StatusPhase.PROGRESS
        )
        
        await bus.publish(event)
        
        # Handler should have been executed without lock held
        assert not lock_held_during_handler, "Lock should not be held during handler execution"

    @pytest.mark.asyncio
    async def test_multiple_sessions_publish_concurrently(self):
        """Test that multiple sessions can publish events concurrently."""
        bus = StatusBus()
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        queue3 = asyncio.Queue()
        
        handler1 = QueueStatusHandler(queue1)
        handler2 = QueueStatusHandler(queue2)
        handler3 = QueueStatusHandler(queue3)
        
        bus.add_handler(handler1)
        bus.add_handler(handler2)
        bus.add_handler(handler3)
        
        # Create events from different "sessions"
        events = [
            StatusEvent(server="session_A", message="msg1", request_id="req_A", phase=StatusPhase.PROGRESS),
            StatusEvent(server="session_B", message="msg2", request_id="req_B", phase=StatusPhase.PROGRESS),
            StatusEvent(server="session_C", message="msg3", request_id="req_C", phase=StatusPhase.PROGRESS),
        ]
        
        # Publish all concurrently
        start = asyncio.get_event_loop().time()
        await asyncio.gather(*[bus.publish(event) for event in events])
        elapsed = asyncio.get_event_loop().time() - start
        
        # Should complete quickly (not serialized by lock)
        assert elapsed < 0.5, f"Concurrent publish took {elapsed}s, should be < 0.5s"
        
        # All queues should have received all events
        assert queue1.qsize() == 3
        assert queue2.qsize() == 3
        assert queue3.qsize() == 3

    @pytest.mark.asyncio
    async def test_slow_handler_does_not_block_publishing(self):
        """Test that a slow handler doesn't prevent publish() from being called by other sessions."""
        bus = StatusBus()
        
        # Create a slow handler
        slow_queue = asyncio.Queue()
        fast_queue = asyncio.Queue()
        
        class SlowHandler:
            async def process(self, event):
                await asyncio.sleep(0.5)  # Slow handler
                await slow_queue.put(event)
        
        bus.add_handler(SlowHandler())
        bus.add_handler(QueueStatusHandler(fast_queue))
        
        # Publish first event (will be slow due to slow handler)
        event1 = StatusEvent(server="slow", message="slow", request_id="req_slow", phase=StatusPhase.PROGRESS)
        task1 = asyncio.create_task(bus.publish(event1))
        
        # Give it a moment to start and acquire lock
        await asyncio.sleep(0.1)
        
        # Try to start second publish while first is still processing handlers
        # This should NOT block waiting for the lock (lock is only held briefly for metadata)
        event2 = StatusEvent(server="fast", message="fast", request_id="req_fast", phase=StatusPhase.PROGRESS)
        task2 = asyncio.create_task(bus.publish(event2))
        
        # Wait a bit to see if task2 can acquire lock
        await asyncio.sleep(0.1)
        
        # Both tasks should be running (not blocked on lock acquisition)
        assert not task1.done(), "First task should still be processing slow handler"
        assert not task2.done(), "Second task should be processing its handlers"
        
        # Wait for both to complete
        await asyncio.gather(task1, task2)
        
        # Both queues should have received both events
        assert fast_queue.qsize() == 2, "Fast queue should have both events"


class TestQueueHandlerTimeout:
    """Test that queue handlers don't hang on slow consumers."""

    @pytest.mark.asyncio
    async def test_queue_handler_timeout_on_full_queue(self):
        """Test that queue.put() drops oldest event when full (no blocking)."""
        # Create a bounded queue that will fill up
        queue = asyncio.Queue(maxsize=1)
        handler = QueueStatusHandler(queue)
        
        # Fill the queue
        event1 = StatusEvent(server="test", message="msg1", request_id="req_001", phase=StatusPhase.PROGRESS)
        await queue.put(event1)
        assert queue.full()
        
        # Try to add another event (should drop oldest, not timeout)
        event2 = StatusEvent(server="test", message="msg2", request_id="req_002", phase=StatusPhase.PROGRESS)
        
        start = asyncio.get_event_loop().time()
        await handler.process(event2)  # Should complete immediately (drop-oldest strategy)
        elapsed = asyncio.get_event_loop().time() - start
        
        # Should complete almost instantly (no blocking)
        assert elapsed < 0.1, f"Expected instant completion (drop-oldest), got {elapsed}s"
        
        # Queue should still be full (old event dropped, new event added)
        assert queue.qsize() == 1
        # Verify new event is in queue (oldest was dropped)
        retrieved_event = await queue.get()
        assert retrieved_event.message == "msg2", "Should have new event (oldest dropped)"

    @pytest.mark.asyncio
    async def test_filtered_queue_handler_timeout(self):
        """Test that FilteredQueueStatusHandler also uses drop-oldest strategy."""
        # Create a bounded queue
        queue = asyncio.Queue(maxsize=1)
        handler = FilteredQueueStatusHandler(queue, server_filter="test")
        
        # Fill the queue
        event1 = StatusEvent(server="test", message="msg1", request_id="req_001", phase=StatusPhase.PROGRESS)
        await queue.put(event1)
        assert queue.full()
        
        # Try to add another matching event (should drop oldest)
        event2 = StatusEvent(server="test", message="msg2", request_id="req_002", phase=StatusPhase.PROGRESS)
        
        start = asyncio.get_event_loop().time()
        await handler.process(event2)
        elapsed = asyncio.get_event_loop().time() - start
        
        # Should complete almost instantly (no blocking)
        assert elapsed < 0.1, f"Expected instant completion (drop-oldest), got {elapsed}s"

    @pytest.mark.asyncio
    async def test_queue_handler_succeeds_with_available_queue(self):
        """Test that normal queue operations work quickly."""
        queue = asyncio.Queue()
        handler = QueueStatusHandler(queue)
        
        event = StatusEvent(server="test", message="msg", request_id="req_001", phase=StatusPhase.PROGRESS)
        
        start = asyncio.get_event_loop().time()
        await handler.process(event)
        elapsed = asyncio.get_event_loop().time() - start
        
        # Should complete almost instantly
        assert elapsed < 0.1, f"Normal queue.put took {elapsed}s, should be instant"
        assert queue.qsize() == 1


class TestConcurrentSessionPublishing:
    """Test that multiple sessions can publish concurrently without interference."""

    @pytest.mark.asyncio
    async def test_high_volume_concurrent_publishing(self):
        """Test many sessions publishing simultaneously."""
        bus = StatusBus()
        
        # Create separate queues for 10 "sessions"
        queues = [asyncio.Queue() for _ in range(10)]
        for queue in queues:
            bus.add_handler(QueueStatusHandler(queue))
        
        # Each session publishes 10 events concurrently
        async def publish_session_events(session_id: int):
            events = [
                StatusEvent(
                    server=f"session_{session_id}",
                    message=f"msg_{i}",
                    request_id=f"req_{session_id}_{i}",
                    phase=StatusPhase.PROGRESS
                )
                for i in range(10)
            ]
            await asyncio.gather(*[bus.publish(event) for event in events])
        
        # Run all sessions concurrently
        start = asyncio.get_event_loop().time()
        await asyncio.gather(*[publish_session_events(i) for i in range(10)])
        elapsed = asyncio.get_event_loop().time() - start
        
        # Should complete in reasonable time (not serialized)
        assert elapsed < 2.0, f"High volume publishing took {elapsed}s, should be < 2s"
        
        # Each queue should have received all 100 events (10 sessions * 10 events)
        for queue in queues:
            assert queue.qsize() == 100

    @pytest.mark.asyncio
    async def test_session_isolation_with_filters(self):
        """Test that filtered handlers correctly isolate sessions."""
        bus = StatusBus()
        
        # Create filtered queues for specific sessions
        queue_A = asyncio.Queue()
        queue_B = asyncio.Queue()
        queue_all = asyncio.Queue()
        
        bus.add_handler(FilteredQueueStatusHandler(queue_A, request_id_filter="session_A"))
        bus.add_handler(FilteredQueueStatusHandler(queue_B, request_id_filter="session_B"))
        bus.add_handler(QueueStatusHandler(queue_all))  # No filter
        
        # Publish events from different sessions
        events_A = [
            StatusEvent(server="test", message=f"A_{i}", request_id="session_A", phase=StatusPhase.PROGRESS)
            for i in range(5)
        ]
        events_B = [
            StatusEvent(server="test", message=f"B_{i}", request_id="session_B", phase=StatusPhase.PROGRESS)
            for i in range(5)
        ]
        
        await asyncio.gather(*[bus.publish(e) for e in events_A + events_B])
        
        # Filtered queues should only have their own events
        assert queue_A.qsize() == 5
        assert queue_B.qsize() == 5
        assert queue_all.qsize() == 10
        
        # Verify correct events in each queue
        for _ in range(5):
            event_a = await queue_A.get()
            assert event_a.request_id == "session_A"
            
            event_b = await queue_B.get()
            assert event_b.request_id == "session_B"


class TestEdgeCasesAndRobustness:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_block_other_handlers(self):
        """Test that one failing handler doesn't affect others."""
        bus = StatusBus()
        
        good_queue = asyncio.Queue()
        
        class FailingHandler:
            async def process(self, event):
                raise RuntimeError("Simulated handler failure")
        
        bus.add_handler(FailingHandler())
        bus.add_handler(QueueStatusHandler(good_queue))
        
        event = StatusEvent(server="test", message="msg", request_id="req_001", phase=StatusPhase.PROGRESS)
        
        # Should not raise, despite failing handler
        await bus.publish(event)
        
        # Good handler should still receive the event
        assert good_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_does_not_affect_other_queues(self):
        """Test that unsubscribing one queue doesn't affect others."""
        bus = StatusBus()
        
        queue1 = await bus.subscribe()
        queue2 = await bus.subscribe()
        
        # Unsubscribe queue1
        bus.unsubscribe(queue1)
        
        # Publish event
        event = StatusEvent(server="test", message="msg", request_id="req_001", phase=StatusPhase.PROGRESS)
        await bus.publish(event)
        
        # queue1 should not receive event
        assert queue1.qsize() == 0
        
        # queue2 should receive event
        assert queue2.qsize() == 1

    @pytest.mark.asyncio
    async def test_empty_handler_list_does_not_crash(self):
        """Test that publishing with no handlers doesn't crash."""
        bus = StatusBus()
        
        event = StatusEvent(server="test", message="msg", request_id="req_001", phase=StatusPhase.PROGRESS)
        
        # Should not raise
        await bus.publish(event)
        
        # Metrics should still be tracked
        metrics = bus.get_status_metrics()
        assert metrics["publish_attempted"] == 1
        assert metrics["delivered"] == 0


class TestPerformanceRegression:
    """Test that fixes don't significantly impact performance."""

    @pytest.mark.asyncio
    async def test_publish_latency_acceptable(self):
        """Test that publish latency is still acceptable."""
        bus = StatusBus()
        
        # Add multiple handlers
        for _ in range(5):
            queue = asyncio.Queue()
            bus.add_handler(QueueStatusHandler(queue))
        
        event = StatusEvent(server="test", message="msg", request_id="req_001", phase=StatusPhase.PROGRESS)
        
        # Measure latency
        latencies = []
        for _ in range(100):
            start = asyncio.get_event_loop().time()
            await bus.publish(event)
            elapsed = asyncio.get_event_loop().time() - start
            latencies.append(elapsed)
        
        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)
        
        # Should be very fast (microseconds to milliseconds)
        assert avg_latency < 0.01, f"Average latency {avg_latency}s too high"
        assert max_latency < 0.05, f"Max latency {max_latency}s too high"

    @pytest.mark.asyncio
    async def test_handler_snapshot_overhead_minimal(self):
        """Test that copying handler list doesn't add significant overhead."""
        bus = StatusBus()
        
        # Add many handlers
        for _ in range(50):
            queue = asyncio.Queue()
            bus.add_handler(QueueStatusHandler(queue))
        
        event = StatusEvent(server="test", message="msg", request_id="req_001", phase=StatusPhase.PROGRESS)
        
        # Measure time with many handlers
        start = asyncio.get_event_loop().time()
        await bus.publish(event)
        elapsed = asyncio.get_event_loop().time() - start
        
        # Should still be reasonably fast even with 50 handlers
        assert elapsed < 0.1, f"Publishing with 50 handlers took {elapsed}s"
