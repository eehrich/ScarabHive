"""A session keeps the run's notes the model answered, where they stood.

The next request of a session must start with the prefix the last one sent,
or the provider's cache reads nothing back. Dropping every marked developer
note on save broke that prefix at the first note on every request. Driven
through a real Agent run (step loop, notes, persistence); only the LLM is
scripted and the tool server is a stub.
"""
import json

import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolConfig,
    ToolServerConfig,
)
from agent_system.llm.message_roles import DEVELOPER
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry


class _ToolDef:
    name = "kit_read"
    description = "Read a file."
    input_schema = {"type": "object", "properties": {}}


class _Server:
    async def list_tools(self):
        return [_ToolDef()]

    async def call(self, name, params):
        return {"ok": True}


class _LLM:
    """Calls the tool `calls` times, then answers; records every request."""

    model = "test/model"

    def __init__(self, calls):
        self.calls = calls
        self.seen = []

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.seen.append([(m.role, m.content, getattr(m, "injected_by", None)) for m in messages])
        n = len(self.seen)
        if n <= self.calls:
            answer = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{n}", "type": "function", "function": {"name": "kit_read", "arguments": json.dumps({})}}]}
        else:
            answer = {"role": "assistant", "content": f"done {n}"}
        yield {"type": "final", "assistant": answer}


def _agent():
    llm_system = LLMSystemConfig(models={"m": LLMModelConfig(provider="openai", model="m", api_key="k")},
                                 profiles={"normal": LLMProfile(model_ref="m")}, default_profile="normal")
    registry = ToolServerRegistry()
    registry.register("kit", _Server())
    config = AgentConfig(max_steps=5, llm_profile="normal", tools=ToolConfig(allowed=["kit/*"]))
    return Agent("test_agent", AgentSystemConfig(llm_system=llm_system),
                 ToolServerConfig(type="agent", enabled=True, agent_config=config), registry)


async def _run(agent, llm, task, n):
    agent.llm = llm
    return [e async for e in agent.run_events(task, request_id=f"req_notes_{n}", session_id="s_notes")]


@pytest.mark.asyncio
async def test_the_next_request_starts_with_the_prefix_the_last_one_sent():
    agent = _agent()
    first = _LLM(calls=4)
    await _run(agent, first, "go", 1)
    last = first.seen[-1]
    assert any(m[0] == DEVELOPER and m[2] for m in last), "fixture: the run wrote no note"

    second = _LLM(calls=0)
    await _run(agent, second, "again", 2)
    opened = second.seen[0]
    assert opened[:len(last)] == last, "the stored history broke the cached prefix"
    assert opened[len(last)][1] == "done 5", "the answer to the last request is not next"
