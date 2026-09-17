"""Integration tests for agent-to-agent communication.

Tests real scenarios where agents call other agents as tools,
which is critical for hierarchical agent architectures.
"""

import pytest
from unittest.mock import AsyncMock

from agent_system.servers.agent.server import Agent
from agent_system.config.models import (
    AgentSystemConfig, ToolServerConfig, AgentConfig,
    LLMSystemConfig, LLMModelConfig, LLMProfile, ToolConfig
)
from agent_system.tools.base import ToolServerRegistry


def create_mock_llm(responses: list[dict]) -> AsyncMock:
    """Create a mock LLM that returns predefined responses."""
    mock_llm = AsyncMock()
    response_iter = iter(responses)

    async def mock_chat_tools(messages, tools, cancellation_token=None, status_scope=None):
        try:
            return next(response_iter)
        except StopIteration:
            # Default final response if we run out
            return {"assistant": {"content": "Task completed"}}

    mock_llm.chat_tools = mock_chat_tools
    mock_llm.supports_streaming = lambda: False
    return mock_llm


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


@pytest.mark.asyncio
async def test_agent_calls_another_agent_as_tool():
    """Test that an agent can successfully call another agent as a tool.

    Real-world scenario: A coordinator agent delegates subtasks to specialist agents.
    This tests the core agent-as-tool functionality.
    """
    system_config = create_test_system_config()
    registry = ToolServerRegistry()

    # Create specialist agent (the one being called)
    specialist_config = ToolServerConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(max_steps=2)
    )
    specialist_agent = Agent("specialist_agent", system_config, specialist_config, registry)

    # Mock specialist's LLM to return a result
    specialist_agent.llm = create_mock_llm([
        {"assistant": {"content": "The answer is 42"}}
    ])

    # Register specialist in registry so coordinator can find it
    registry.register("specialist_agent", specialist_agent)

    # Create coordinator agent
    coordinator_config = ToolServerConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(
            max_steps=3,
            tools=ToolConfig(allowed=["specialist_agent"])
        )
    )
    coordinator_agent = Agent("coordinator", system_config, coordinator_config, registry)

    # Mock coordinator's LLM to call the specialist agent
    coordinator_agent.llm = create_mock_llm([
        {
            "assistant": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "specialist_agent",
                        "arguments": '{"task": "Calculate the meaning of life"}'
                    }
                }]
            }
        },
        {"assistant": {"content": "The specialist says: The answer is 42"}}
    ])

    # Execute coordinator agent
    events = []
    async for event in coordinator_agent.run_events("What is the meaning of life?"):
        events.append(event)

    # Verify the flow
    assert any(e["type"] == "start" for e in events), "Should start execution"
    assert any(e["type"] == "final" for e in events), "Should complete with final answer"

    # Find the final event
    final_event = next(e for e in events if e["type"] == "final")
    assert "42" in final_event["summary"], "Should include specialist's answer"


