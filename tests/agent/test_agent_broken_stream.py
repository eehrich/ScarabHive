"""A text answer whose stream broke off without any end signal is asked again, not delivered.

incomplete_stream means no [DONE] and no terminal event (llm_openai_compat): the text is a fragment. It used to be
delivered as the reply, cut off mid-sentence, with only a warning in the log. The step is asked again once in a row,
the fragment taken out of the history; a second break in a row is delivered as before. (Idea from opencode, which
continues a broken stream; asking again keeps a text answer whole.)
"""
import pytest

from agent_system.hooks import HookContext, HookResult, HookType, PluginHook
from agent_system.hooks.registry import get_hook_registry

from test_agent_output_cap_note import _agent, _run, _scripted


def _finals(events):
    return [e for e in events if e.get("type") == "final"]


@pytest.mark.asyncio
async def test_a_broken_off_answer_is_asked_again_and_the_fragment_leaves_the_history():
    agent = _agent(max_steps=4, output_cap_notes=0)
    agent.llm, seen = _scripted([("Die Antwort lautet: zw", "incomplete_stream"),
                                 ("Die Antwort lautet: zwei.", "stop")])

    events = await _run(agent)

    assert len(seen) == 2, "the fragment was delivered as the answer"
    assert not any(m.role == "assistant" and "lautet: zw" in str(m.content) for m in seen[1]), \
        "the second request still carries the fragment"
    assert _finals(events) and _finals(events)[-1]["summary"].endswith("zwei.")


@pytest.mark.asyncio
async def test_a_break_after_a_step_that_ended_on_its_own_is_asked_again_too():
    """'In a row' means answers: a tool step in between starts the count again."""
    agent = _agent(max_steps=8, output_cap_notes=0)
    agent.llm, seen = _scripted([("Erst", "incomplete_stream"), ("", "tool"),
                                 ("Dann", "incomplete_stream"), ("Fertig.", "stop")])

    events = await _run(agent)

    assert len(seen) == 4, f"{len(seen)} calls: the second break was delivered"
    assert _finals(events)[-1]["summary"].endswith("Fertig.")


@pytest.mark.asyncio
async def test_a_second_break_in_a_row_is_delivered():
    agent = _agent(max_steps=6, output_cap_notes=0)
    agent.llm, seen = _scripted([("Teil eins", "incomplete_stream")])

    events = await _run(agent)

    assert len(seen) == 2, f"{len(seen)} calls: one retry, then the fragment stands"
    assert _finals(events)


@pytest.mark.asyncio
async def test_the_session_tracker_drops_the_fragment_before_the_step_is_asked_again():
    """The tracker took the fragment with the answer; its checkpoint wrote it to disk during the retry."""
    agent = _agent(max_steps=4, output_cap_notes=0)
    agent.llm, seen = _scripted([("Die Antwort lautet: zw", "incomplete_stream"),
                                 ("Die Antwort lautet: zwei.", "stop")])
    tracked = []
    stream = agent.llm.chat_tools_streaming

    async def watching(messages, tools, **kwargs):
        if seen:
            tracked.append([str(m.content) for m in agent._session_tracker.get_session_messages("cap")])
        async for chunk in stream(messages, tools, **kwargs):
            yield chunk

    agent.llm.chat_tools_streaming = watching
    await _run(agent)

    assert len(seen) == 2 and tracked, "fixture: no retry"
    assert "Die Antwort lautet: zw" not in tracked[0], tracked[0]


@pytest.mark.asyncio
async def test_post_llm_hooks_are_told_and_their_continue_keeps_no_fragment():
    """A judging hook (agent_continuation) said "continue" to the fragment: it stayed in the history as an answer."""
    agent = _agent(max_steps=4, output_cap_notes=0)
    agent.llm, seen = _scripted([("Die Antwort lautet: zw", "incomplete_stream"),
                                 ("Die Antwort lautet: zwei.", "stop")])
    told = []

    async def hooks(**kwargs):
        told.append(kwargs["asked_again"])
        if len(told) == 1:
            return kwargs["llm_response"], {"continue": True, "continue_message": "Continue."}
        return kwargs["llm_response"], {}

    agent._hook_manager.execute_post_llm_hooks = hooks
    events = await _run(agent)

    assert told == [True, False]
    assert len(seen) == 2
    assert not any("lautet: zw" in str(m.content) and not str(m.content).endswith("zwei.") for m in seen[1]),         "the fragment stayed in the history"
    assert _finals(events)[-1]["summary"].endswith("zwei.")


