"""llm_progress: hooks that watch a streaming call while it is still thinking.

Two things are under test. The stream fires the callback on its tick and
survives an observer that fails. And the whole path — run loop, hook manager,
registry, hook — delivers a context a hook can use: request id, thinking so
far, and no messages.
"""

from types import SimpleNamespace

import pytest

from agent_system.hooks import HookResult, HookType, PluginHook
from agent_system.hooks.registry import get_hook_registry
from agent_system.servers.agent.mixins.llm_loop import llm_call
from agent_system.servers.agent.server import Agent

from test_reasoning_loop_wiring import (
    FINAL, _CountingLLM, _ScriptedLLM, _real_agent, _stand_in, healthy_text, thinking)


async def _drive(llm, on_progress):
    return [event async for event in Agent._call_llm_with_streaming(
        _stand_in(enabled=False), llm=llm, messages=[], tools_schema=[],
        cancellation_token=None, step=0, yield_pending_status_fn=lambda: [],
        on_reasoning_progress=on_progress)]


class TestTheStreamTicks:

    @pytest.mark.asyncio
    async def test_fires_every_tick_with_the_thinking_so_far(self):
        text = healthy_text(9_000)
        calls = []

        async def on_progress(so_far, chars, previous):
            calls.append((so_far, chars, previous))

        await _drive(_ScriptedLLM(thinking(text) + [FINAL]), on_progress)

        tick = llm_call._REASONING_PROGRESS_TICK
        assert len(calls) == len(text) // tick
        for so_far, chars, previous in calls:
            assert so_far == text[:chars]
            assert chars - previous >= tick
        assert [c[2] for c in calls[1:]] == [c[1] for c in calls[:-1]], \
            "previous must be the length at the previous tick"

    @pytest.mark.asyncio
    async def test_a_failing_observer_does_not_break_the_call(self):
        async def on_progress(*_):
            raise RuntimeError("observer broke")

        events = await _drive(_ScriptedLLM(thinking(healthy_text(9_000)) + [FINAL]),
                              on_progress)
        assert events[-1]["type"] == "thinking_complete"


class _Recorder(PluginHook):
    def __init__(self):
        super().__init__({})
        self.contexts = []

    async def on_llm_progress(self, context):
        self.contexts.append(context)
        return HookResult(success=True, modified=False)


class TestTheRunLoopReachesTheHook:

    @pytest.mark.asyncio
    async def test_a_registered_hook_sees_request_and_thinking_but_no_messages(self):
        registry = get_hook_registry()
        recorder = _Recorder()
        await registry.register_hook(HookType.LLM_PROGRESS, "test_progress.record", recorder)
        try:
            agent = _real_agent()
            agent.llm = _CountingLLM(loops_for_calls=0)
            agent._reasoning_loop_config = {**agent._reasoning_loop_config, "enabled": False}
            # _CountingLLM thinks 2 000 characters: exactly one tick.
            [event async for event in agent.run_events("do it", session_id="p1")]
        finally:
            await registry.unregister_hook(HookType.LLM_PROGRESS, "test_progress.record")

        assert len(recorder.contexts) == 1, "the hook was not reached"
        context = recorder.contexts[0]
        assert context.request_id and context.session_id == "p1"
        assert context.messages is None
        assert context.reasoning_chars >= 2_000 and context.previous_reasoning_chars == 0
        assert context.reasoning_text and len(context.reasoning_text) == context.reasoning_chars

    @pytest.mark.asyncio
    async def test_the_thinking_is_only_collected_for_an_agent_that_watches(self):
        """A hook registered but off for this agent must cost the stream nothing."""
        registry = get_hook_registry()
        await registry.register_hook(HookType.LLM_PROGRESS, "test_progress.off",
                                     _Recorder(), enabled=False)
        try:
            agent = _real_agent()
            assert agent._hook_manager.wants_llm_progress() is False
            agent._hook_manager._hooks_config = SimpleNamespace(
                enabled=True, overrides={"test_progress.off": {"enabled": True}})
            assert agent._hook_manager.wants_llm_progress() is True
        finally:
            await registry.unregister_hook(HookType.LLM_PROGRESS, "test_progress.off")
