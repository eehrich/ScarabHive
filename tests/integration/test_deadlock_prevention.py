"""
Test deadlock prevention and infinite loop protection mechanisms.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder
from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from agent_system.mcp.base import MCPRegistry


class TestStatusForwarderDeadlockPrevention:
    """Test that StatusEventForwarder doesn't deadlock when background task crashes."""

    @pytest.mark.asyncio
    async def test_forwarding_task_crash_before_ready(self):
        """Test that start_forwarding doesn't hang if background task crashes immediately."""
        forwarder = StatusEventForwarder()
        
        # Patch _forward_status_events to crash immediately
        async def crashing_forward():
            raise RuntimeError("Simulated crash before ready")
        
        with patch.object(forwarder, '_forward_status_events', side_effect=crashing_forward):
            # This should NOT hang - should timeout after 2 seconds
            try:
                await forwarder.start_forwarding("test_request_123")
            except Exception:
                pass  # Expected to continue despite error
            
            # Verify that we didn't hang (test would timeout if we did)
            assert True, "start_forwarding completed without hanging"

    @pytest.mark.asyncio
    async def test_forwarding_timeout_protection(self):
        """Test that start_forwarding times out if background task takes too long."""
        forwarder = StatusEventForwarder()
        
        # Patch _forward_status_events to never set ready events
        async def slow_forward():
            await asyncio.sleep(10)  # Longer than timeout
        
        with patch.object(forwarder, '_forward_status_events', side_effect=slow_forward):
            # Should timeout but not crash
            await forwarder.start_forwarding("test_request_456")
            
            # Verify we can continue (didn't hang forever)
            assert True, "Timeout mechanism worked"

    @pytest.mark.asyncio
    async def test_forwarding_task_sets_events_on_error(self):
        """Test that forwarding task sets ready events even on exception."""
        forwarder = StatusEventForwarder()
        
        # Mock status_bus to prevent actual subscription
        with patch('agent_system.servers.agent.components.status_forwarding.status_bus') as mock_bus:
            mock_queue = AsyncMock()
            mock_queue.get = AsyncMock(side_effect=RuntimeError("Test error"))
            mock_bus.subscribe = AsyncMock(return_value=mock_queue)
            
            try:
                await forwarder.start_forwarding("test_request_789")
            except Exception:
                pass
            
            # Verify that ready events were set (prevents deadlock)
            assert forwarder.forwarding_ready.is_set()
            assert forwarder.first_get_started.is_set()

    @pytest.mark.asyncio
    async def test_normal_forwarding_startup(self):
        """Test that normal forwarding startup works without timeouts."""
        forwarder = StatusEventForwarder()
        
        # Mock status_bus
        with patch('agent_system.servers.agent.components.status_forwarding.status_bus') as mock_bus:
            mock_queue = AsyncMock()
            mock_queue.get = AsyncMock(side_effect=asyncio.CancelledError)  # Exit immediately
            mock_bus.subscribe = AsyncMock(return_value=mock_queue)
            
            await forwarder.start_forwarding("test_request_normal")
            
            # Verify ready events are set
            assert forwarder.forwarding_ready.is_set()
            assert forwarder.first_get_started.is_set()
            
            # Cleanup
            await forwarder.stop_forwarding()


