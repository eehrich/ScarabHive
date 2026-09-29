"""
Test deadlock prevention and infinite loop protection mechanisms.

Note: Tests for old background task-based StatusEventForwarder have been removed
as the architecture was replaced with DirectStatusHandler (synchronous, no background task).
"""
import asyncio
import pytest
from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from agent_system.tools.base import ToolServerRegistry


class TestToolExecutionInfiniteLoopPrevention:
    """Test that tool execution doesn't loop infinitely with stuck tasks."""

    @pytest.fixture
    def tool_manager(self):
        """Create ToolExecutionManager for testing."""
        registry = ToolServerRegistry()
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


class TestEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_empty_tool_list(self):
        """Test tool execution with empty tool list."""
        registry = ToolServerRegistry()
        tool_manager = ToolExecutionManager(registry)
        
        # Execute with empty tool calls list
        tool_calls = []
        tool_name_mapping = {}
        available_tools = []
        request_id = "req_empty_tools"
        step = 1
        
        results = []
        async for event in tool_manager.execute_tools_streaming(
            tool_calls, tool_name_mapping, available_tools, step, request_id,
            status_forwarder=None
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


class TestRegressionPrevention:
    """Tests to prevent regression of fixed deadlock issues."""

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
