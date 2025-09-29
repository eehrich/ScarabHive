import pytest

from agent_system.servers.agent.server import Agent
from agent_system.config.models import AgentConfig
from agent_system.mcp.base import MCPRegistry
from test_agent_comprehensive import MockMCPServer


class MockLLMClient:
    """Mock LLM client that returns responses without tool calls to trigger infinite loop prevention."""
    
    def __init__(self, response_type="empty"):
        self.response_type = response_type
        self.call_count = 0
    
    async def chat_tools(self, messages, tools, cancellation_token=None):
        self.call_count += 1
        
        if self.response_type == "empty":
            return {"assistant": {"role": "assistant", "content": None, "tool_calls": None}}
        elif self.response_type == "content_only":
            return {"assistant": {"role": "assistant", "content": f"Response {self.call_count}", "tool_calls": None}}
        elif self.response_type == "mixed":
            # First call has tool calls, subsequent calls don't
            if self.call_count == 1:
                return {"assistant": {"role": "assistant", "content": "First response", "tool_calls": [{"function": {"name": "test_tool", "arguments": "{}"}}]}}
            else:
                return {"assistant": {"role": "assistant", "content": f"Follow-up {self.call_count}", "tool_calls": None}}


@pytest.mark.asyncio
async def test_agent_prevents_infinite_loop_empty_responses():
    """Test that agent breaks out of loop when getting consecutive empty responses."""
    config = AgentConfig(max_steps=10)
    registry = MCPRegistry()
    
    # Mock LLM that returns empty responses
    mock_llm = MockLLMClient("empty")
    
    agent = Agent("test_agent", config, registry, llm=mock_llm)
    
    # Run agent and collect events
    events = []
    async for event in agent.run_events("test task"):
        events.append(event)
        if event.get("type") in ["final", "error", "end"]:
            break
    
    # Should break early due to consecutive empty responses
    assert mock_llm.call_count <= 3  # Should stop after 2 consecutive empty responses
    
    # Should have an error event about empty responses
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) > 0
    assert "consecutive empty" in error_events[-1]["message"]


@pytest.mark.asyncio
async def test_agent_prevents_infinite_loop_no_tool_calls():
    """Test that agent breaks out of loop when getting consecutive responses without tool calls."""
    config = AgentConfig(max_steps=10)
    registry = MCPRegistry()
    
    # Mock LLM that returns content but no tool calls
    mock_llm = MockLLMClient("content_only")
    
    agent = Agent("test_agent", config, registry, llm=mock_llm)
    
    # Run agent and collect events
    events = []
    async for event in agent.run_events("test task"):
        events.append(event)
        if event.get("type") in ["final", "error", "end"]:
            break
    
    # Should break early due to consecutive responses without tool calls
    assert mock_llm.call_count <= 4  # Should stop after 3 consecutive no-tool responses
    
    # Should either have a final event (if content was present) or error event
    final_events = [e for e in events if e.get("type") == "final"]
    error_events = [e for e in events if e.get("type") == "error"]
    
    assert len(final_events) > 0 or len(error_events) > 0
    
    if error_events:
        assert "consecutive responses without tool calls" in error_events[-1]["message"]


@pytest.mark.asyncio 
async def test_agent_normal_execution_not_affected():
    """Test that normal agent execution with tool calls is not affected by the safeguards."""
    config = AgentConfig(max_steps=10)
    registry = MCPRegistry()
    
    # Add a mock tool to registry
    mock_tool = MockMCPServer("test_tool")
    registry.register("test_tool", mock_tool)
    
    # Mock LLM that makes tool calls initially, then provides final content
    class NormalMockLLM:
        def __init__(self):
            self.call_count = 0
        
        async def chat_tools(self, messages, tools, cancellation_token=None):
            self.call_count += 1
            if self.call_count == 1:
                # First call: make a tool call
                return {
                    "assistant": {
                        "role": "assistant", 
                        "content": "I'll use the test tool",
                        "tool_calls": [{"id": "call_1", "function": {"name": "test_tool", "arguments": "{}"}}]
                    }
                }
            else:
                # Second call: provide final answer
                return {
                    "assistant": {
                        "role": "assistant",
                        "content": "Task completed successfully",
                        "tool_calls": None
                    }
                }
    
    mock_llm = NormalMockLLM()
    agent = Agent("test_agent", config, registry, llm=mock_llm)
    
    # Run agent and collect events
    events = []
    async for event in agent.run_events("test task"):
        events.append(event)
        if event.get("type") in ["final", "error", "end"]:
            break
    
    # Should complete normally without triggering safeguards
    final_events = [e for e in events if e.get("type") == "final"]
    assert len(final_events) > 0
    assert "Task completed successfully" in final_events[0]["summary"]
    
    # Should have made the expected tool call
    assert mock_tool.called