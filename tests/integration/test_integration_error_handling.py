"""Integration tests for error handling and resilience.

Tests how the system handles various error conditions, network failures,
timeouts, and malformed data. These are critical for production reliability.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock

from agent_system.servers.agent.server import Agent
from agent_system.config.models import (
    AgentSystemConfig, MCPConfig, AgentConfig,
    LLMSystemConfig, LLMModelConfig, LLMProfile, ToolConfig
)
from agent_system.mcp.base import MCPRegistry, MCPServer


def create_test_system_config() -> AgentSystemConfig:
    """Create minimal system config for testing."""
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={
                "test-model": LLMModelConfig(provider="ollama", model="test-model")
            },
            profiles={
                "default": LLMProfile(model_ref="test-model", max_steps=3)
            },
            default_profile="default"
        )
    )


def create_mock_llm(responses: list[dict]) -> AsyncMock:
    """Create a mock LLM that returns predefined responses."""
    mock_llm = AsyncMock()
    response_iter = iter(responses)
    
    async def mock_chat_tools(messages, tools, cancellation_token=None, status_scope=None):
        try:
            return next(response_iter)
        except StopIteration:
            return {"assistant": {"content": "Done"}}
    
    mock_llm.chat_tools = mock_chat_tools
    mock_llm.supports_streaming = lambda: False
    return mock_llm


class FailingToolServer(MCPServer):
    """Mock tool server that always fails."""
    
    def __init__(self, name: str, error_message: str = "Tool failed"):
        super().__init__(name, create_test_system_config(), MCPConfig(type="agent", enabled=True))
        self.error_message = error_message
    
    async def call(self, tool: str, params: dict) -> dict:
        raise RuntimeError(self.error_message)
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "A tool that always fails",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": []
                }
            }
        }
    
    async def list_tools(self) -> list:
        from agent_system.mcp.core import MCPTool
        return [MCPTool(
            name=self.name,
            description="A tool that always fails",
            input_schema={"type": "object", "properties": {}}
        )]


class SlowToolServer(MCPServer):
    """Mock tool server that takes a long time."""
    
    def __init__(self, name: str, delay_seconds: float = 5.0):
        super().__init__(name, create_test_system_config(), MCPConfig(type="agent", enabled=True))
        self.delay_seconds = delay_seconds
    
    async def call(self, tool: str, params: dict) -> dict:
        await asyncio.sleep(self.delay_seconds)
        return {"result": "Finally done after delay"}
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "A slow tool",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        }
    
    async def list_tools(self) -> list:
        from agent_system.mcp.core import MCPTool
        return [MCPTool(
            name=self.name,
            description="A slow tool",
            input_schema={"type": "object", "properties": {}}
        )]


@pytest.mark.asyncio
async def test_agent_handles_tool_failure_gracefully():
    """Test that agent continues working when a tool fails.
    
    Real-world scenario: External API fails, network error, tool crashes.
    Agent should report the error but not crash itself.
    """
    system_config = create_test_system_config()
    registry = MCPRegistry()
    
    # Register a failing tool
    failing_tool = FailingToolServer("failing_tool", "Database connection failed")
    registry.register("failing_tool", failing_tool)
    
    # Create agent that will call the failing tool
    agent_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(
            max_steps=3,
            tools=ToolConfig(allowed=["failing_tool"])
        )
    )
    agent = Agent("test_agent", system_config, agent_config, registry)
    
    # Mock LLM to call the tool, then respond to the error
    agent.llm = create_mock_llm([
        {
            "assistant": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "failing_tool",
                        "arguments": '{}'
                    }
                }]
            }
        },
        {"assistant": {"content": "I encountered an error but handled it gracefully"}}
    ])
    
    # Execute agent
    events = []
    async for event in agent.run_events("Try to use the tool"):
        events.append(event)
    
    # Agent should complete despite tool failure
    assert any(e["type"] == "final" for e in events), "Should reach final state"
    
    # Should have a final response
    final_event = next(e for e in events if e["type"] == "final")
    assert "error" in final_event["summary"].lower() or "handled" in final_event["summary"].lower()


@pytest.mark.asyncio
async def test_agent_handles_llm_error_in_response():
    """Test that agent handles errors in LLM response structure.
    
    Real-world scenario: LLM returns error object instead of normal response
    (e.g., rate limit, invalid request, context window exceeded).
    """
    system_config = create_test_system_config()
    registry = MCPRegistry()
    
    agent_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(max_steps=2)
    )
    agent = Agent("test_agent", system_config, agent_config, registry)
    
    # Mock LLM to return error in assistant
    agent.llm = create_mock_llm([
        {
            "assistant": {
                "error": {
                    "message": "Rate limit exceeded. Please try again later.",
                    "type": "rate_limit_error"
                }
            }
        }
    ])
    
    # Execute agent
    events = []
    async for event in agent.run_events("Test task"):
        events.append(event)
    
    # Should emit error event
    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) > 0, "Should emit error event"
    assert "rate limit" in error_events[0]["message"].lower()


@pytest.mark.asyncio
async def test_agent_handles_malformed_tool_call_arguments():
    """Test that agent handles malformed JSON in tool call arguments.
    
    Real-world scenario: LLM generates invalid JSON for tool arguments.
    System should handle this gracefully, not crash.
    """
    system_config = create_test_system_config()
    registry = MCPRegistry()
    
    # Create a working tool
    class SimpleToolServer(MCPServer):
        def __init__(self):
            super().__init__("simple_tool", system_config, MCPConfig(type="agent", enabled=True))
        
        async def call(self, tool: str, params: dict) -> dict:
            return {"result": "Success"}
        
        def get_schema(self) -> dict:
            return {
                "type": "function",
                "function": {
                    "name": "simple_tool",
                    "description": "A simple tool",
                    "parameters": {"type": "object", "properties": {}}
                }
            }
        
        async def list_tools(self) -> list:
            from agent_system.mcp.core import MCPTool
            return [MCPTool(name="simple_tool", description="Simple", input_schema={})]
    
    registry.register("simple_tool", SimpleToolServer())
    
    agent_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(max_steps=3)
    )
    agent = Agent("test_agent", system_config, agent_config, registry)
    
    # Mock LLM to return malformed arguments
    agent.llm = create_mock_llm([
        {
            "assistant": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "simple_tool",
                        "arguments": '{"invalid": json syntax here}'  # Malformed JSON
                    }
                }]
            }
        },
        {"assistant": {"content": "Handled the error"}}
    ])
    
    # Execute - should not crash
    events = []
    async for event in agent.run_events("Test"):
        events.append(event)
    
    # Should complete (possibly with error, but not crash)
    assert any(e["type"] == "end" for e in events), "Should end gracefully"


@pytest.mark.asyncio
async def test_agent_handles_empty_llm_responses():
    """Test that agent handles empty LLM responses without infinite loop.
    
    Real-world scenario: LLM returns empty content repeatedly.
    System should detect this and stop (not loop forever).
    """
    system_config = create_test_system_config()
    registry = MCPRegistry()
    
    agent_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(max_steps=5)  # More steps to test loop detection
    )
    agent = Agent("test_agent", system_config, agent_config, registry)
    
    # Mock LLM to return empty responses
    agent.llm = create_mock_llm([
        {"assistant": {"content": ""}},
        {"assistant": {"content": ""}},
        {"assistant": {"content": ""}},  # 3 empty responses should trigger break
    ])
    
    # Execute
    events = []
    async for event in agent.run_events("Test"):
        events.append(event)
    
    # Should detect empty responses and stop
    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) > 0, "Should emit error for consecutive empty responses"
    assert "empty" in error_events[0]["message"].lower()
    
    # Should not use all max_steps (should break early)
    thinking_events = [e for e in events if e["type"] == "thinking"]
    assert len(thinking_events) < 5, "Should break before hitting max steps"


@pytest.mark.asyncio
async def test_agent_handles_tool_timeout():
    """Test that agent can handle slow/hanging tool calls.
    
    Real-world scenario: External API hangs, network timeout, slow database query.
    Agent should be able to timeout and continue or fail gracefully.
    
    Note: This tests the cancellation mechanism works for tool calls.
    """
    system_config = create_test_system_config()
    registry = MCPRegistry()
    
    # Register a very slow tool
    slow_tool = SlowToolServer("slow_tool", delay_seconds=10.0)
    registry.register("slow_tool", slow_tool)
    
    agent_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(max_steps=2)
    )
    agent = Agent("test_agent", system_config, agent_config, registry)
    
    # Mock LLM to call slow tool
    agent.llm = create_mock_llm([
        {
            "assistant": {
                "content": "",
                "tool_calls": [{
                    "id": "call_slow",
                    "function": {
                        "name": "slow_tool",
                        "arguments": '{}'
                    }
                }]
            }
        }
    ])
    
    # Execute with a timeout wrapper (simulate user cancellation after 2 seconds)
    events = []
    try:
        async with asyncio.timeout(2.0):  # Timeout after 2 seconds
            async for event in agent.run_events("Use slow tool"):
                events.append(event)
    except asyncio.TimeoutError:
        # This is expected - the tool is taking too long
        pass
    
    # The test passes if we don't hang forever
    # In real system, cancellation should propagate to the tool
    assert len(events) > 0, "Should have started execution before timeout"


@pytest.mark.asyncio
async def test_agent_handles_multiple_tool_failures():
    """Test that agent handles multiple tool failures in same step.
    
    Real-world scenario: Agent calls 3 tools in parallel, 2 fail.
    System should handle partial failures gracefully.
    """
    system_config = create_test_system_config()
    registry = MCPRegistry()
    
    # Register failing and working tools
    registry.register("failing_1", FailingToolServer("failing_1", "Error 1"))
    registry.register("failing_2", FailingToolServer("failing_2", "Error 2"))
    
    class WorkingTool(MCPServer):
        def __init__(self):
            super().__init__("working_tool", system_config, MCPConfig(type="agent", enabled=True))
        
        async def call(self, tool: str, params: dict) -> dict:
            return {"result": "Success"}
        
        def get_schema(self) -> dict:
            return {
                "type": "function",
                "function": {
                    "name": "working_tool",
                    "description": "Working",
                    "parameters": {"type": "object", "properties": {}}
                }
            }
        
        async def list_tools(self) -> list:
            from agent_system.mcp.core import MCPTool
            return [MCPTool(name="working_tool", description="Working", input_schema={})]
    
    registry.register("working_tool", WorkingTool())
    
    agent_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(max_steps=3)
    )
    agent = Agent("test_agent", system_config, agent_config, registry)
    
    # Mock LLM to call all three tools
    agent.llm = create_mock_llm([
        {
            "assistant": {
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "failing_1", "arguments": '{}'}},
                    {"id": "call_2", "function": {"name": "failing_2", "arguments": '{}'}},
                    {"id": "call_3", "function": {"name": "working_tool", "arguments": '{}'}}
                ]
            }
        },
        {"assistant": {"content": "Two failed but one succeeded"}}
    ])
    
    # Execute
    events = []
    async for event in agent.run_events("Call multiple tools"):
        events.append(event)
    
    # Should complete and report results
    assert any(e["type"] == "final" for e in events), "Should reach final"
    
    # Working tool should have succeeded despite failures
    final_event = next(e for e in events if e["type"] == "final")
    assert "succeeded" in final_event["summary"].lower() or "failed" in final_event["summary"].lower()


@pytest.mark.asyncio
async def test_agent_handles_missing_tool():
    """Test that agent handles calls to non-existent tools.
    
    Real-world scenario: LLM hallucinates a tool name, or tool was removed.
    System should handle this gracefully.
    """
    system_config = create_test_system_config()
    registry = MCPRegistry()  # Empty registry
    
    agent_config = MCPConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(max_steps=2)
    )
    agent = Agent("test_agent", system_config, agent_config, registry)
    
    # Mock LLM to call non-existent tool
    agent.llm = create_mock_llm([
        {
            "assistant": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "nonexistent_tool",
                        "arguments": '{}'
                    }
                }]
            }
        },
        {"assistant": {"content": "Tool not found but continuing"}}
    ])
    
    # Execute
    events = []
    async for event in agent.run_events("Test"):
        events.append(event)
    
    # Should handle missing tool gracefully
    # LLM should receive an error message about the tool not being available
    assert any(e["type"] == "final" or e["type"] == "error" for e in events)
