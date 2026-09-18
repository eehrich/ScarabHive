"""The call after the step budget is a step of its own.

It used to be a bare chat_tools() after the loop: no pre-LLM hooks (the
message_validator left an orphaned tool call in the request, context_engineer
capped nothing), no fallback chain (a 503 threw away the whole run), no
post-LLM hooks. Now it runs through the same machinery as every step; what
differs is that no step follows it. Tool calls it makes run once, and the run
ends without another model call, saying the budget ran out.
"""
import pytest

from agent_system.hooks import HookResult, HookType, PluginHook
from agent_system.hooks.registry import get_hook_registry
from agent_system.llm.message_roles import DEVELOPER
from agent_system.llm.models import ChatMessage, LLMServerError
from agent_system.servers.agent.server import Agent
from plugins.message_validator.hooks import InternalMessageValidator
from test_reasoning_loop_wiring import _real_agent

MAX_STEPS_REQUEST = "agent.max_steps"
REQUEST_ID = "final_call_request"


def _tool_call(n, args):
    return {"id": f"call_{n}", "type": "function",
            "function": {"name": "missing_tool", "arguments": args}}


class _LLM:
    """Streams one answer per call. By default: a new tool call per step and a
    text answer to the max-steps request."""

    model = "test/model"

    def __init__(self, *, same_args=False, same_args_from=None, tools_on_final=False,
                 text_on_final="summary of what I have", text_steps=False, empty_from_call=None,
                 blank_calls=(), server_error_on=None, cancel_on=None, append_on_final=None):
        self.seen = []
        self.agent = None  # set by _run
        self._same_args = same_args
        self._same_args_from = same_args_from
        self._tools_on_final = tools_on_final
        self._text_on_final = text_on_final
        self._text_steps = text_steps
        self._empty_from_call = empty_from_call
        self._blank_calls = blank_calls
        self._server_error_on = server_error_on
        self._cancel_on = cancel_on
        self._append_on_final = append_on_final

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.seen.append(list(messages))
        n = len(self.seen)
        if n == self._server_error_on:
            raise LLMServerError("boom", status_code=503)
        final = messages[-1].injected_by == MAX_STEPS_REQUEST
        if n == self._cancel_on:
            cancellation_token.cancel()
        if final and self._append_on_final:
            assert await self.agent.append_user_message(REQUEST_ID, self._append_on_final), "fixture: not appended"
        if self._empty_from_call is not None and n >= self._empty_from_call:
            answer = {"role": "assistant", "content": None}
        elif n in self._blank_calls:
            answer = {"role": "assistant", "content": "  "}
        elif final and not self._tools_on_final:
            answer = {"role": "assistant", "content": self._text_on_final}
        elif self._text_steps:
            answer = {"role": "assistant", "content": f"interim {n}"}
        else:
            same = self._same_args or (self._same_args_from is not None and n >= self._same_args_from)
            args = "{}" if same else '{"n": %d}' % n
            answer = {"role": "assistant", "content": self._text_on_final if final else None,
                      "tool_calls": [_tool_call(n, args)]}
        yield {"type": "final", "assistant": answer}

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        raise AssertionError("the final call went around the step loop")


class _RecordsPreLLM(PluginHook):
    def __init__(self, validate=False):
        super().__init__({})
        self.runs = []
        self._validate = validate

    async def on_pre_llm_call(self, context):
        self.runs.append((context.step, context.messages[-1].injected_by))
        if not self._validate:
            return HookResult(success=True)
        context.messages = InternalMessageValidator().validate_and_repair(
            list(context.messages), "test").repaired_messages
        return HookResult(success=True, modified=True, context=context)


class _AppendsABlock(PluginHook):
    """A plugin that appends its block at the END of the list.

    That is how the hook plugins write since they stopped inserting into the
    leading system run -- and it puts them behind everything the loop itself
    had added."""

    MARKER = "test.todo"

    def __init__(self):
        super().__init__({})

    async def on_pre_llm_call(self, context):
        context.messages = list(context.messages) + [
            ChatMessage(role=DEVELOPER, content="## Todo\n- [ ] still open",
                        injected_by=self.MARKER)]
        return HookResult(success=True, modified=True, context=context)


