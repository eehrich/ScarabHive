"""A continuation nudge from a post_llm_call hook is never a human turn.

Turn counters (context_engineer, OKF, tool_preload) take a user message with
injected_by None for one a person typed.
"""
import pytest

from agent_system.hooks import HookResult, HookType, PluginHook
from test_agent_max_steps_final_call import _LLM, _run


class _ContinuesUnmarked(PluginHook):
    async def on_post_llm_call(self, context):
        return HookResult(success=True, metadata={"continue": True})


@pytest.mark.asyncio
async def test_a_continuation_without_a_marker_is_still_marked():
    llm = _LLM(text_steps=True)
    _, _, stored = await _run(llm, max_steps=3, session="continuation_marker",
                              hooks=[(HookType.POST_LLM_CALL, "test.continues_unmarked",
                                      _ContinuesUnmarked({}))])

    nudges = [m for m in stored if m.role == "user" and m.content == "Continue with your task."]
    assert nudges, "fixture: no continuation nudge was added"
    assert all(m.injected_by for m in nudges), [m.injected_by for m in nudges]