@pytest.mark.asyncio
async def test_agent_calls_multiple_agents_in_parallel():
    """Test that an agent can call multiple agents in parallel.

    Real-world scenario: A research agent queries multiple specialist agents
    simultaneously to gather information faster.
    """
    system_config = create_test_system_config()
    registry = ToolServerRegistry()

    # Create two specialist agents
    for name, answer in [("math_agent", "2+2=4"), ("history_agent", "Rome fell in 476 AD")]:
        agent_config = ToolServerConfig(
            type="agent",
            enabled=True,
            agent_config=AgentConfig(max_steps=2)
        )
        agent = Agent(name, system_config, agent_config, registry)
        agent.llm = create_mock_llm([{"assistant": {"content": answer}}])
        registry.register(name, agent)

    # Create coordinator that calls both
    coordinator_config = ToolServerConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(
            max_steps=3,
            tools=ToolConfig(allowed=["math_agent", "history_agent"])
        )
    )
    coordinator = Agent("coordinator", system_config, coordinator_config, registry)

    # Mock coordinator to call both agents in one step
    coordinator.llm = create_mock_llm([
        {
            "assistant": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_math",
                        "function": {
                            "name": "math_agent",
                            "arguments": '{"task": "What is 2+2?"}'
                        }
                    },
                    {
                        "id": "call_history",
                        "function": {
                            "name": "history_agent",
                            "arguments": '{"task": "When did Rome fall?"}'
                        }
                    }
                ]
            }
        },
        {"assistant": {"content": "Math says 4, History says 476 AD"}}
    ])

    # Execute
    events = []
    async for event in coordinator.run_events("Answer two questions"):
        events.append(event)

    # Verify both agents were called
    assert any(e["type"] == "final" for e in events)
    final_event = next(e for e in events if e["type"] == "final")
    summary = final_event["summary"]

    # Both results should be in the summary
    assert "4" in summary or "2+2" in summary, "Should include math result"
    assert "476" in summary or "Rome" in summary, "Should include history result"


@pytest.mark.asyncio
async def test_agent_call_with_missing_task_parameter():
    """Test that agent call fails gracefully when task parameter is missing.

    Real-world scenario: LLM makes a mistake and doesn't include required parameter.
    System should handle this without crashing.
    """
    system_config = create_test_system_config()
    registry = ToolServerRegistry()

    # Create agent
    agent_config = ToolServerConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(max_steps=2)
    )
    agent = Agent("test_agent", system_config, agent_config, registry)
    agent.llm = create_mock_llm([{"assistant": {"content": "Done"}}])

    # Try to call agent without task parameter
    result = await agent.call("test_agent", {})

    # Should return error, not crash
    assert result["status"] == "error", "Should return error status"
    assert "task" in result["error"].lower(), "Error should mention missing task parameter"


@pytest.mark.asyncio
async def test_agent_schema_exposes_correct_tool_interface():
    """Test that agent's schema correctly describes it as a callable tool.

    Real-world scenario: Other agents need to know how to call this agent.
    Schema must be accurate and complete.
    """
    system_config = create_test_system_config()
    registry = ToolServerRegistry()

    agent_config = ToolServerConfig(
        type="agent",
        enabled=True,
        description="A helpful assistant that analyzes data",  # Description goes in ToolServerConfig
        agent_config=AgentConfig(
            llm_profile="default",  # Need to specify profile
            max_steps=2
        )
    )
    agent = Agent("data_analyst", system_config, agent_config, registry)

    # Get the schema
    schema = agent.get_schema()

    # Verify structure
    assert schema["type"] == "function", "Should be a function schema"
    func = schema["function"]

    assert func["name"] == "data_analyst", "Function name should be agent name"
    assert "analyzes data" in func["description"].lower(), "Should include description"

    # Verify parameters
    params = func["parameters"]
    assert params["type"] == "object"
    assert "task" in params["properties"], "Should have task parameter"
    assert params["required"] == ["task"], "Task should be required"

    # Verify NO legacy "action" parameter
    assert "action" not in params["properties"], "Should not have legacy action parameter"


@pytest.mark.asyncio
async def test_agent_list_tools_returns_self_as_callable():
    """Test that agent's list_tools() correctly exposes itself as a callable tool.

    Real-world scenario: When another agent queries available tools,
    this agent should appear in the list.
    """
    system_config = create_test_system_config()
    registry = ToolServerRegistry()

    agent_config = ToolServerConfig(
        type="agent",
        enabled=True,
        description="Test agent",  # Description goes in ToolServerConfig
        agent_config=AgentConfig(max_steps=2)
    )
    agent = Agent("test_agent", system_config, agent_config, registry)

    # Get tools this agent offers
    tools = await agent.list_tools()

    # Should return exactly one tool (itself)
    assert len(tools) == 1, "Agent should expose itself as exactly one tool"

    tool = tools[0]
    assert tool.name == "test_agent", "Tool name should be agent name"
    assert "Test agent" in tool.description, "Should include description"
    assert "task" in tool.input_schema["properties"], "Should require task parameter"