class _Continues(PluginHook):
    async def on_post_llm_call(self, context):
        return HookResult(success=True, metadata={"continue": True, "continue_injected_by": "test.continue"})


#: What the last _run() finished with, notes and all. The session keeps no
#: volatile developer note, so "what the loop added after the final call" is
#: only measurable here -- ``stored`` answers the other question, what a resume
#: of that session would read back.
_LAST_LIVE: list = []


def _notes(messages):
    """The loop's own notes in a REQUEST, in order."""
    return [m for m in messages if m.role == DEVELOPER]


def _loop_notes(messages):
    """What the loop added, found by its MARKER.

    Against the session a role filter is worthless: the tracker drops exactly
    `developer` + marker, so it comes back empty whatever the loop does --
    including when a site goes back to signing its notes `user` and they are
    persisted again. The marker outlives that."""
    return [m for m in messages if (m.injected_by or "").startswith("agent.")]


def _orphans(messages):
    called = {tc["id"] for m in messages if m.role == "assistant" for tc in (m.tool_calls or [])}
    answered = {m.tool_call_id for m in messages if m.role == "tool"}
    return sorted(called - answered)


async def _run(llm, *, max_steps=5, hooks=(), fallback=None, session="final_call"):
    registry = get_hook_registry()
    for hook_type, name, hook in hooks:
        await registry.register_hook(hook_type, name, hook)
    try:
        agent = _real_agent(with_fallback_chain=fallback is not None)
        agent.agent_config.max_steps = max_steps
        # Pinned: a default change must not stop a blocking test from blocking.
        agent._loop_detection_config = {"history_size": 20, "exact_match_threshold": 3,
                                        "sequence_threshold": 2, "block_after_threshold": 5,
                                        "auto_unblock_after_steps": 3}
        agent.llm = llm
        llm.agent = agent
        if fallback is not None:
            agent._create_fallback_llm = lambda profile: fallback
        executed = []
        execute = agent._tool_execution_manager.execute_tools_streaming

        async def counting(**kwargs):
            executed.append(kwargs["step"])
            async for item in execute(**kwargs):
                yield item
        agent._tool_execution_manager.execute_tools_streaming = counting
        # Cleared first: a run that never persists (a cancel) would otherwise
        # leave the previous test's list standing and be measured for this one.
        _LAST_LIVE.clear()
        # What the loop finished with, before the tracker drops the notes.
        # get_live_messages() is written per step and is stale after the final
        # call -- the last persist is the only point that sees the whole list.
        persist = agent._persist_conversation

        async def capturing(session_id, messages, **kwargs):
            _LAST_LIVE[:] = list(messages)
            return await persist(session_id, messages, **kwargs)
        agent._persist_conversation = capturing
        events = [event async for event in agent.run_events("the task", request_id=REQUEST_ID,
                                                         session_id=session)]
        stored = list(agent._session_tracker.get_session_messages(session))
        assert _LAST_LIVE, "no persist captured -- the wrapper missed the funnel"
    finally:
        for hook_type, name, _ in hooks:
            await registry.unregister_hook(hook_type, name)
    return events, executed, stored


def _outcome(events):
    return ([e.get("summary") for e in events if e.get("type") == "final"],
            [e.get("message") for e in events if e.get("type") == "error"])


@pytest.mark.asyncio
async def test_the_max_steps_call_goes_through_the_pre_llm_hooks():
    hook = _RecordsPreLLM()
    llm = _LLM()
    events, _, _ = await _run(llm, hooks=[(HookType.PRE_LLM_CALL, "test.final_call.records", hook)])

    assert len(llm.seen) == 6, f"fixture: {len(llm.seen)} requests"
    assert len(hook.runs) == 6, f"the hooks ran {len(hook.runs)} times for 6 requests: {hook.runs}"
    assert hook.runs[-1][0] == 5, hook.runs
    # The note is appended AFTER the hooks, which is what keeps the run's last
    # word last. So the hooks do not see it -- the request does.
    assert hook.runs[-1][1] != MAX_STEPS_REQUEST, hook.runs
    assert llm.seen[-1][-1].injected_by == MAX_STEPS_REQUEST, "the run lost its last word"
    assert _outcome(events) == (["summary of what I have"], [])


