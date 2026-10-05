"""A run connects its on_demand external servers BEFORE it lists its tools.

The connect (config/mcp_servers.yaml ``connect: on_demand``) is one line in
the run path; after the listing, the run that needs the server would go
without its tools.
"""
import pytest

from test_reasoning_loop_wiring import _real_agent


@pytest.mark.asyncio
async def test_the_run_connects_before_it_lists_its_tools(monkeypatch):
    agent = _real_agent()
    agent.agent_config.max_steps = 1
    calls = []

    async def no_setup():
        return None

    async def connect():
        calls.append("connect")

    async def usable():
        calls.append("list")
        return [], None, None

    monkeypatch.setattr(agent._tool_integration_manager, "setup_tool_integration", no_setup)
    monkeypatch.setattr(agent._tool_integration_manager, "connect_on_demand_servers", connect)
    monkeypatch.setattr(agent, "list_usable_tools", usable)

    class _Done:
        model = "test/model"

        def supports_streaming(self):
            return True

        async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
            yield {"type": "final", "assistant": {"role": "assistant", "content": "done"}}

    agent.llm = _Done()
    [event async for event in agent.run_events("the task", session_id="on_demand_order")]

    assert "list" in calls, "fixture: the run never listed its tools"
    assert calls[0] == "connect", calls
