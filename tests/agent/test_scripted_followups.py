"""Scripted follow-ups through the real run loop.

The plugin decides from the history; the loop has to write the marker the
plugin counts by, and the caller has to get the LAST final answer. Only the
whole path shows both.
"""

import pytest

from agent_system.hooks import HookType, PluginHook
from agent_system.hooks.registry import get_hook_registry
from plugins.agent_continuation.hooks import FOLLOWUP_MARKER, AgentContinuationPlugin
from plugins import agent_continuation

from test_reasoning_loop_wiring import _real_agent

FOLLOWUPS = ["Review your result once more.", "Now return the complete result."]


class _AnswersInTurn:
    """Answers each call with the next text, no tools."""

    model = "test/model"

    def __init__(self):
        self.calls = 0
        self.seen = []

    def supports_streaming(self) -> bool:
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None,
                                   status_scope=None):
        self.calls += 1
        self.seen.append([(m.role, m.content, m.injected_by) for m in messages
                          if m.role == "user"])
        yield {"type": "final", "assistant": {"role": "assistant",
                                              "content": f"answer {self.calls}"}}


class _WithFollowups(PluginHook):
    """The real plugin, with the config an agent would carry in hooks.overrides."""

    def __init__(self):
        super().__init__({})
        from pathlib import Path
        self.plugin = AgentContinuationPlugin(Path(agent_continuation.__file__).parent)

    async def on_post_llm_call(self, context):
        context.hook_config = {"followups": FOLLOWUPS}
        return await self.plugin.evaluate_completion(context)


@pytest.mark.asyncio
async def test_each_followup_is_sent_once_and_the_last_answer_is_the_result():
    registry = get_hook_registry()
    await registry.register_hook(HookType.POST_LLM_CALL, "test_followups.evaluate",
                                 _WithFollowups())
    try:
        agent = _real_agent()
        agent.agent_config.max_steps = 10
        llm = _AnswersInTurn()
        agent.llm = llm
        events = [event async for event in agent.run_events("Score chapter 3.", session_id="f1")]
    finally:
        await registry.unregister_hook(HookType.POST_LLM_CALL, "test_followups.evaluate")

    assert llm.calls == 3, f"expected answer + 2 follow-up rounds, got {llm.calls} calls"
    last_users = llm.seen[-1]
    assert [content for _, content, _ in last_users] == ["Score chapter 3.", *FOLLOWUPS]
    assert [marker for _, _, marker in last_users] == [None, FOLLOWUP_MARKER, FOLLOWUP_MARKER]
    # A scripted follow-up stays a user turn: the plugin counts what it sent by
    # the marker, and v4 reads the follow-up back out of the stored transcript
    # by its configured text. Only the marker tells it from a person.
    assert [role for role, _, _ in last_users] == ["user", "user", "user"]
    finals = [e for e in events if e.get("type") == "final"]
    assert finals and "answer 3" in str(finals[-1].get("summary"))