@pytest.mark.asyncio
async def test_a_plugin_block_does_not_get_the_last_word_over_the_max_steps_request():
    """The last thing in the prompt is the run's, not a plugin's.

    The max-steps request says "answer NOW, do NOT use any tools". A model that
    reads a todo list with open items after it goes back to the tools -- the
    one thing the request exists to prevent. The note is therefore appended
    after the pre_llm_call hooks, not before them.
    """
    hook = _AppendsABlock()
    llm = _LLM()
    events, _, _ = await _run(llm, hooks=[(HookType.PRE_LLM_CALL, "test.final_call.appends", hook)])

    assert len(llm.seen) == 6, f"fixture: {len(llm.seen)} requests"
    final = llm.seen[-1]
    assert any(m.injected_by == _AppendsABlock.MARKER for m in final), \
        "fixture: the hook's block never reached the request"
    assert final[-1].injected_by == MAX_STEPS_REQUEST, \
        f"the request ends on {final[-1].injected_by!r}, not on the max-steps request"
    assert _outcome(events) == (["summary of what I have"], [])


@pytest.mark.asyncio
async def test_a_tool_blocked_on_the_last_step_leaves_no_orphan_in_the_final_request():
    llm = _LLM(same_args=True)
    hook = _RecordsPreLLM(validate=True)
    events, executed, _ = await _run(llm, hooks=[(HookType.PRE_LLM_CALL, "test.final_call.validates", hook)])

    assert len(llm.seen) == 6, f"fixture: {len(llm.seen)} requests"
    assert executed == [0, 1, 2, 3], f"fixture: the last step was not blocked, tool rounds at {executed}"
    assert not _orphans(llm.seen[-1]), f"the final request carries unanswered tool calls: {_orphans(llm.seen[-1])}"
    assert _outcome(events) == (["summary of what I have"], [])


@pytest.mark.asyncio
async def test_the_max_steps_call_falls_back_on_a_server_error():
    backup = _LLM(text_on_final="the backup's summary")
    events, _, _ = await _run(_LLM(server_error_on=2), max_steps=1, fallback=backup)

    assert len(backup.seen) == 1, f"fixture: the backup answered {len(backup.seen)} calls"
    assert _outcome(events) == (["the backup's summary"], [])


@pytest.mark.parametrize("max_steps", [1, 5, 30])
def test_the_call_after_the_budget_gets_the_max_steps_request(max_steps):
    note = Agent._step_budget_note(max_steps, max_steps)

    assert note is not None and note.role == DEVELOPER
    assert note.injected_by == MAX_STEPS_REQUEST
    assert f"maximum number of steps ({max_steps})" in note.content


@pytest.mark.asyncio
async def test_a_continuation_signal_on_the_final_call_is_ignored():
    llm = _LLM(text_steps=True)
    events, _, _ = await _run(llm, max_steps=3, hooks=[(HookType.POST_LLM_CALL, "test.final_call.continues",
                                                         _Continues({}))])

    assert len(llm.seen) == 4, f"{len(llm.seen)} requests for 3 steps and the final call"
    assert _outcome(events) == (["summary of what I have"], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, "delivered, see the store"])
async def test_tool_calls_on_the_final_call_run_once_and_end_the_run(text):
    llm = _LLM(tools_on_final=True, text_on_final=text)
    events, executed, stored = await _run(llm)

    assert len(llm.seen) == 6, f"{len(llm.seen)} requests: a call followed the final call's tools"
    assert executed == [0, 1, 2, 3, 4, 5], f"tool rounds at steps {executed}"
    assert [m.tool_call_id for m in stored if m.role == "tool"][-1] == "call_6", "the final call's result was not kept"
    assert not _orphans(stored), f"unanswered tool calls in the session: {_orphans(stored)}"
    assert all(m.content or m.tool_calls for m in stored if m.role == "assistant"), "an empty assistant message"

    # No final: the writer dispatch and the job manager take any final as
    # success, whatever the tools returned and whatever error follows.
    finals, errors = _outcome(events)
    assert finals == [], "a final event marks a run that ran out of steps as a success"
    assert len(errors) == 1 and errors[0].startswith("Agent incomplete: max steps (5) reached"), errors
    assert "missing_tool (error)" in errors[0]


