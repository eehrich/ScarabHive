"""A client that retries from scratch after deltas went out says so with
`stream_restart`: the call's buffers start over and the chat is told to empty
the step's thinking -- else the retried reasoning was shown twice and fed the
loop detectors and llm_progress hooks twice."""

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


def _llm():
    async def stream(messages, tools, cancellation_token=None, status_scope=None):
        yield {"type": "thinking_delta", "delta": "first try"}
        yield {"type": "content_delta", "delta": "hel", "accumulated": "hel"}
        yield {"type": "stream_restart"}
        yield {"type": "thinking_delta", "delta": "second try"}
        yield {"type": "content_delta", "delta": "hello", "accumulated": "hello"}
        yield {"type": "final", "finish_reason": "stop", "usage": {},
               "assistant": {"role": "assistant", "content": "hello"}}

    llm = AsyncMock()
    llm.supports_streaming = lambda: True
    llm.chat_tools_streaming = stream
    return llm


async def test_a_restart_empties_the_steps_thinking_and_the_call_starts_over():
    agent = _agent()
    agent.llm = _llm()
    events = [e async for e in agent.run_events("do it", session_id="s1")]
    kinds = [e.get("type") for e in events if e.get("type") in ("reasoning_delta", "reasoning_reset")]
    assert kinds == ["reasoning_delta", "reasoning_reset", "reasoning_delta"]
    reset = next(e for e in events if e.get("type") == "reasoning_reset")
    assert reset["step"] == 1


async def test_the_loop_windows_and_progress_buffers_start_over():
    """Driven through the streaming call itself: what the call keeps of its
    thinking after a restart is the second attempt's only."""
    agent = _agent()
    ticks = []

    async def on_progress(text, chars, previous):
        ticks.append(text)

    import agent_system.servers.agent.mixins.llm_loop as llm_loop
    old_tick = llm_loop._REASONING_PROGRESS_TICK
    llm_loop._REASONING_PROGRESS_TICK = 1
    try:
        events = [e async for e in agent._call_llm_with_streaming(
            _llm(), [], [], cancellation_token=None, step=0,
            yield_pending_status_fn=lambda: [], on_reasoning_progress=on_progress)]
    finally:
        llm_loop._REASONING_PROGRESS_TICK = old_tick
    assert ticks[-1] == "second try"
    assert any(e.get("type") == "thinking_complete" for e in events)
