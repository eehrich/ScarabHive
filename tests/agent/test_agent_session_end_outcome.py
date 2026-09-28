"""session_end: the hooks are told how the run ended.

A hook sees none of the run's events -- telemetry needs the outcome from the
hook itself: ``metadata["cancelled"]``, ``metadata["errors"]`` and
``metadata["completed"]`` next to ``persisted``. Real path: Agent.run_events,
the hook manager and the global registry; only the LLM is scripted.
"""

from __future__ import annotations

import asyncio
from typing import Any

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
from agent_system.core.cancellation import get_cancellation_manager
from agent_system.hooks import HookResult, HookType, PluginHook
from agent_system.hooks.registry import get_hook_registry
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry
from agent_system.tools.status import current_request_id


class _Model:
    model = "test/model"

    def __init__(self, cancel: bool = False):
        self._cancel = cancel

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        if self._cancel:
            # Cancelled while the model answers with a call: the loop notices
            # before the next step.
            self._cancel = False
            get_cancellation_manager().cancel_request(current_request_id.get())
            yield {"type": "final", "assistant": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "nothing", "arguments": "{}"}}]}}
            return
        yield {"type": "final", "assistant": {"role": "assistant", "content": "done"}}


class _Recorder(PluginHook):
    def __init__(self):
        super().__init__("recorder")
        self.metadata: list[dict[str, Any]] = []

    async def on_session_end(self, context):
        self.metadata.append(dict(context.metadata))
        return HookResult(success=True)


@pytest.fixture
async def recorder():
    registry = get_hook_registry()
    hook = _Recorder()
    await registry.register_hook(HookType.SESSION_END, "outcome.recorder", hook)
    yield hook
    await registry.unregister_hook(HookType.SESSION_END, "outcome.recorder")


def _agent() -> Agent:
    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4", api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")}, default_profile="normal")
    agent_config = AgentConfig(max_steps=3, llm_profile="normal", tools=ToolConfig(allowed=[]))
    return Agent("test_agent", AgentSystemConfig(llm_system=llm_system),
                 ToolServerConfig(type="agent", enabled=True, agent_config=agent_config),
                 ToolServerRegistry())


async def _run(agent: Agent, model: _Model) -> list[dict[str, Any]]:
    return [event async for event in agent.run_events("go", session_id="outcome-1", llm_override=model)]


async def test_a_completed_run(recorder):
    await _run(_agent(), _Model())

    [metadata] = recorder.metadata
    assert metadata["completed"] is True
    assert metadata["cancelled"] is False
    assert metadata["errors"] == []
    assert "persisted" in metadata


async def test_a_run_that_crashed_names_its_error(recorder):
    agent = _agent()
    tokens = []

    async def broken_setup():
        tokens.append(get_cancellation_manager().get_token(current_request_id.get()))
        raise ValueError("setup broke")

    agent._tool_integration_manager.setup_tool_integration = broken_setup
    events = await _run(agent, _Model())

    assert any(e.get("type") == "error" for e in events), "fixture: the run did not crash"
    [metadata] = recorder.metadata
    assert metadata["completed"] is False
    assert len(metadata["errors"]) == 1 and "setup broke" in metadata["errors"][0], \
        "the crash never reached the run's errors"
    # The crash cancels the run's token after the error (its tools and
    # background work stop on it) -- read on to the end here, so it did; the
    # hooks must still not be told the run was cancelled.
    [token] = tokens
    assert token is not None and token.is_cancelled, "fixture: the crash did not cancel the run's token"
    assert metadata["cancelled"] is False


async def test_a_cancelled_run(recorder):
    events = await _run(_agent(), _Model(cancel=True))

    assert any(e.get("type") == "cancelled" for e in events), "fixture: the run was not cancelled"
    [metadata] = recorder.metadata
    assert metadata["cancelled"] is True
    assert metadata["errors"] == []


class _BrokenModel(_Model):
    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        raise ValueError("the model is down")
        yield  # pragma: no cover - makes this an async generator


class _EndlessModel(_Model):
    """Asks for a tool call on every step: the loop ends at max_steps with an
    error event of its own."""

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        yield {"type": "final", "assistant": {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{len(messages)}", "type": "function",
             "function": {"name": "nothing", "arguments": "{}"}}]}}


@pytest.mark.parametrize("model", [_BrokenModel, _EndlessModel], ids=["crash", "loop-error-event"])
async def test_an_error_the_consumer_stopped_at_still_counts(recorder, model):
    """sub_agent_manager and stategraph stop reading at the first error event:
    the error must be on record before it is handed out, not after."""
    agent = _agent()
    stream = agent.run_events("go", session_id="outcome-2", llm_override=model())
    seen = []
    async for event in stream:
        seen.append(event)
        if event.get("type") == "error":
            break
    await stream.aclose()
    # run_events leaves its inner generator to the loop's async-generator
    # finalizer: the run's finalize (and its session_end) follows shortly.
    for _ in range(200):
        if recorder.metadata:
            break
        await asyncio.sleep(0.01)

    assert seen[-1].get("type") == "error", "fixture: the run reported no error"
    [metadata] = recorder.metadata
    assert metadata["errors"] == [seen[-1]["message"]]
    assert metadata["completed"] is False
    assert metadata["cancelled"] is False


async def test_a_run_without_an_llm_names_its_error(recorder):
    agent = _agent()
    agent.llm = None

    events = [event async for event in agent.run_events("go", session_id="outcome-3")]

    [error] = [e for e in events if e.get("type") == "error"]
    [metadata] = recorder.metadata
    assert metadata["errors"] == [error["message"]]