@pytest.mark.asyncio
async def test_an_empty_final_call_persists_no_continue_nudge():
    events, _, stored = await _run(_LLM(empty_from_call=5))

    assert _outcome(events) == ([], ["LLM planner reached max steps without final answer."])
    markers = [m.injected_by for m in _notes(_LAST_LIVE)]
    assert MAX_STEPS_REQUEST in markers, f"fixture: no max-steps request in {markers}"
    assert "agent.empty_response" not in markers[markers.index(MAX_STEPS_REQUEST):], markers
    assert not _loop_notes(stored), (
        f"what the loop added outlived the run: {[m.injected_by for m in _loop_notes(stored)]}")


@pytest.mark.asyncio
async def test_tool_calls_blocked_on_the_final_call_leave_nothing_unanswered():
    """The same call every time: loop detection blocks it on the last step and
    on the final call. The validator repairs the step's orphan on the next
    request; after the final call there is none, so the loop must."""
    llm = _LLM(same_args=True, tools_on_final=True, text_on_final=None)
    hook = _RecordsPreLLM(validate=True)
    events, executed, stored = await _run(llm, hooks=[(HookType.PRE_LLM_CALL, "test.final_call.validates", hook)])

    assert len(llm.seen) == 6 and 5 not in executed, f"fixture: tool rounds at {executed}"
    assert _outcome(events) == ([], ["LLM planner reached max steps without final answer."])
    assert not _orphans(stored), f"unanswered tool calls in the session: {_orphans(stored)}"
    assert all(m.content or m.tool_calls for m in stored if m.role == "assistant"), "an empty assistant message"
    tail = _LAST_LIVE[[m.injected_by for m in _LAST_LIVE].index(MAX_STEPS_REQUEST):]
    assert [m.injected_by for m in tail] == [MAX_STEPS_REQUEST], f"the loop added after the final call: {tail}"


@pytest.mark.asyncio
async def test_a_cancel_during_the_final_call_reports_cancelled():
    """A step's cancel is reported at the top of the next iteration; the final
    call has none, so its tool round must check itself."""
    llm = _LLM(tools_on_final=True, text_on_final=None, cancel_on=6)
    events, _, _ = await _run(llm)

    assert len(llm.seen) == 6, f"fixture: {len(llm.seen)} requests"
    terminal = [e["type"] for e in events if e.get("type") in ("final", "error", "cancelled")]
    assert terminal == ["cancelled"], terminal


@pytest.mark.asyncio
async def test_text_with_only_blocked_tool_calls_on_the_final_call_is_the_answer():
    llm = _LLM(same_args=True, tools_on_final=True, text_on_final="here is my answer")
    hook = _RecordsPreLLM(validate=True)
    events, executed, stored = await _run(llm, hooks=[(HookType.PRE_LLM_CALL, "test.final_call.validates", hook)])

    assert len(llm.seen) == 6 and 5 not in executed, f"fixture: tool rounds at {executed}"
    assert _outcome(events) == (["here is my answer"], [])
    assert stored[-1].role == "assistant" and stored[-1].content == "here is my answer", stored[-1]
    assert not stored[-1].tool_calls, stored[-1].tool_calls


@pytest.mark.asyncio
async def test_blank_text_with_only_blocked_tool_calls_leaves_no_blank_assistant():
    llm = _LLM(same_args=True, tools_on_final=True, text_on_final="\n\n")
    hook = _RecordsPreLLM(validate=True)
    events, executed, stored = await _run(llm, hooks=[(HookType.PRE_LLM_CALL, "test.final_call.validates", hook)])

    assert len(llm.seen) == 6 and 5 not in executed, f"fixture: tool rounds at {executed}"
    assert _outcome(events) == ([], ["LLM planner reached max steps without final answer."])
    blank = [m for m in stored if m.role == "assistant" and not (m.content or "").strip() and not m.tool_calls]
    assert not blank, f"blank assistant messages in the session: {blank}"


