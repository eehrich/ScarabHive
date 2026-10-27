"""current_run_session: an agent run holds its session in the context while it runs, and lets go of it after.

A model call that is a run of its own (a coding CLI as an LLM) hands the session to that run's tools; the session
must be the run's own, not the last one some earlier run left behind.
"""
from __future__ import annotations

from typing import Any

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolServerConfig,
)
from agent_system.core.request_context import current_run_session
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry


class _Model:
    """Answers at once; records the session in the context at each call."""

    model = "test/model"

    def __init__(self):
        self.sessions: list[Any] = []

    def supports_streaming(self):
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None, **_):
        self.sessions.append(current_run_session.get())
        return {"assistant": {"role": "assistant", "content": "done"}}


def _agent() -> Agent:
    llm = LLMSystemConfig(models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
                          profiles={"normal": LLMProfile(model_ref="m")}, default_profile="normal")
    return Agent("session_ctx_agent", AgentSystemConfig(llm_system=llm), ToolServerConfig(
        type="agent", enabled=True, agent_config=AgentConfig(max_steps=2, llm_profile="normal")), ToolServerRegistry())


async def test_a_run_holds_its_session_while_the_model_is_asked_and_lets_go_after():
    agent = _agent()
    model = _Model()
    agent.llm = model
    assert current_run_session.get() is None
    [event async for event in agent.run_events("go", session_id="sess-a")]
    [event async for event in agent.run_events("again", session_id="sess-b")]
    assert model.sessions == ["sess-a", "sess-b"]
    assert current_run_session.get() is None, "a run left its session in the context"


async def test_a_run_whose_setup_fails_gives_the_session_back():
    """It fails after setting the session and before it has a context to reset it from: the caller's comes back."""
    agent = _agent()
    agent.llm = None  # the setup stops at "No LLM available"
    token = current_run_session.set("outer")
    try:
        [event async for event in agent.run_events("go", session_id="sess-failed")]
        assert current_run_session.get() == "outer"
    finally:
        current_run_session.reset(token)



async def test_a_run_whose_stream_is_closed_early_gives_the_session_back():
    """A consumer that stops reading before "end" (and closes the stream): the caller's session comes back."""
    agent = _agent()
    model = _Model()
    agent.llm = model
    token = current_run_session.set("outer")
    try:
        events = agent.run_events("go", session_id="sess-early")
        async for _ in events:
            if model.sessions:
                break
        await events.aclose()
        assert model.sessions == ["sess-early"], "the model was not reached: this test would prove nothing"
        assert current_run_session.get() == "outer"
    finally:
        current_run_session.reset(token)


async def test_a_polled_model_call_is_cancelled_when_its_stream_is_closed():
    """The 100 ms polls are shielded; closing the stream (a consumer that leaves, a cancelled run) must not."""
    import asyncio

    from agent_system.servers.agent.mixins.llm_loop.llm_call import _polled_call

    stopped = asyncio.Event()

    class _Slow:
        async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None, **_):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stopped.set()
                raise

    # A heartbeat every 2nd poll: the first poll awaits, so the call has started when the stream is closed (one
    # cancelled before it ran never sees its cancel).
    stream = _polled_call(_Slow(), [], [], None, 0, lambda: [], None, {}, None, 0.2)
    assert (await asyncio.wait_for(stream.__anext__(), 5))["type"] == "heartbeat"
    await stream.aclose()
    await asyncio.wait_for(stopped.wait(), 5)


async def test_a_consumer_that_stops_at_end_without_closing_has_the_session_back():
    """The app and agent_service read up to "end" and never close the stream: "end" itself gives it back."""
    agent = _agent()
    agent.llm = _Model()
    token = current_run_session.set("outer")
    events = agent.run_events("go", session_id="sess-end")
    try:
        async for event in events:
            if event.get("type") == "end":
                break
        assert current_run_session.get() == "outer"
    finally:
        current_run_session.reset(token)
        await events.aclose()


async def test_a_call_cancelled_at_the_polling_limit_finishes_its_cleanup():
    """The max-iterations exit cancels the call itself; it must not be cancelled again in the middle of its cleanup."""
    import asyncio
    from types import SimpleNamespace

    import pytest

    from agent_system.servers.agent.mixins.llm_loop.llm_call import _polled_call

    seen: list[str] = []

    class _Slow:
        async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None, **_):
            try:
                await asyncio.sleep(30)
            finally:
                seen.append("cleanup started")
                await asyncio.sleep(0.3)  # longer than the 0.1 s the exit waits
                seen.append("cleanup done")

    stream = _polled_call(_Slow(), [], [], None, 0, lambda: [], None, {},
                          SimpleNamespace(llm_task_max_iterations=2), 3600)
    with pytest.raises(asyncio.TimeoutError):
        async for _ in stream:
            pass
    await asyncio.sleep(0.5)
    assert seen == ["cleanup started", "cleanup done"]
