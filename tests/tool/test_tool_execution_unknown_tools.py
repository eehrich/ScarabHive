"""
Tests for handling unknown/hallucinated tool calls.

When an LLM hallucinates a tool name, the agent should return an error message
instead of crashing with a RuntimeError.
"""
import asyncio
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from agent_system.llm.models import ChatMessage
from tool_execution_test_helpers import execute_tools_collect


@pytest.fixture
def empty_registry():
    """Create an empty registry."""
    registry = MagicMock()
    registry.list = MagicMock(return_value=[])
    registry.get = MagicMock(return_value=None)
    return registry


@pytest.fixture
def mock_agent():
    """Create a mock agent without the hallucinated tool."""
    agent = MagicMock()
    agent.name = "test_agent"
    agent.agent_config = MagicMock()
    agent.agent_config.timeouts = MagicMock()
    agent.agent_config.timeouts.tool_cleanup_timeout = 30.0
    
    # Mock _get_server_from_any_registry to return None (tool not found)
    agent._get_server_from_any_registry = MagicMock(return_value=None)
    
    # Mock MCP integration manager
    agent._mcp_integration_manager = MagicMock()
    agent._mcp_integration_manager.mcp_integration = None
    
    # Mock registry (empty)
    agent.registry = MagicMock()
    agent.registry.list = MagicMock(return_value=[])
    agent.registry.get = MagicMock(return_value=None)
    
    return agent


@pytest.fixture
def tool_execution_manager(empty_registry, mock_agent):
    """Create a ToolExecutionManager with empty registry and mock agent."""
    return ToolExecutionManager(registry=empty_registry, agent=mock_agent)


