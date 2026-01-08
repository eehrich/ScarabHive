import pytest
from agent_system.utils.id import short_id

from agent_system.servers.agent.server import Agent
from agent_system.mcp.base import MCPRegistry
from agent_system.config.settings import load_settings as load_config


class DummyLLM:
    async def chat(self, messages, tools_schema=None):
        # Simulate LLM generating a plan and then a simple assistant response
        # Return a planner-like response format
        return {"assistant": {"content": "planner response", "tool_calls": []}, "usage": {"total_tokens": 10}}

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        # Final answer
        return {"assistant": {"content": "final response"}, "usage": {"total_tokens": 5}}


@pytest.mark.asyncio
async def test_append_message_consumed(tmp_path):
    from agent_system.config.models import MCPConfig
    
    # Load default config - returns AgentSystemConfig
    system_config = load_config("config/config.yaml")
    
    # Get agent config from system config (should have default agent config)
    agent_config = system_config.agent_config if hasattr(system_config, 'agent_config') and system_config.agent_config else None
    
    if not agent_config:
        # Create minimal agent config for test
        from agent_system.config.models import AgentConfig
        agent_config = AgentConfig()
    
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    
    agent = Agent("test_agent", system_config, mcp_config, registry)
    # Inject dummy LLM
    agent.llm = DummyLLM()

    task = "Initial task"
    request_id = short_id()

    # Start run_events generator
    gen = agent.run_events(task, request_id=request_id)

    # Prime generator until start event
    start_ev = await gen.__anext__()
    assert start_ev["type"] == "start"
    assert start_ev["request_id"] == request_id

    # Append a user message
    appended = await agent.append_user_message(request_id, "Follow-up question")
    assert appended is True

    # Let the generator run a few steps and capture events
    found_appended = False
    try:
        async for ev in gen:
            if ev.get("type") == "cancelled":
                break
            # after the planner run, agent._current_messages should include appended user message
            if hasattr(agent, '_current_messages'):
                msgs = agent._current_messages
                for m in msgs:
                    if getattr(m, 'content', None) and 'Follow-up question' in str(m.content):
                        found_appended = True
                        break
            if ev.get("type") == "end":
                break
    except StopAsyncIteration:
        pass

    assert found_appended, "Appended message was not consumed by run_events"