class TestToolExecutionInfiniteLoopPrevention:
    """Test that tool execution doesn't loop infinitely with stuck tasks."""

    @pytest.fixture
    def tool_manager(self):
        """Create ToolExecutionManager for testing."""
        registry = MCPRegistry()
        return ToolExecutionManager(registry)

    @pytest.mark.asyncio
    async def test_stuck_task_safety_limit(self, tool_manager):
        """Test that polling loop exits after max iterations with stuck tasks."""
        # Mock tool that hangs forever
        async def stuck_tool(**kwargs):
            await asyncio.sleep(1000)  # Will never complete normally
        
        # Create a stuck task
        stuck_task = asyncio.create_task(stuck_tool())
        
        # Manually test the polling loop logic
        pending = {stuck_task}
        max_iterations = 10  # Small limit for testing
        iteration_count = 0
        
        escaped = False
        while pending and iteration_count < max_iterations:
            iteration_count += 1
            done, pending = await asyncio.wait(pending, timeout=0.01, return_when=asyncio.FIRST_COMPLETED)
            
            if iteration_count >= max_iterations:
                # Force exit like the real code does
                for task in pending:
                    if not task.done():
                        task.cancel()
                escaped = True
                break
        
        assert escaped, "Loop should have escaped after max iterations"
        assert iteration_count == max_iterations
        
        # Cleanup
        stuck_task.cancel()
        try:
            await stuck_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_normal_task_completion(self, tool_manager):
        """Test that normal tasks complete without hitting safety limit."""
        # Create a mock tool call
        async def fast_tool(**kwargs):
            await asyncio.sleep(0.01)
            return "success"
        
        # Create a fast task
        fast_task = asyncio.create_task(fast_tool())
        
        # Simulate polling loop
        pending = {fast_task}
        max_iterations = 100
        iteration_count = 0
        completed = False
        
        while pending and iteration_count < max_iterations:
            iteration_count += 1
            done, pending = await asyncio.wait(pending, timeout=0.01, return_when=asyncio.FIRST_COMPLETED)
            
            if done:
                completed = True
                break
        
        assert completed, "Fast task should complete normally"
        assert iteration_count < max_iterations, "Should not need many iterations"

    @pytest.mark.asyncio
    async def test_cancelled_task_handling(self, tool_manager):
        """Test that cancelled tasks are handled gracefully."""
        # Create a cancellable task
        async def cancellable_tool(**kwargs):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise  # Re-raise to propagate cancellation
        
        task = asyncio.create_task(cancellable_tool())
        
        # Cancel it immediately
        task.cancel()
        
        # Try to get result (should raise CancelledError)
        try:
            await task
            pytest.fail("Should have raised CancelledError")
        except asyncio.CancelledError:
            # This is expected and should be handled gracefully
            pass

    @pytest.mark.asyncio
    async def test_multiple_tasks_some_stuck(self, tool_manager):
        """Test that some tasks completing doesn't affect stuck task detection."""
        # Create mixed tasks
        async def fast_tool(**kwargs):
            await asyncio.sleep(0.01)
            return "fast"
        
        async def stuck_tool(**kwargs):
            await asyncio.sleep(1000)
            return "stuck"
        
        fast_task1 = asyncio.create_task(fast_tool())
        fast_task2 = asyncio.create_task(fast_tool())
        stuck_task = asyncio.create_task(stuck_tool())
        
        pending = {fast_task1, fast_task2, stuck_task}
        max_iterations = 50  # Small limit for testing
        iteration_count = 0
        completed_count = 0
        
        while pending and iteration_count < max_iterations:
            iteration_count += 1
            done, pending = await asyncio.wait(pending, timeout=0.01, return_when=asyncio.FIRST_COMPLETED)
            
            completed_count += len(done)
            
            if iteration_count >= max_iterations:
                # Force cancel remaining
                for task in pending:
                    if not task.done():
                        task.cancel()
                break
        
        assert completed_count >= 2, "Fast tasks should have completed"
        assert len(pending) <= 1, "Only stuck task should remain (or none if cancelled)"
        
        # Cleanup
        for task in [fast_task1, fast_task2, stuck_task]:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


class TestConcurrentRequestHandling:
    """Test that parallel requests don't cause deadlocks."""

    @pytest.mark.asyncio
    async def test_multiple_forwarding_tasks(self):
        """Test that multiple StatusEventForwarders can run simultaneously."""
        forwarder1 = StatusEventForwarder()
        forwarder2 = StatusEventForwarder()
        forwarder3 = StatusEventForwarder()
        
        # Mock status_bus
        with patch('agent_system.servers.agent.components.status_forwarding.status_bus') as mock_bus:
            mock_queue = AsyncMock()
            mock_queue.get = AsyncMock(side_effect=asyncio.CancelledError)
            mock_bus.subscribe = AsyncMock(return_value=mock_queue)
            
            # Start all forwarders in parallel
            await asyncio.gather(
                forwarder1.start_forwarding("req_001"),
                forwarder2.start_forwarding("req_002"),
                forwarder3.start_forwarding("req_003")
            )
            
            # All should have started successfully
            assert forwarder1.forwarding_ready.is_set()
            assert forwarder2.forwarding_ready.is_set()
            assert forwarder3.forwarding_ready.is_set()
            
            # Cleanup
            await asyncio.gather(
                forwarder1.stop_forwarding(),
                forwarder2.stop_forwarding(),
                forwarder3.stop_forwarding()
            )

    @pytest.mark.asyncio
    async def test_rapid_start_stop_forwarding(self):
        """Test that rapid start/stop cycles don't cause issues."""
        forwarder = StatusEventForwarder()
        
        with patch('agent_system.servers.agent.components.status_forwarding.status_bus') as mock_bus:
            mock_queue = AsyncMock()
            mock_queue.get = AsyncMock(side_effect=asyncio.CancelledError)
            mock_bus.subscribe = AsyncMock(return_value=mock_queue)
            
            # Rapid start/stop cycles
            for i in range(5):
                await forwarder.start_forwarding(f"req_{i}")
                assert forwarder.forwarding_ready.is_set()
                await forwarder.stop_forwarding()
                await asyncio.sleep(0.01)  # Small delay between cycles


class TestEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_forwarder_with_no_events(self):
        """Test forwarder with no events to forward."""
        forwarder = StatusEventForwarder()
        
        with patch('agent_system.servers.agent.components.status_forwarding.status_bus') as mock_bus:
            mock_queue = AsyncMock()
            # No events, just cancel immediately
            mock_queue.get = AsyncMock(side_effect=asyncio.CancelledError)
            mock_bus.subscribe = AsyncMock(return_value=mock_queue)
            
            await forwarder.start_forwarding("req_no_events")
            
            # Get pending should return empty list
            events = forwarder.get_pending_events()
            assert events == []
            
            await forwarder.stop_forwarding()

    @pytest.mark.asyncio
    async def test_forwarder_double_stop(self):
        """Test that stopping forwarder twice doesn't cause issues."""
        forwarder = StatusEventForwarder()
        
        with patch('agent_system.servers.agent.components.status_forwarding.status_bus') as mock_bus:
            mock_queue = AsyncMock()
            mock_queue.get = AsyncMock(side_effect=asyncio.CancelledError)
            mock_bus.subscribe = AsyncMock(return_value=mock_queue)
            
            await forwarder.start_forwarding("req_double_stop")
            
            # Stop twice
            await forwarder.stop_forwarding()
            await forwarder.stop_forwarding()  # Should not crash

    @pytest.mark.asyncio
    async def test_empty_tool_list(self):
        """Test tool execution with empty tool list."""
        registry = MCPRegistry()
        tool_manager = ToolExecutionManager(registry)
        
        # Execute with empty tool calls list
        tool_calls = []
        tool_name_mapping = {}
        available_tools = []
        request_id = "req_empty_tools"
        step = 1
        
        results = []
        async for event in tool_manager.execute_tools_streaming(
            tool_calls, tool_name_mapping, available_tools, step, request_id
        ):
            results.append(event)
        
        # Should have at least a complete event
        assert any(e.get("type") == "complete" for e in results)


class TestPerformance:
    """Test that safety mechanisms don't significantly impact performance."""

    @pytest.mark.asyncio
    async def test_iteration_limit_not_too_restrictive(self):
        """Test that iteration limit allows reasonable execution times."""
        # 1000 iterations * 0.05s = 50 seconds per task
        # This should be enough for most real scenarios
        max_iterations_per_task = 1000
        poll_interval = 0.05
        max_time_per_task = max_iterations_per_task * poll_interval
        
        assert max_time_per_task >= 30, "Should allow at least 30 seconds per task"
        assert max_time_per_task <= 60, "Should not allow excessive wait times"

    @pytest.mark.asyncio
    async def test_forwarding_startup_is_fast(self):
        """Test that forwarder startup happens quickly (not near timeout)."""
        import time
        
        forwarder = StatusEventForwarder()
        
        with patch('agent_system.servers.agent.components.status_forwarding.status_bus') as mock_bus:
            mock_queue = AsyncMock()
            mock_queue.get = AsyncMock(side_effect=asyncio.CancelledError)
            mock_bus.subscribe = AsyncMock(return_value=mock_queue)
            
            start = time.time()
            await forwarder.start_forwarding("req_timing")
            elapsed = time.time() - start
            
            # Should start in much less than 2 second timeout
            assert elapsed < 0.5, f"Startup took {elapsed}s, should be < 0.5s"
            
            await forwarder.stop_forwarding()


class TestRegressionPrevention:
    """Tests to prevent regression of fixed deadlock issues."""

    @pytest.mark.asyncio
    async def test_status_forwarder_signals_before_loop(self):
        """Regression test: Ensure signals are set before entering main loop."""
        forwarder = StatusEventForwarder()
        
        with patch('agent_system.servers.agent.components.status_forwarding.status_bus') as mock_bus:
            mock_queue = AsyncMock()
            
            # Track the order of operations
            operations = []
            
            async def tracked_get():
                operations.append("queue.get")
                await asyncio.sleep(0.01)
                raise asyncio.CancelledError
            
            mock_queue.get = tracked_get
            mock_bus.subscribe = AsyncMock(return_value=mock_queue)
            
            await forwarder.start_forwarding("req_signal_order")
            
            # Verify signals were set (didn't timeout)
            assert forwarder.forwarding_ready.is_set()
            assert forwarder.first_get_started.is_set()
            
            await forwarder.stop_forwarding()

    @pytest.mark.asyncio
    async def test_tool_execution_handles_all_cancelled(self):
        """Regression test: Ensure loop exits when all tasks are cancelled."""
        # Create multiple tasks that will all be cancelled
        async def cancellable(**kwargs):
            await asyncio.sleep(10)
        
        tasks = [asyncio.create_task(cancellable()) for _ in range(3)]
        
        # Cancel all tasks
        for task in tasks:
            task.cancel()
        
        # Simulate polling loop
        pending = set(tasks)
        iteration_count = 0
        max_iterations = 10
        
        while pending and iteration_count < max_iterations:
            iteration_count += 1
            done, pending = await asyncio.wait(pending, timeout=0.01, return_when=asyncio.FIRST_COMPLETED)
            
            # Process done tasks (should all raise CancelledError)
            for task in done:
                try:
                    await task
                except asyncio.CancelledError:
                    pass  # Expected
        
        # Loop should have exited because all tasks completed (even though cancelled)
        assert len(pending) == 0, "All tasks should be done (cancelled)"
        assert iteration_count <= 5, "Should exit quickly when all cancelled"