class TestUnknownToolHandling:
    """Test cases for handling unknown/hallucinated tools."""

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_not_exception(self, tool_execution_manager):
        """Test that unknown tool returns error message instead of raising exception."""
        # Simulate LLM hallucinating a tool "writer_audio"
        tool_calls = [{
            "id": "call_123",
            "function": {
                "name": "writer_audio",
                "arguments": '{"operation": "create"}'
            }
        }]
        
        tool_name_mapping = {"writer_audio": "writer_audio"}
        available_tools = ["writer_audio"]  # Tool is in available_tools but server doesn't exist
        step = 1
        request_id = "test_request_123"
        
        # Execute tools - should NOT raise exception
        tool_messages, events, results = await execute_tools_collect(tool_execution_manager,
            tool_calls, tool_name_mapping, available_tools, step, request_id
        )
        
        # Verify we got an error message instead of an exception
        assert len(tool_messages) == 1
        message = tool_messages[0]
        
        assert message.role == "tool"
        assert message.tool_call_id == "call_123"
        assert message.name == "writer_audio"
        
        # Parse content to check error message
        content = json.loads(message.content)
        assert "error" in content
        assert "Unknown tool" in content["error"]
        assert "writer_audio" in content["error"]
        assert content.get("type") == "ToolNotFoundError"
        
        # Verify event was emitted
        assert len(events) == 1
        assert events[0]["type"] == "tool_error"
        assert events[0]["tool"] == "writer_audio"

    @pytest.mark.asyncio
    async def test_multiple_unknown_tools_all_return_errors(self, tool_execution_manager):
        """Test that multiple unknown tools all return error messages."""
        # Simulate LLM hallucinating multiple tools
        tool_calls = [
            {
                "id": "call_1",
                "function": {
                    "name": "writer_audio",
                    "arguments": '{}'
                }
            },
            {
                "id": "call_2",
                "function": {
                    "name": "fake_tool",
                    "arguments": '{}'
                }
            },
            {
                "id": "call_3",
                "function": {
                    "name": "nonexistent_plugin",
                    "arguments": '{}'
                }
            }
        ]
        
        tool_name_mapping = {
            "writer_audio": "writer_audio",
            "fake_tool": "fake_tool",
            "nonexistent_plugin": "nonexistent_plugin"
        }
        available_tools = ["writer_audio", "fake_tool", "nonexistent_plugin"]
        step = 1
        request_id = "test_request_multi"
        
        # Execute tools - should NOT raise exception
        tool_messages, events, results = await execute_tools_collect(tool_execution_manager,
            tool_calls, tool_name_mapping, available_tools, step, request_id
        )
        
        # Verify we got error messages for all tools
        assert len(tool_messages) == 3
        
        for i, message in enumerate(tool_messages):
            assert message.role == "tool"
            content = json.loads(message.content)
            assert "error" in content
            assert "Unknown tool" in content["error"]
            assert content.get("type") == "ToolNotFoundError"
        
        # Verify all error events were emitted
        error_events = [e for e in events if e["type"] == "tool_error"]
        assert len(error_events) == 3

    @pytest.mark.asyncio
    async def test_unknown_tool_not_in_available_tools(self, tool_execution_manager):
        """Test that tool not in available_tools is also handled gracefully."""
        # Simulate LLM hallucinating a tool that's not even in available_tools
        tool_calls = [{
            "id": "call_xyz",
            "function": {
                "name": "completely_made_up_tool",
                "arguments": '{}'
            }
        }]
        
        tool_name_mapping = {"completely_made_up_tool": "completely_made_up_tool"}
        available_tools = []  # Tool is NOT in available_tools
        step = 1
        request_id = "test_request_not_available"
        
        # Execute tools - should NOT raise exception
        tool_messages, events, results = await execute_tools_collect(tool_execution_manager,
            tool_calls, tool_name_mapping, available_tools, step, request_id
        )
        
        # Verify we got an error message
        assert len(tool_messages) == 1
        message = tool_messages[0]
        
        assert message.role == "tool"
        content = json.loads(message.content)
        assert "error" in content
        # Error message should indicate the tool doesn't exist
        assert "does not exist" in content["error"] or "Unknown tool" in content["error"]

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_tools(self, tool_execution_manager, mock_agent):
        """Test that when some tools are valid and others are not, all are handled correctly."""
        # Create a mock server for valid_tool
        mock_server = AsyncMock()
        mock_server.call_with_status = AsyncMock(return_value={"result": "success"})
        
        # Update mock_agent to return server for valid_tool
        def get_server(tool_name):
            if tool_name == "valid_tool":
                return mock_server
            return None
        
        mock_agent._get_server_from_any_registry.side_effect = get_server
        
        # Tool calls: one valid, one invalid
        tool_calls = [
            {
                "id": "call_valid",
                "function": {
                    "name": "valid_tool",
                    "arguments": '{}'
                }
            },
            {
                "id": "call_invalid",
                "function": {
                    "name": "invalid_tool",
                    "arguments": '{}'
                }
            }
        ]
        
        tool_name_mapping = {
            "valid_tool": "valid_tool",
            "invalid_tool": "invalid_tool"
        }
        available_tools = ["valid_tool", "invalid_tool"]
        step = 1
        request_id = "test_request_mixed"
        
        # Execute tools
        tool_messages, events, results = await execute_tools_collect(tool_execution_manager,
            tool_calls, tool_name_mapping, available_tools, step, request_id
        )
        
        # Verify we got 2 messages: one success, one error
        assert len(tool_messages) == 2
        
        # Check valid tool result
        valid_msg = [m for m in tool_messages if m.tool_call_id == "call_valid"][0]
        valid_content = json.loads(valid_msg.content)
        assert "result" in valid_content
        
        # Check invalid tool error
        invalid_msg = [m for m in tool_messages if m.tool_call_id == "call_invalid"][0]
        invalid_content = json.loads(invalid_msg.content)
        assert "error" in invalid_content
        assert "Unknown tool" in invalid_content["error"]

    @pytest.mark.asyncio
    async def test_streaming_unknown_tool_yields_error(self, tool_execution_manager):
        """Test that streaming execution also handles unknown tools correctly."""
        tool_calls = [{
            "id": "call_stream",
            "function": {
                "name": "unknown_streaming_tool",
                "arguments": '{}'
            }
        }]
        
        tool_name_mapping = {"unknown_streaming_tool": "unknown_streaming_tool"}
        available_tools = ["unknown_streaming_tool"]
        step = 1
        request_id = "test_request_stream"
        
        # Collect all streaming results
        results_list = []
        async for item in tool_execution_manager.execute_tools_streaming(
            tool_calls, tool_name_mapping, available_tools, step, request_id
        ):
            results_list.append(item)
        
        # Find the complete event
        complete_events = [r for r in results_list if r.get("type") == "complete"]
        assert len(complete_events) == 1
        
        messages = complete_events[0]["messages"]
        assert len(messages) == 1
        
        content = json.loads(messages[0].content)
        assert "error" in content
        assert "Unknown tool" in content["error"]

    @pytest.mark.asyncio
    async def test_error_message_format(self, tool_execution_manager):
        """Test that error message has correct format for LLM recovery."""
        tool_calls = [{
            "id": "call_format",
            "function": {
                "name": "hallucinated_tool",
                "arguments": '{}'
            }
        }]
        
        tool_name_mapping = {"hallucinated_tool": "hallucinated_tool"}
        available_tools = ["hallucinated_tool"]
        step = 1
        request_id = "test_request_format"
        
        tool_messages, events, results = await execute_tools_collect(tool_execution_manager,
            tool_calls, tool_name_mapping, available_tools, step, request_id
        )
        
        message = tool_messages[0]
        content = json.loads(message.content)
        
        # Verify error message gives helpful guidance to LLM
        assert "error" in content
        assert "hallucinated_tool" in content["error"]
        assert "does not exist" in content["error"]
        assert "check available tools" in content["error"]
        assert content["type"] == "ToolNotFoundError"
        
        # Verify event has error info
        error_event = [e for e in events if e["type"] == "tool_error"][0]
        assert "Unknown tool" in error_event["error"]
