"""The step count reaches the model through the history, not the system prompt.

The system prompt is the cached prefix and is re-rendered before every step; a
"Current step: 3/30" there re-billed the whole conversation on every call.
"""
import pytest

from agent_system.servers.agent.server import Agent


def test_no_note_before_the_last_steps():
    for step in range(28):
        assert Agent._step_budget_note(step, 30) is None


def test_the_last_two_steps_get_a_note():
    second_to_last = Agent._step_budget_note(28, 30)
    last = Agent._step_budget_note(29, 30)

    assert "Step 29 of 30" in second_to_last.content and "1 step left" in second_to_last.content
    assert "step 30 of 30, the last one" in last.content
    assert "closing tool call" in last.content, "an agent that delivers through a tool is told to answer in prose"
    assert second_to_last.role == last.role == "user"


def test_single_call_agents_get_no_note():
    for max_steps in (1, 2, 3, 4):
        for step in range(max_steps):
            assert Agent._step_budget_note(step, max_steps) is None


@pytest.mark.asyncio
async def test_every_request_is_a_prefix_of_the_next():
    """Five steps through the real loop: follow-ups keep it going without tools.

    Anthropic caches at the last message of a request. A note sent only at the
    tail of one call is missing from the next call's prefix, and that call
    reads nothing back."""
    import inspect
    from pathlib import Path

    from agent_system.hooks import HookType, PluginHook
    from agent_system.hooks.registry import get_hook_registry
    from plugins.agent_continuation.hooks import AgentContinuationPlugin
    from test_reasoning_loop_wiring import _real_agent

    class _Followups(PluginHook):
        def __init__(self):
            super().__init__({})
            # The module file, not the package's: imported as a namespace
            # package in some test orders, plugins.agent_continuation has no __file__.
            self.plugin = AgentContinuationPlugin(Path(inspect.getfile(AgentContinuationPlugin)).parent)

        async def on_post_llm_call(self, context):
            context.hook_config = {"followups": [f"go on {i}" for i in range(4)]}
            return await self.plugin.evaluate_completion(context)

    class _Records:
        model = "test/model"

        def __init__(self):
            self.seen = []

        def supports_streaming(self):
            return True

        async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
            self.seen.append([(m.role, str(m.content)) for m in messages])
            yield {"type": "final", "assistant": {"role": "assistant", "content": f"answer {len(self.seen)}"}}

    registry = get_hook_registry()
    await registry.register_hook(HookType.POST_LLM_CALL, "test_step_budget.followups", _Followups())
    try:
        agent = _real_agent()
        agent.agent_config.max_steps = 5
        llm = _Records()
        agent.llm = llm
        [event async for event in agent.run_events("the task", session_id="budget")]
    finally:
        await registry.unregister_hook(HookType.POST_LLM_CALL, "test_step_budget.followups")

    assert len(llm.seen) == 5, f"fixture: {len(llm.seen)} calls"
    for i, (request, following) in enumerate(zip(llm.seen, llm.seen[1:])):
        assert following[:len(request)] == request, f"call {i + 2} does not start with call {i + 1}"
    notes = [[text for role, text in request if role == "user" and ("Step " in text or "the last one" in text)]
             for request in llm.seen]
    assert notes[:3] == [[], [], []], "a note before the last two steps"
    assert len(notes[3]) == 1 and "Step 4 of 5" in notes[3][0], llm.seen[3]
    assert len(notes[4]) == 2 and "the last one" in notes[4][1], llm.seen[4]
    assert llm.seen[4][-1] == ("user", notes[4][1]), "the last note is not what the model reads last"
    followups = [text for role, text in llm.seen[4] if text.startswith("go on")]
    assert followups == ["go on 0", "go on 1", "go on 2", "go on 3"], (
        "the follow-ups restarted: the note was taken for a message a person wrote")


@pytest.mark.asyncio
async def test_a_run_that_hits_the_cap_marks_what_the_loop_adds():
    """A model that answers empty twice, then keeps calling the same failing tool:
    the "continue" nudge, loop interventions, the step notes and the max-steps
    request all join the history as role user. Unmarked, every hook looking for
    what a person wrote took them for it.

    The max-steps request is a step of its own: its call starts with the one
    before it and sends the same tools -- a tool list that changes on the last
    call breaks the provider cache on the longest request of the run."""
    from test_reasoning_loop_wiring import _real_agent

    class _Stuck:
        model = "test/model"

        def __init__(self):
            self.seen = []
            self.tools = []

        def supports_streaming(self):
            return True

        async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
            self.seen.append([(m.role, str(m.content), m.injected_by) for m in messages])
            self.tools.append(tools)
            if messages[-1].injected_by == "agent.max_steps":
                yield {"type": "final", "assistant": {"role": "assistant", "content": "what I have"}}
                return
            if len(self.seen) <= 2:
                yield {"type": "final", "assistant": {"role": "assistant", "content": None}}
                return
            call = {"id": f"call_{len(self.seen)}", "type": "function",
                    "function": {"name": "missing_tool", "arguments": "{}"}}
            yield {"type": "final", "assistant": {"role": "assistant", "content": None, "tool_calls": [call]}}

        async def chat_tools(self, messages, tools, cancellation_token=None):
            raise AssertionError("the max-steps request went around the step loop")

    agent = _real_agent()
    agent.agent_config.max_steps = 7
    llm = _Stuck()
    agent.llm = llm
    schema = [{"type": "function", "function": {"name": "missing_tool", "parameters": {"type": "object"}}}]
    initialize = agent._initialize_request_and_conversation

    async def with_a_tool(**kwargs):
        context = await initialize(**kwargs)
        context.tools_schema = schema
        return context
    agent._initialize_request_and_conversation = with_a_tool
    events = [event async for event in agent.run_events("the task", session_id="stuck")]

    assert len(llm.seen) == 8, f"fixture: {len(llm.seen)} calls, the max-steps request never went out"
    for i, (request, following) in enumerate(zip(llm.seen, llm.seen[1:])):
        assert following[:len(request)] == request, f"call {i + 2} does not start with call {i + 1}"
    assert all(tools == schema for tools in llm.tools), f"the tools changed between calls: {llm.tools}"
    assert [e.get("summary") for e in events if e.get("type") == "final"] == ["what I have"], (
        [e for e in events if e.get("type") in ("final", "error")])
    users = [(text, marker) for role, text, marker in llm.seen[-1] if role == "user"]
    assert users[0] == ("the task", None)
    markers = [marker for _, marker in users[1:]]
    assert "agent.loop_intervention" in markers, f"fixture: no loop intervention in {users}"
    assert markers[0] == "agent.empty_response", f"fixture: no continue nudge first in {users}"
    assert markers.count("agent.step_budget") == 2 and markers[-1] == "agent.max_steps", markers
    assert None not in markers, f"an unmarked message the loop added: {users}"