@pytest.mark.asyncio
async def test_a_blank_text_answer_to_the_final_call_leaves_no_blank_assistant():
    llm = _LLM(text_on_final="   ")
    events, _, stored = await _run(llm)

    assert len(llm.seen) == 6, f"fixture: {len(llm.seen)} requests"
    assert _outcome(events) == ([], ["LLM planner reached max steps without final answer."])
    assert _LAST_LIVE[-1].injected_by == MAX_STEPS_REQUEST, f"the run ends on {_LAST_LIVE[-1]!r}"
    # And the session behind it: no blank turn, and no demand for a final
    # answer left standing. Resumed, that demand would be the last instruction
    # the model reads before whatever the person asks next.
    assert stored[-1].role == "tool", f"the session ends on {stored[-1]!r}"
    assert not _loop_notes(stored), "the max-steps demand outlived the run"


@pytest.mark.asyncio
async def test_a_cancel_behind_an_empty_final_call_reports_cancelled():
    """No tool round and no next iteration: only the check after the loop sees it."""
    llm = _LLM(empty_from_call=6, cancel_on=6)
    events, _, _ = await _run(llm)

    assert len(llm.seen) == 6, f"fixture: {len(llm.seen)} requests"
    terminal = [e["type"] for e in events if e.get("type") in ("final", "error", "cancelled")]
    assert terminal == ["cancelled"], terminal


@pytest.mark.asyncio
async def test_an_empty_final_call_after_blank_steps_is_no_empty_success():
    """Two blank steps and an empty final call reach the no-tools limit on the
    final call; that break would report an empty answer as a success."""
    llm = _LLM(blank_calls=(4, 5), empty_from_call=6)
    events, executed, _ = await _run(llm)

    assert len(llm.seen) == 6 and executed == [0, 1, 2], f"fixture: tool rounds at {executed}"
    assert _outcome(events) == ([], ["LLM planner reached max steps without final answer."])


@pytest.mark.asyncio
async def test_a_message_appended_during_the_final_call_does_not_drop_the_answer():
    llm = _LLM(append_on_final="one more thing")
    events, _, stored = await _run(llm)

    assert len(llm.seen) == 6, f"{len(llm.seen)} requests: a call followed the final call"
    assert _outcome(events) == (["summary of what I have"], [])
    assert any(m.role == "user" and m.content == "one more thing" for m in stored), "fixture: not drained"


@pytest.mark.asyncio
async def test_no_stuck_signal_on_the_final_call_opens_an_escalation(monkeypatch):
    """A loop and an error streak both fire on the final call here; an
    escalation window opened there has no step to run in, and its status line
    would announce one."""
    from agent_system.servers.agent.escalation import StuckEscalator

    llm = _LLM(same_args_from=4, tools_on_final=True, text_on_final=None)
    triggers = []

    class _Records(StuckEscalator):
        def trigger(self, reason):
            triggers.append((len(llm.seen), reason))
            return super().trigger(reason)

    monkeypatch.setattr(Agent, "_create_stuck_escalator",
                        lambda self, already_advanced: _Records(enabled=False, rounds=1, max_calls=1))
    _, executed, _ = await _run(llm)

    assert executed == [0, 1, 2, 3, 4, 5], f"fixture: tool rounds at {executed}"
    # The streak runs into the final call; the loop is first detected there
    # ("Tool loop detected at step 5", see the test below).
    assert any("all-error" in reason for n, reason in triggers if n == 5), f"fixture: no error streak on step 5: {triggers}"
    assert [t for t in triggers if t[0] == 6] == [], f"stuck signals on the final call: {triggers}"


@pytest.mark.asyncio
async def test_a_loop_detected_on_the_final_call_adds_no_intervention(caplog):
    """The same call on the last three requests: loop detection fires on the
    final call without blocking, so its tools run, and no step reads a warning."""
    llm = _LLM(same_args_from=4, tools_on_final=True, text_on_final=None)
    with caplog.at_level("WARNING"):
        events, executed, stored = await _run(llm)

    assert executed == [0, 1, 2, 3, 4, 5], f"fixture: tool rounds at {executed}"
    assert "Tool loop detected at step 5" in caplog.text, "fixture: no loop detected on the final call"
    tail = _LAST_LIVE[[m.injected_by for m in _LAST_LIVE].index(MAX_STEPS_REQUEST):]
    assert "agent.loop_intervention" not in [m.injected_by for m in tail], [m.injected_by for m in tail]
