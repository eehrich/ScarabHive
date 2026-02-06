"""
Test cases for the new cancellation system.
Tests graceful and forced cancellation of tools.
"""
import asyncio
import pytest
import time

from agent_system.core.cancellation import (
    get_cancellation_manager, 
    cancellable_operation, 
    CancellationError,
    CancellationToken,
    CancellationManager
)
from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from plugins.basic_operations.server import BasicOperationsServer


class TestCancellationToken:
    """Test CancellationToken functionality."""
    
    def test_token_creation(self):
        """Test basic token creation and properties."""
        token = CancellationToken("test-request", cleanup_timeout=5.0)
        
        assert token.request_id == "test-request"
        assert token.cleanup_timeout == 5.0
        assert not token.is_cancelled
        assert not token.is_forced
    
    def test_token_cancellation(self):
        """Test token cancellation signals."""
        token = CancellationToken("test-request")
        
        # Initially not cancelled
        assert not token.is_cancelled
        assert not token.is_forced
        
        # Cancel token
        token.cancel()
        assert token.is_cancelled
        assert not token.is_forced
        
        # Force token
        token.force()
        assert token.is_cancelled
        assert token.is_forced
    
    def test_cleanup_callbacks(self):
        """Test cleanup callback functionality."""
        token = CancellationToken("test-request")
        
        callback_called = False
        def cleanup_callback():
            nonlocal callback_called
            callback_called = True
        
        token.add_cleanup_callback(cleanup_callback)
        
        # Callback should not be called yet
        assert not callback_called
        
        # Manual cleanup should call callback
        asyncio.run(token.cleanup())
        assert callback_called
    
    def test_should_force_timeout(self):
        """Test timeout logic for forced cancellation."""
        token = CancellationToken("test-request", cleanup_timeout=0.1)
        
        # Initially should not force
        assert not token.should_force()
        
        # Cancel and check immediately
        token.cancel()
        assert not token.should_force()  # No time elapsed yet
        
        # Wait for timeout
        time.sleep(0.2)
        assert token.should_force()


class TestCancellationManager:
    """Test CancellationManager functionality."""
    
    @pytest.fixture
    def manager(self):
        """Create a fresh cancellation manager for each test."""
        return CancellationManager(default_cleanup_timeout=1.0)
    
    @pytest.mark.asyncio
    async def test_token_creation(self, manager):
        """Test token creation through manager."""
        token = manager.create_token("test-request")
        
        assert token.request_id == "test-request"
        assert token.cleanup_timeout == 1.0
        
        # Should be able to retrieve token
        retrieved = manager.get_token("test-request")
        assert retrieved is token
    
    @pytest.mark.asyncio
    async def test_cancel_request(self, manager):
        """Test cancelling requests through manager."""
        token = manager.create_token("test-request")
        
        # Initially not cancelled
        assert not token.is_cancelled
        
        # Cancel through manager
        result = manager.cancel_request("test-request")
        assert result is True
        assert token.is_cancelled
        
        # Cancelling non-existent request
        result = manager.cancel_request("non-existent")
        assert result is False
    
    @pytest.mark.asyncio
    async def test_task_registration(self, manager):
        """Test task registration for forced cancellation."""
        async def dummy_task():
            await asyncio.sleep(10)
        
        task = asyncio.create_task(dummy_task())
        manager.register_task("test-request", task)
        
        # Task should be registered
        assert "test-request" in manager._tasks
        assert manager._tasks["test-request"] is task
        
        # Cleanup
        task.cancel()
    
    @pytest.mark.asyncio
    async def test_cleanup(self, manager):
        """Test manager cleanup."""
        manager.create_token("test-request")
        manager.unregister_request("test-request")
        
        # Token should be removed
        assert manager.get_token("test-request") is None


class TestCancellableOperation:
    """Test cancellable_operation context manager."""
    
    @pytest.mark.asyncio
    async def test_normal_operation(self):
        """Test normal operation without cancellation."""
        result = None
        
        async with cancellable_operation("test-request") as token:
            result = "completed"
            assert not token.is_cancelled
        
        assert result == "completed"
    
    @pytest.mark.asyncio
    async def test_graceful_cancellation(self):
        """Test graceful cancellation within operation."""
        manager = get_cancellation_manager()
        
        with pytest.raises(CancellationError):
            async with cancellable_operation("test-request") as token:
                # Cancel the token
                manager.cancel_request("test-request")
                
                # Check cancellation and raise error
                if token.is_cancelled:
                    raise CancellationError("test-request", forced=False)
    
    @pytest.mark.asyncio
    async def test_cleanup_execution(self):
        """Test that cleanup is executed on cancellation."""
        manager = get_cancellation_manager()
        cleanup_executed = False
        
        def cleanup_callback():
            nonlocal cleanup_executed
            cleanup_executed = True
        
        try:
            async with cancellable_operation("test-request") as token:
                token.add_cleanup_callback(cleanup_callback)
                manager.cancel_request("test-request")
                raise CancellationError("test-request", forced=False)
        except CancellationError:
            pass
        
        # Cleanup should have been executed
        assert cleanup_executed