class _Rebuilds(PluginHook):
    """A hook that hands on a context of its own, the way context_engineer rebuilds it field by field."""

    def __init__(self):
        super().__init__({})

    async def on_post_llm_call(self, context):
        rebuilt = HookContext(hook_type=context.hook_type, request_id=context.request_id,
                              session_id=context.session_id, messages=context.messages,
                              llm_response=context.llm_response, step=context.step)
        return HookResult(success=True, modified=True, context=rebuilt)


class _Records(PluginHook):
    def __init__(self):
        super().__init__({})
        self.told = []

    async def on_post_llm_call(self, context):
        self.told.append(context.asked_again)
        return HookResult(success=True)


@pytest.mark.asyncio
async def test_a_registered_hook_is_told_even_behind_one_that_rebuilds_the_context():
    registry = get_hook_registry()
    recorder = _Records()
    hooks = [(HookType.POST_LLM_CALL, "test.broken.rebuilds", _Rebuilds()),
             (HookType.POST_LLM_CALL, "test.broken.records", recorder)]
    for hook_type, name, hook in hooks:
        await registry.register_hook(hook_type, name, hook)
    try:
        agent = _agent(max_steps=4, output_cap_notes=0)
        agent.llm, seen = _scripted([("Die Antwort lautet: zw", "incomplete_stream"),
                                     ("Die Antwort lautet: zwei.", "stop")])
        await _run(agent)
    finally:
        for hook_type, name, _ in hooks:
            await registry.unregister_hook(hook_type, name)

    assert len(seen) == 2, "fixture: no retry"
    assert recorder.told == [True, False], recorder.told


@pytest.mark.asyncio
async def test_a_broken_off_tool_call_runs_and_starts_the_count_again():
    """A tool call that broke off runs as it is -- it is no fragment to ask again -- and is an answer of its own
    between two broken texts: the second text is asked again too."""
    agent = _agent(max_steps=8, output_cap_notes=0)
    agent.llm, seen = _scripted([("Erst", "incomplete_stream"), ("Erst ganz.", "tool"),
                                 ("Dann", "incomplete_stream"), ("Fertig.", "stop")])
    stream = agent.llm.chat_tools_streaming

    async def tool_call_breaks_off(messages, tools, **kwargs):
        async for chunk in stream(messages, tools, **kwargs):
            if chunk.get("finish_reason") == "tool_calls":
                chunk = {**chunk, "finish_reason": "incomplete_stream"}
            yield chunk

    agent.llm.chat_tools_streaming = tool_call_breaks_off
    events = await _run(agent)

    assert len(seen) == 4, f"{len(seen)} calls: the second broken text was delivered"
    assert any(m.role == "assistant" and m.tool_calls for m in seen[2]), "the broken-off tool call was dropped"
    assert _finals(events)[-1]["summary"].endswith("Fertig.")


@pytest.mark.asyncio
async def test_hooks_judge_a_broken_off_tool_call_as_the_answer_it_is():
    agent = _agent(max_steps=4, output_cap_notes=0)
    agent.llm, seen = _scripted([("Ich lese die Datei.", "tool"), ("Fertig.", "stop")])
    stream = agent.llm.chat_tools_streaming

    async def tool_call_breaks_off(messages, tools, **kwargs):
        async for chunk in stream(messages, tools, **kwargs):
            if chunk.get("finish_reason") == "tool_calls":
                chunk = {**chunk, "finish_reason": "incomplete_stream"}
            yield chunk

    agent.llm.chat_tools_streaming = tool_call_breaks_off
    told = []

    async def hooks(**kwargs):
        told.append(kwargs["asked_again"])
        return kwargs["llm_response"], {}

    agent._hook_manager.execute_post_llm_hooks = hooks
    await _run(agent)

    assert told == [False, False], told
