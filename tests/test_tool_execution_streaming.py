"""
Tests for streaming tool execution with real-time status events.

These tests verify:
1. execute_tools_streaming() yields events during execution
2. execute_tools() wrapper produces same results
3. Parallel execution is maintained
4. Multiple requests work (state reset)
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder


@pytest.fixture
def mock_registry():
    """Mock plugin registry with test tool"""
    registry = MagicMock()
    registry.list.return_value = ["test_tool"]
    
    # Mock server
    mock_server = MagicMock()
    mock_server.get_default_action.return_value = "run"
    
    # Simulate delayed execution
    async def slow_call_with_status(action, params):
        await asyncio.sleep(0.1)  # Simulate work
        return {"content": "Result from test_tool"}
    
    mock_server.call_with_status = AsyncMock(side_effect=slow_call_with_status)
    registry.get.return_value = mock_server
    
    return registry


@pytest.fixture
async def manager_with_streaming(mock_registry):
    """ToolExecutionManager with StatusEventForwarder"""
    forwarder = StatusEventForwarder()
    manager = ToolExecutionManager(
        mock_registry,
        agent=None,
        status_forwarder=forwarder
    )
    
    yield manager, forwarder
    
    # Cleanup
    try:
        await forwarder.stop_forwarding()
    except Exception:
        pass


class TestStreamingToolExecution:
    """Test real-time streaming of tool execution"""
    
    @pytest.mark.asyncio
    async def test_streaming_yields_complete_event(self, manager_with_streaming):
        """execute_tools_streaming() should yield complete event with results"""
        manager, forwarder = manager_with_streaming
        
        tool_calls = [{
            "id": "call_1",
            "function": {
                "name": "test_tool",
                "arguments": "{}"
            }
        }]
        
        complete_received = False
        tool_messages = []
        
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="test123"
        ):
            if item["type"] == "complete":
                complete_received = True
                tool_messages = item["messages"]
        
        assert complete_received, "No complete event received!"
        assert len(tool_messages) == 1
        assert tool_messages[0].role == "tool"
    
    @pytest.mark.asyncio
    async def test_parallel_execution_maintained(self, manager_with_streaming):
        """Tools should still execute in parallel with streaming"""
        manager, forwarder = manager_with_streaming
        
        # Create 3 tool calls (each takes 0.1s)
        tool_calls = [
            {
                "id": f"call_{i}",
                "function": {"name": "test_tool", "arguments": "{}"}
            }
            for i in range(3)
        ]
        
        start_time = asyncio.get_event_loop().time()
        
        # Collect results
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="test456"
        ):
            if item["type"] == "complete":
                messages = item["messages"]
                break
        
        elapsed = asyncio.get_event_loop().time() - start_time
        
        # 3 tools @ 0.1s each should complete in ~0.1s (parallel), not 0.3s (sequential)
        assert elapsed < 0.25, f"Took {elapsed:.2f}s - not parallel! (expected <0.25s)"
        assert len(messages) == 3, "Missing tool results"
    
    @pytest.mark.asyncio
    async def test_wrapper_compatibility(self, manager_with_streaming):
        """execute_tools() wrapper should produce same results as streaming version"""
        manager, forwarder = manager_with_streaming
        
        tool_calls = [{
            "id": "call_1",
            "function": {"name": "test_tool", "arguments": "{}"}
        }]
        
        # Call wrapper version
        messages, events, results = await manager.execute_tools(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="test999"
        )
        
        # Should get same messages as streaming version
        assert len(messages) == 1
        assert messages[0].role == "tool"
    
    @pytest.mark.asyncio
    async def test_multiple_streaming_calls(self, manager_with_streaming):
        """Multiple sequential streaming calls should work (state reset)"""
        manager, forwarder = manager_with_streaming
        
        tool_calls = [{
            "id": "call_1",
            "function": {"name": "test_tool", "arguments": "{}"}
        }]
        
        # First call
        completed_first = False
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="req1"
        ):
            if item["type"] == "complete":
                completed_first = True
                break
        
        assert completed_first
        
        # Second call should work without hanging
        completed_second = False
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="req2"
        ):
            if item["type"] == "complete":
                completed_second = True
                break
        
        assert completed_second, "Second streaming call hung!"
    
    @pytest.mark.asyncio
    async def test_wrapper_and_streaming_produce_same_results(self, manager_with_streaming):
        """Wrapper and streaming version should yield identical results"""
        manager, forwarder = manager_with_streaming
        
        tool_calls = [
            {"id": f"call_{i}", "function": {"name": "test_tool", "arguments": "{}"}}
            for i in range(2)
        ]
        
        # Streaming version
        streaming_messages = []
        async for item in manager.execute_tools_streaming(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="stream_test"
        ):
            if item["type"] == "complete":
                streaming_messages = item["messages"]
        
        # Wrapper version
        wrapper_messages, _, _ = await manager.execute_tools(
            tool_calls=tool_calls,
            tool_name_mapping={"test_tool": "test_tool"},
            available_tools=["test_tool"],
            step=1,
            request_id="wrapper_test"
        )
        
        # Same number of messages
        assert len(streaming_messages) == len(wrapper_messages)
        
        # Same message roles
        assert all(m1.role == m2.role for m1, m2 in zip(streaming_messages, wrapper_messages))

