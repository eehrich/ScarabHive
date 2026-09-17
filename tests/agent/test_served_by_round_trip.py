"""The server keeps the backend a turn came from, so the next request can pin to it.

The LLM client reports it as ``served_by`` on the assistant dict. If the server
dropped it while building the ChatMessage, every client would see a history
without it, and the OpenRouter provider pin would silently never engage.
"""
from __future__ import annotations

import copy
from unittest.mock import AsyncMock

import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolServerConfig,
)
from agent_system.tools.base import ToolServerRegistry
from agent_system.servers.agent.server import Agent


def _agent():
    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4",
                                        api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")},
        default_profile="normal",
    )
    server_config = ToolServerConfig(type="agent", enabled=True,
                           agent_config=AgentConfig(max_steps=2))
    return Agent("test_agent", AgentSystemConfig(llm_system=llm_system),
                 server_config, ToolServerRegistry())


@pytest.mark.asyncio
async def test_the_next_request_sees_the_backend_of_the_last_turn():
    seen: list = []
    responses = [
        {"assistant": {"role": "assistant", "content": "",
                       "tool_calls": [{"id": "t1", "type": "function",
                                       "function": {"name": "some_tool",
                                                    "arguments": "{}"}}],
                       "served_by": "Google AI Studio"}},
        {"assistant": {"role": "assistant", "content": "done"}},
    ]

    async def chat_tools(messages, tools, **kwargs):
        seen.append([copy.deepcopy(m) for m in messages])
        return responses[min(len(seen) - 1, len(responses) - 1)]

    llm = AsyncMock()
    llm.supports_streaming = lambda: False
    llm.chat_tools = chat_tools
    agent = _agent()
    agent.llm = llm

    [ev async for ev in agent.run_events("do it", session_id="s1")]

    assert len(seen) >= 2, "loop must have reached the second LLM call"
    assistants = [m for m in seen[1] if getattr(m, "role", None) == "assistant"]
    assert assistants, "second request carries no assistant turn -- vacuous test"
    assert assistants[-1].served_by == "Google AI Studio"


@pytest.mark.asyncio
async def test_the_final_answer_after_max_steps_is_kept_once_with_its_backend():
    """max_steps runs out, one more call produces the answer: that message is
    built on its own path and must carry served_by too -- and land once."""
    seen: list = []
    tool_turn = {"assistant": {"role": "assistant", "content": "",
                               "tool_calls": [{"id": "t1", "type": "function",
                                               "function": {"name": "some_tool",
                                                            "arguments": "{}"}}],
                               "served_by": "backend-a"}}
    script = [tool_turn, tool_turn,
              {"assistant": {"role": "assistant", "content": "final words",
                             "served_by": "backend-b"}},
              {"assistant": {"role": "assistant", "content": "ok"}}]

    async def chat_tools(messages, tools, **kwargs):
        seen.append([copy.deepcopy(m) for m in messages])
        return script[min(len(seen) - 1, len(script) - 1)]

    llm = AsyncMock()
    llm.supports_streaming = lambda: False
    llm.chat_tools = chat_tools
    agent = _agent()  # max_steps=2: both steps call a tool, the third call answers
    agent.llm = llm

    [ev async for ev in agent.run_events("do it", session_id="s1")]
    [ev async for ev in agent.run_events("and now?", session_id="s1")]

    assert len(seen) >= 4, "the follow-up request never reached the LLM"
    finals = [m for m in seen[3] if getattr(m, "role", None) == "assistant"
              and m.content == "final words"]
    assert len(finals) == 1
    assert finals[0].served_by == "backend-b"