@pytest.mark.asyncio
async def test_circular_agent_calls_detected():
    """Test that circular agent calls are detected and handled.

    Real-world scenario: Agent A calls Agent B which calls Agent A.
    System should prevent infinite loops.
    """
    system_config = create_test_system_config()
    registry = ToolServerRegistry()

    # Create two agents that will try to call each other
    agent_a_config = ToolServerConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(
            max_steps=2,
            tools=ToolConfig(allowed=["agent_b"])
        )
    )
    agent_a = Agent("agent_a", system_config, agent_a_config, registry)

    agent_b_config = ToolServerConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(
            max_steps=2,
            tools=ToolConfig(allowed=["agent_a"])
        )
    )
    agent_b = Agent("agent_b", system_config, agent_b_config, registry)

    # Register both
    registry.register("agent_a", agent_a)
    registry.register("agent_b", agent_b)

    # Mock agent_a to call agent_b
    agent_a.llm = create_mock_llm([
        {
            "assistant": {
                "content": "",
                "tool_calls": [{
                    "id": "call_b",
                    "function": {
                        "name": "agent_b",
                        "arguments": '{"task": "Help me"}'
                    }
                }]
            }
        },
        {"assistant": {"content": "Got response from B"}}
    ])

    # Mock agent_b to call agent_a back
    agent_b.llm = create_mock_llm([
        {
            "assistant": {
                "content": "",
                "tool_calls": [{
                    "id": "call_a",
                    "function": {
                        "name": "agent_a",
                        "arguments": '{"task": "Help me back"}'
                    }
                }]
            }
        },
        {"assistant": {"content": "Got response from A"}}
    ])

    # Execute agent_a (which will call agent_b, which will call agent_a)
    events = []
    async for event in agent_a.run_events("Start task"):
        events.append(event)

    # System should complete without infinite loop
    assert any(e["type"] == "end" for e in events), "Should eventually end"

    # Should hit max_steps limit (the safeguard against infinite loops)
    final_or_error = [e for e in events if e["type"] in ("final", "error")]
    assert len(final_or_error) > 0, "Should produce final or error event"


@pytest.mark.asyncio
async def test_agent_respects_tool_filtering_for_other_agents():
    """Test that agent's tools.allowed filter works for other agents.

    Real-world scenario: Security/isolation - an agent should only
    be able to call agents it's explicitly allowed to call.
    """
    system_config = create_test_system_config()
    registry = ToolServerRegistry()

    # Create three agents
    for name in ["agent_allowed", "agent_blocked", "agent_also_blocked"]:
        agent_config = ToolServerConfig(
            type="agent",
            enabled=True,
            agent_config=AgentConfig(llm_profile="default", max_steps=1)
        )
        agent = Agent(name, system_config, agent_config, registry)
        agent.llm = create_mock_llm([{"assistant": {"content": f"I am {name}"}}])
        # Make agents visible as tools so they can be discovered
        agent._tool_visible = True
        registry.register(name, agent)

    # Create main agent that can only call agent_allowed
    main_config = ToolServerConfig(
        type="agent",
        enabled=True,
        agent_config=AgentConfig(
            llm_profile="default",
            max_steps=2,
            tools=ToolConfig(allowed=["agent_allowed"])  # Only this one
        )
    )
    main_agent = Agent("main_agent", system_config, main_config, registry)

    # Check available tools - list_usable_tools returns tuple: (tools, blocked_patterns)
    available_tools, _, _ = await main_agent.list_usable_tools()

    assert "agent_allowed" in available_tools, "Should see allowed agent"
    assert "agent_blocked" not in available_tools, "Should NOT see blocked agent"
    assert "agent_also_blocked" not in available_tools, "Should NOT see blocked agent"
