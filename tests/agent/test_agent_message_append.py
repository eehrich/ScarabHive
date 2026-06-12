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


def _build_agent() -> Agent:
    """Build a minimal Agent instance like the original append test does."""
    from agent_system.config.models import MCPConfig

    system_config = load_config("config/config.yaml")
    agent_config = system_config.agent_config if hasattr(system_config, 'agent_config') and system_config.agent_config else None
    if not agent_config:
        from agent_system.config.models import AgentConfig
        agent_config = AgentConfig()

    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    registry = MCPRegistry()
    return Agent("test_agent", system_config, mcp_config, registry)


class NonStreamingDummyLLM(DummyLLM):
    """DummyLLM usable as llm_override (run loop probes supports_streaming)."""

    def supports_streaming(self):
        return False


class MidCallInjectingLLM:
    """Simulates a user message arriving WHILE the final LLM call is in flight.

    Call 1 injects an appended user message before returning a text-only
    answer; without the pre-final drain the loop would finalize and drop it.
    """

    def __init__(self):
        self.calls = 0
        self.agent = None
        self.request_id = None
        self.seen_contents = []

    def supports_streaming(self):
        return False

    async def chat(self, messages, tools_schema=None):
        return {"assistant": {"content": "planner response", "tool_calls": []}, "usage": {"total_tokens": 10}}

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.calls += 1
        self.seen_contents.append([str(getattr(m, 'content', '') or '') for m in messages])
        if self.calls == 1:
            appended = await self.agent.append_user_message(self.request_id, "Mid-call follow-up")
            assert appended is True
            return {"assistant": {"content": "interim answer"}, "usage": {"total_tokens": 5}}
        return {"assistant": {"content": "real final answer"}, "usage": {"total_tokens": 5}}


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


@pytest.mark.asyncio
async def test_append_during_final_llm_call_continues_loop(tmp_path):
    """A message injected while the LLM produces a text-only ('final') answer
    must NOT be dropped: the loop has to continue and let the next LLM call
    react to it."""
    agent = _build_agent()
    llm = MidCallInjectingLLM()

    request_id = short_id()
    llm.agent = agent
    llm.request_id = request_id

    final_summary = None
    async for ev in agent.run_events("Initial task", request_id=request_id, llm_override=llm):
        if ev.get("type") == "final":
            final_summary = ev.get("summary")
        if ev.get("type") == "end":
            break

    assert llm.calls == 2, "Loop must continue after mid-call injection (second LLM call expected)"
    assert final_summary is not None and "real final answer" in str(final_summary)

    # The second LLM call must see both the interim answer and the injected message
    second_call_contents = llm.seen_contents[1]
    assert any("Mid-call follow-up" in c for c in second_call_contents), \
        "Injected user message not visible to the follow-up LLM call"
    assert any("interim answer" in c for c in second_call_contents), \
        "Interim assistant answer missing from conversation"


@pytest.mark.asyncio
async def test_append_after_final_is_flushed_to_session(tmp_path):
    """A message that arrives after the final event (too late to be answered)
    must still be persisted into the session instead of silently dropped."""
    agent = _build_agent()

    request_id = short_id()
    gen = agent.run_events("Initial task", request_id=request_id, llm_override=NonStreamingDummyLLM())

    session_id = None
    async for ev in gen:
        if ev.get("type") == "start":
            session_id = ev.get("session_id")
        if ev.get("type") == "final":
            break

    assert session_id is not None

    # Generator is suspended right after yielding 'final': the request entry is
    # still registered, so the append succeeds — but the loop will not run again.
    appended = await agent.append_user_message(request_id, "Too late message")
    assert appended is True

    # Drain the rest of the generator (runs _finalize_request)
    async for ev in gen:
        pass

    msgs = agent._session_tracker.get_session_messages(session_id)
    assert any("Too late message" in str(getattr(m, 'content', '') or '') for m in msgs), \
        "Late appended message was not flushed into the persisted session"
