"""A reader that stops reading a run closes the LLM's stream at once.

Starlette never closes a streaming response's generator when its client goes
away (app.sse_response closes it explicitly), and closing the run's generator
closes only that one frame: every generator it delegates to that is not closed
with it is left to the asyncgen finalizer, which runs it on later loop
iterations -- the provider's stream, and the request it holds, stay open
meanwhile. Each level from run_events down to the client's stream closes the
one below it (contextlib.aclosing)."""

import asyncio
from unittest.mock import AsyncMock

from agent_system.config.models import (
    AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig, ToolServerConfig,
)
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry


def _agent():
    llm_system = LLMSystemConfig(
        models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="m")}, default_profile="normal")
    config = ToolServerConfig(type="agent", enabled=True, agent_config=AgentConfig(max_steps=1))
    return Agent("test_agent", AgentSystemConfig(llm_system=llm_system), config, ToolServerRegistry())


async def test_a_reader_that_stops_closes_the_llm_stream_before_the_loop_turns_again():
    closed = []

    async def stream(messages, tools, cancellation_token=None, status_scope=None):
        try:
            for i in range(10_000):
                yield {"type": "thinking_delta", "delta": f"thought {i} "}
                await asyncio.sleep(0)
        finally:
            closed.append(True)

    llm = AsyncMock()
    llm.supports_streaming = lambda: True
    llm.chat_tools_streaming = stream
    agent = _agent()
    agent.llm = llm

    run = agent.run_events("do it", session_id="s1")
    async for event in run:
        if event.get("type") == "reasoning_delta":
            break
    await run.aclose()

    assert closed == [True], "the LLM stream outlived the run its reader closed"
