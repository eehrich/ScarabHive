"""
Integration test for hallucinated tools.

Verifies that the tool execution layer correctly handles unknown tools
without crashing the agent.
"""
import json
import pytest

from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from tool_execution_test_helpers import execute_tools_collect


@pytest.mark.asyncio
async def test_hallucinated_tool_example_writer_audio():
    """
    Test the specific example from the user: "writer_audio" hallucination.
    
    When an LLM hallucinates a tool like "writer_audio", the agent should:
    1. NOT crash with RuntimeError("Unknown tool: writer_audio")
    2. Return an error message to the LLM
    3. Allow the agent to continue and recover
    """
    # Create a ToolExecutionManager with no servers (simulating hallucination)
    from unittest.mock import MagicMock
    
    registry = MagicMock()
    registry.list = MagicMock(return_value=[])
    registry.get = MagicMock(return_value=None)
    
    agent = MagicMock()
    agent.name = "test_agent"
    agent._get_server_from_any_registry = MagicMock(return_value=None)
    agent._mcp_integration_manager = MagicMock()
    agent._mcp_integration_manager.mcp_integration = None
    agent.registry = registry
    agent.agent_config = MagicMock()
    agent.agent_config.timeouts = MagicMock()
    agent.agent_config.timeouts.tool_cleanup_timeout = 30.0
    
    manager = ToolExecutionManager(registry=registry, agent=agent)
    
    # Simulate LLM calling "writer_audio" which doesn't exist
    tool_calls = [{
        "id": "call_12345",
        "function": {
            "name": "writer_audio",
            "arguments": '{"operation": "create", "filename": "test.wav"}'
        }
    }]
    
    tool_name_mapping = {"writer_audio": "writer_audio"}
    available_tools = ["writer_audio"]  # Tool is listed but server doesn't exist
    step = 1
    request_id = "test_request_writer_audio"
    
    # This should NOT raise RuntimeError!
    # Before fix: Would crash with RuntimeError("Server not found for tool: writer_audio")
    # After fix: Returns error message to LLM
    try:
        tool_messages, events, results = await execute_tools_collect(manager,
            tool_calls, tool_name_mapping, available_tools, step, request_id
        )
        
        # Verify we got an error message, not an exception
        assert len(tool_messages) == 1
        message = tool_messages[0]
        
        # Verify message format
        assert message.role == "tool"
        assert message.tool_call_id == "call_12345"
        assert message.name == "writer_audio"
        
        # Verify error content
        content = json.loads(message.content)
        assert "error" in content, "Should have error field"
        assert "Unknown tool" in content["error"], "Should mention unknown tool"
        assert "writer_audio" in content["error"], "Should mention the hallucinated tool name"
        assert content.get("type") == "ToolNotFoundError", "Should have error type"
        
        # Verify error event
        error_events = [e for e in events if e["type"] == "tool_error"]
        assert len(error_events) == 1
        assert "writer_audio" in error_events[0]["tool"]
        
        print("\n✓ Agent correctly handled hallucinated tool 'writer_audio'")
        print(f"✓ Error message to LLM: {content['error']}")
        print("✓ Agent can now recover and continue execution")
        
    except RuntimeError as e:
        pytest.fail(
            f"Agent should NOT crash with RuntimeError on hallucinated tool!\n"
            f"Got: {e}\n"
            f"Expected: Error message in tool result"
        )


@pytest.mark.asyncio  
async def test_multiple_hallucinated_tools_no_crash():
    """Test that multiple hallucinated tools in parallel don't crash the agent."""
    from unittest.mock import MagicMock
    
    registry = MagicMock()
    registry.list = MagicMock(return_value=[])
    registry.get = MagicMock(return_value=None)
    
    agent = MagicMock()
    agent.name = "test_agent"
    agent._get_server_from_any_registry = MagicMock(return_value=None)
    agent._mcp_integration_manager = MagicMock()
    agent._mcp_integration_manager.mcp_integration = None
    agent.registry = registry
    agent.agent_config = MagicMock()
    agent.agent_config.timeouts = MagicMock()
    agent.agent_config.timeouts.tool_cleanup_timeout = 30.0
    
    manager = ToolExecutionManager(registry=registry, agent=agent)
    
    # Simulate LLM hallucinating multiple tools
    tool_calls = [
        {"id": "call_1", "function": {"name": "writer_audio", "arguments": '{}'}},
        {"id": "call_2", "function": {"name": "fake_tool_xyz", "arguments": '{}'}},
        {"id": "call_3", "function": {"name": "nonexistent", "arguments": '{}'}}
    ]
    
    tool_name_mapping = {
        "writer_audio": "writer_audio",
        "fake_tool_xyz": "fake_tool_xyz",
        "nonexistent": "nonexistent"
    }
    available_tools = ["writer_audio", "fake_tool_xyz", "nonexistent"]
    
    # Should handle all gracefully
    tool_messages, events, results = await execute_tools_collect(manager,
        tool_calls, tool_name_mapping, available_tools, 1, "test_multi"
    )
    
    # All should return error messages
    assert len(tool_messages) == 3
    for message in tool_messages:
        content = json.loads(message.content)
        assert "error" in content
        assert "Unknown tool" in content["error"]
    
    print("\n✓ Agent handled multiple hallucinated tools without crashing")
    print(f"✓ {len(tool_messages)} error messages returned to LLM")
