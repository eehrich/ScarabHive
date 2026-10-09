"""The step budget reaches every LLM hook: HookContext.max_steps and final_call.

A post_llm_call ``continue`` on the final call is dropped by the loop, so a hook
that records its nudges reads final_call first. Each hook gets a copy of the
context, and a hook that rebuilds the context field by field (context_engineer,
context_summarizer) names neither field -- both must still reach the next hook.
"""
import pytest

from agent_system.hooks import HookContext, HookResult, HookType, PluginHook
from agent_system.hooks.registry import HookRegistry


class _Rebuilds(PluginHook):
    async def on_pre_llm_call(self, context):
        rebuilt = HookContext(hook_type=context.hook_type, request_id=context.request_id,
                              session_id=context.session_id, messages=context.messages, step=context.step)
        return HookResult(success=True, modified=True, context=rebuilt)


class _Records(PluginHook):
    def __init__(self):
        super().__init__({})
        self.seen = []

    async def on_pre_llm_call(self, context):
        self.seen.append((context.step, context.max_steps, context.final_call))
        return HookResult(success=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("step,final_call", [(1, False), (3, True)])
async def test_the_budget_survives_the_copy_and_a_rebuilding_hook(step, final_call):
    registry = HookRegistry()
    first, last = _Records(), _Records()
    await registry.register_hook(HookType.PRE_LLM_CALL, "first", first)
    await registry.register_hook(HookType.PRE_LLM_CALL, "rebuilds", _Rebuilds({}), order_spec={"after": ["first"]})
    await registry.register_hook(HookType.PRE_LLM_CALL, "last", last, order_spec={"after": ["rebuilds"]})

    context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id="r", session_id="s", messages=[],
                          step=step, max_steps=3, final_call=final_call)
    await registry.execute_hooks(HookType.PRE_LLM_CALL, context)

    assert first.seen == last.seen == [(step, 3, final_call)], (first.seen, last.seen)
