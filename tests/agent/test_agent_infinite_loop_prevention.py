import pytest

from agent_system.servers.agent.server import Agent
from agent_system.config.models import AgentConfig
from agent_system.tools.base import ToolServerRegistry


def create_test_config():
    """Create a test configuration with the new LLM system structure."""
    cfg = AgentConfig(
        max_steps=10
    )
    # Allow all tools in tests by default so registry-registered mock tools are reachable
    cfg.tools.allowed = ["*"]
    return cfg


class MockLLMClient:
    """Mock LLM client that returns responses without tool calls to trigger infinite loop prevention."""

    def __init__(self, response_type="empty"):
        self.response_type = response_type
        self.call_count = 0

    def supports_streaming(self):
        """Mock LLM doesn't support streaming."""
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
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
    """Test that agent breaks out of loop when getting consecutive empty responses.

    With the 'Continue' prompt injection feature:
    - First 2 empty responses: no injection yet (max_consecutive_empty=2)
    - After 2 empty: starts injecting "Continue with your task." messages
    - After 3 more empty responses (5 total): gives up
    """
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

    agent_config = create_test_config()
    system_config = AgentSystemConfig()
    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = ToolServerRegistry()

    # Mock LLM that returns empty responses
    mock_llm = MockLLMClient("empty")

    agent = Agent("test_agent", system_config, server_config, registry, llm=mock_llm)

    # Run agent and collect events
    events = []
    async for event in agent.run_events("test task"):
        events.append(event)
        if event.get("type") in ["final", "error", "end"]:
            break

    # Should stop after 5 consecutive empty responses (2 initial + 3 with "Continue" prompts)
    assert mock_llm.call_count <= 6  # May have 1 extra call before final break

    # Should have an error event about empty responses
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) > 0
    assert "empty responses repeatedly" in error_events[-1]["message"]


@pytest.mark.asyncio
async def test_agent_prevents_infinite_loop_no_tool_calls():
    """Test that agent breaks out of loop when getting consecutive responses without tool calls."""
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

    agent_config = create_test_config()
    system_config = AgentSystemConfig()
    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = ToolServerRegistry()

    # Mock LLM that returns content but no tool calls
    mock_llm = MockLLMClient("content_only")

    agent = Agent("test_agent", system_config, server_config, registry, llm=mock_llm)

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
    """Test that normal agent execution is not affected by the safeguards.

    This test verifies that an LLM providing normal responses (content without tool calls)
    doesn't trigger the infinite loop safeguards when it terminates naturally.
    """
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig, LLMSystemConfig, LLMModelConfig

    agent_config = create_test_config()
    system_config = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"test-model": LLMModelConfig(provider="openai", model="test-model")},
            default_profile="normal",
            profiles={"normal": {"model_ref": "test-model"}}
        )
    )
    server_config = ToolServerConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = ToolServerRegistry()

    # Mock LLM that provides a normal response (content, no tool calls)
    class NormalMockLLM:
        def __init__(self):
            self.call_count = 0

        def supports_streaming(self):
            """Mock LLM doesn't support streaming."""
            return False

        async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
            self.call_count += 1
            # Provide a normal final answer immediately
            return {
                "assistant": {
                    "role": "assistant",
                    "content": "Task completed successfully",
                    "tool_calls": None
                }
            }

    mock_llm = NormalMockLLM()
    agent = Agent("test_agent", system_config, server_config, registry, llm=mock_llm)

    # Run agent and collect events
    events = []
    async for event in agent.run_events("test task"):
        events.append(event)
        if event.get("type") in ["final", "error", "end"]:
            break

    # Should complete normally without triggering safeguards
    final_events = [e for e in events if e.get("type") == "final"]
    error_events = [e for e in events if e.get("type") == "error"]

    # Should get a final event (normal completion)
    assert len(final_events) > 0
    assert "Task completed successfully" in final_events[0]["summary"]

    # Should not have any errors
    assert len(error_events) == 0

    # Should have called LLM only once (immediate completion)
    assert mock_llm.call_count == 1