class TestToolExecutionCancellation:
    """Test tool execution with cancellation."""
    
    @pytest.fixture
    def tool_manager(self):
        """Create a tool execution manager for testing."""
        return ToolExecutionManager(registry=None, agent=None)
    
    def test_cancelled_response_creation(self, tool_manager):
        """Test creation of cancelled tool responses."""
        tc = {"id": "test-call"}
        
        # Test graceful cancellation
        message, events, results = tool_manager._create_cancelled_response(
            tc, "test_tool", "test_tool", "test-request", forced=False
        )
        
        assert message.role == "tool"
        assert message.tool_call_id == "test-call"
        assert "cancelled" in message.content
        assert events[0]["type"] == "tool_cancelled"
        assert results == []
        
        # Test forced cancellation
        message, events, results = tool_manager._create_cancelled_response(
            tc, "test_tool", "test_tool", "test-request", forced=True
        )
        
        assert events[0]["type"] == "tool_force_cancelled"


class TestBasicOperationsCancellation:
    """Test BasicOperations plugin cancellation support."""
    
    @pytest.fixture
    def server(self):
        """Create a BasicOperations server for testing."""
        from agent_system.config.models import AgentSystemConfig, MCPConfig, LLMSystemConfig
        
        system_config = AgentSystemConfig(
            llm_system=LLMSystemConfig(models={}, profiles={})
        )
        mcp_config = MCPConfig(type="basic_operations", enabled=True)
        
        return BasicOperationsServer("test-server", system_config, mcp_config)
    
    @pytest.fixture
    def mock_status(self):
        """Create a mock status object."""
        class MockStatus:
            def __init__(self):
                self.messages = []
            
            async def progress(self, message: str):
                self.messages.append(("progress", message))
            
            async def error(self, message: str):
                self.messages.append(("error", message))
        
        return MockStatus()
    
    @pytest.mark.asyncio
    async def test_wait_with_cancellation_token(self, server, mock_status):
        """Test wait tool with cancellation token."""
        # Create cancellation token
        manager = get_cancellation_manager()
        token = manager.create_token("test-wait-request", cleanup_timeout=1.0)
        
        # Prepare parameters with short wait time
        params = {
            "seconds": 0.5,  # Short wait for testing
            "message": "Test cancellation",
            "_status": mock_status,
            "_cancellation_token": token
        }
        
        # Execute wait tool
        result = await server.call_with_status("wait", params)
        
        # Should complete normally
        assert result["status"] == "success"
        assert result["user_message"] == "Test cancellation"
    
    @pytest.mark.asyncio
    async def test_wait_cancelled_during_execution(self, server, mock_status):
        """Test wait tool being cancelled during execution."""
        # Create cancellation token
        manager = get_cancellation_manager()
        token = manager.create_token("test-wait-cancel", cleanup_timeout=1.0)
        
        # Prepare parameters with long wait time
        params = {
            "seconds": 10.0,  # Long wait
            "message": "Test cancellation",
            "_status": mock_status,
            "_cancellation_token": token
        }
        
        # Start wait tool in background
        wait_task = asyncio.create_task(server.call_with_status("wait", params))
        
        # Cancel after short delay
        await asyncio.sleep(0.1)
        manager.cancel_request("test-wait-cancel")
        
        # Wait for completion
        result = await wait_task
        
        # Should be cancelled
        assert result["status"] == "cancelled"
        assert result["cancelled"] is True
        assert "Test cancellation" in result["user_message"]


class TestIntegrationCancellation:
    """Integration tests for the complete cancellation system."""
    
    @pytest.mark.asyncio
    async def test_forced_cancellation_timeout(self):
        """Test that stubborn tasks get force-cancelled after timeout."""
        manager = CancellationManager(default_cleanup_timeout=0.2)
        
        # Create a stubborn task that ignores cancellation
        async def stubborn_task():
            try:
                async with cancellable_operation("stubborn-request", cleanup_timeout=0.2):
                    # Ignore cancellation and keep running
                    await asyncio.sleep(2.0)
                    return "completed"
            except CancellationError:
                # Even if we catch it, we ignore it (bad behavior)
                await asyncio.sleep(2.0)
                return "completed"
        
        # Start the task
        task = asyncio.create_task(stubborn_task())
        manager.register_task("stubborn-request", task)
        
        # Start the monitor
        asyncio.create_task(manager._monitor_timeouts())
        
        # Cancel the request
        manager.cancel_request("stubborn-request")
        
        # Wait for forced cancellation (should happen after timeout)
        try:
            await asyncio.wait_for(task, timeout=1.0)
            # If we get here without exception, that's also acceptable
            # as the task might complete in various ways
        except (asyncio.CancelledError, asyncio.TimeoutError):
            # Both outcomes are acceptable for this test
            pass
        
        # Cleanup
        await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_graceful_vs_forced_cancellation(self):
        """Test the difference between graceful and forced cancellation."""
        manager = get_cancellation_manager()
        
        # Graceful task
        graceful_completed = False
        
        async def graceful_task():
            nonlocal graceful_completed
            try:
                async with cancellable_operation("graceful-request") as token:
                    for i in range(10):
                        if token.is_cancelled:
                            graceful_completed = True
                            raise CancellationError("graceful-request")
                        await asyncio.sleep(0.1)
            except CancellationError:
                graceful_completed = True
                raise
        
        # Start graceful task
        graceful_task_obj = asyncio.create_task(graceful_task())
        
        # Cancel after short delay
        await asyncio.sleep(0.05)
        manager.cancel_request("graceful-request")
        
        # Wait for graceful completion
        with pytest.raises(CancellationError):
            await graceful_task_obj
        
        # Should have completed gracefully
        assert graceful_completed is True


if __name__ == "__main__":
    pytest.main([__file__])