"""A run keeps the status lines of each step with the step's message (ChatMessage.status_lines).

What a tool did and how it ended went out on the run's stream and nowhere else: a session opened again showed its
calls without their outcome, and a step whose model ran tools by itself (a coding CLI as the model) as if nothing
had been called. The forwarder through the real status bus; the loop through Agent.run_events with a scripted model.
"""
from __future__ import annotations

import copy

import pytest

from agent_system.llm.models import PRIVATE_MESSAGE_FIELDS, ChatMessage
from agent_system.servers.agent.components import status_forwarding
from agent_system.servers.agent.components.status_forwarding import StatusEventForwarder, lines_stay_with_the_caller
from agent_system.tools.status import StatusScope, current_request_id, status_bus, status_scope
from test_agent_session_end_outcome import _agent, _Model

_scope = status_scope      # a model's method takes a parameter of that name


@pytest.fixture
async def run():
    """The forwarder of a run "r1" that streams."""
    forwarder = StatusEventForwarder()
    await forwarder.start_forwarding("r1")
    yield forwarder
    await forwarder.stop_forwarding()


def _step(run, messages, number):
    """The run goes on to step ``number`` and stores its message, as the loop does; answers the message."""
    run.begin_step(number, messages)
    message = ChatMessage(role="assistant", content="", step=number)
    messages.append(message)
    run.stamp(messages)
    return message


def _said(message):
    return [(line["request_id"], line["phase"], line["message"]) for line in message.status_lines or []]


async def test_a_step_keeps_the_last_line_of_each_of_its_operations(run):
    messages = [ChatMessage(role="user", content="go")]
    run.begin_step(1, messages)
    # said while the model answers, before the step has a message: a model that runs tools by itself
    async with status_scope(status_bus, "coding_cli.run_task()", "r1_003") as during:
        await during.progress("turn 2")
        await during.end("run finished")
    message = ChatMessage(role="assistant", content="", step=1)
    messages.append(message)
    run.stamp(messages)
    # ... and by the step's tools, after it
    async with status_scope(status_bus, "file_ops.read_file()", "r1_004") as tool:
        await tool.progress("reading")
        await tool.end("Read README.md")
    async with status_scope(status_bus, "file_ops.write_file()", "r1_005") as failing:
        await failing.error("No such directory")
    assert _said(message) == [("r1_003", "end", "run finished"), ("r1_004", "end", "Read README.md"),
                              ("r1_005", "error", "No such directory")]
    line = message.status_lines[1]
    assert line["server"] == "file_ops.read_file()" and line["tree"] == {"parent_id": "r1", "depth_level": 1}
    assert line["timestamp"], "a line does not say when"
    assert "status_lines" in PRIVATE_MESSAGE_FIELDS, "the lines would be sent to a provider with the message"


async def test_the_next_steps_lines_are_the_next_messages(run):
    messages = [ChatMessage(role="user", content="go")]
    first = _step(run, messages, 1)
    async with status_scope(status_bus, "terminal.run()", "r1_003") as going:
        await going.progress("make: compiling")
        second = _step(run, messages, 2)
        assert second.status_lines is None, "a step that said nothing holds a list all the same"
        # what a step says before it has a message is not the last step's
        run.begin_step(3, messages)
        async with status_scope(status_bus, "context.summarize()", "r1_009") as early:
            await early.end("Summed up")
        assert second.status_lines is None
        third = ChatMessage(role="assistant", content="", step=3)
        messages.append(third)
        run.stamp(messages)
        async with status_scope(status_bus, "file_ops.read_file()", "r1_004") as tool:
            await tool.end("Read a.py")
        assert _said(first) == [("r1_003", "progress", "make: compiling")]
        # an operation that ends steps later ends on the line of the step it began in
        await going.end("make: done")
    assert _said(first) == [("r1_003", "end", "make: done")]
    assert second.status_lines is None
    assert _said(third) == [("r1_009", "end", "Summed up"), ("r1_004", "end", "Read a.py")]


async def test_a_copy_a_hook_handed_the_run_gets_the_lines_of_its_step(run):
    """Hooks hand the run copies of its messages: a compaction rebuilds every one, a pre-LLM hook that changes the
    list gets a deep copy of it. Given to the message once, the lines stayed with the one the run no longer held:
    an operation still going never ended in the session, and what its step's tools said afterwards was missing."""
    earlier = [ChatMessage(role="user", content="before"), ChatMessage(role="assistant", content="then", step=1),
               ChatMessage(role="assistant", content="and then", step=2)]
    messages = [*earlier, ChatMessage(role="user", content="go")]
    first = _step(run, messages, 1)
    async with status_scope(status_bus, "context_engineer.compact()", "r1_003") as compacting:
        await compacting.progress("Compacting 37 messages...")
        rebuilt = [ChatMessage(**message.model_dump(exclude_none=True)) for message in messages]   # as the tool does
        assert _said(rebuilt[-1]) == [("r1_003", "progress", "Compacting 37 messages...")]
        await compacting.end("Compacted: saved 30000 tokens")
        async with status_scope(status_bus, "file_ops.read_file()", "r1_004") as sibling:
            await sibling.end("Read a.py")
    assert _said(rebuilt[-1]) == [("r1_003", "progress", "Compacting 37 messages...")], "fixture: the copy followed"
    # the next step begins with the list the hook handed back: right then, not when its model has answered --
    # a reader of the session sees the step before as it ended for all of that call
    run.begin_step(2, rebuilt)
    assert rebuilt[-1] is not first
    assert _said(rebuilt[-1]) == [("r1_003", "end", "Compacted: saved 30000 tokens"), ("r1_004", "end", "Read a.py")]
    second = ChatMessage(role="assistant", content="", step=2)
    rebuilt.append(second)
    run.stamp(rebuilt)
    # a deep copy mid-step (a pre-LLM hook changed the list): the run's last save finds the lines all the same
    async with status_scope(status_bus, "terminal.run()", "r1_005") as tool:
        copied = copy.deepcopy(rebuilt)
        await tool.end("make: done")
    assert [line[1] for line in _said(copied[-1])] == ["start"] and _said(second) == [("r1_005", "end", "make: done")]
    run.stamp(copied)
    assert _said(copied[-1]) == [("r1_005", "end", "make: done")]
    # ... and the run before this one keeps its own: its steps are numbered 1 and 2 as well
    assert all(message.status_lines is None for message in copied[:3]), "an earlier run was given this run's lines"
    # also where a compaction took this run's first step out and the run before it was the shorter one: the
    # message that opened this run is where it began -- typed by its person, or a wake's, which carries the
    # run's id. In a session older than that id the numbers say it, which stop falling
    for opener in (ChatMessage(role="user", content="go"),
                   ChatMessage(role="user", content="woken", injected_by="presence", request_id="r1")):
        shorter = [ChatMessage(role="assistant", content="before", step=1), opener, copy.deepcopy(copied[-1])]
        shorter[-1].status_lines = None
        run.stamp(shorter)
        assert shorter[0].status_lines is None, f"the run before was given this run's lines ({opener.content})"
        assert _said(shorter[-1]) == [("r1_005", "end", "make: done")]
    later = [ChatMessage(role="assistant", content="before", step=2), ChatMessage(role="user", content="woken",
                                                                                   injected_by="presence"),
             copy.deepcopy(copied[-1])]
    run.stamp(later)
    assert later[0].status_lines is None, "a run before with as many steps was given this run's lines"
    # a message that holds more than its step's lines (two merged into one) keeps them
    merged = [*copied[:-1], copied[-1].model_copy(update={"status_lines": [*copied[-2].status_lines,
                                                                           *copied[-1].status_lines]})]
    run.stamp(merged)
    assert len(merged[-1].status_lines) == 3


async def test_the_runs_own_frame_and_the_runs_below_it_keep_their_lines_to_themselves(run):
    # the run's coordinator: it begins before the first step and goes on through all of them -- the frame of the
    # run, no step's. Read back it was a row "step 3/30" still going
    frame = StatusScope(status_bus, "agent_coordinator", "r1_001")
    await frame.__aenter__()
    messages = [ChatMessage(role="user", content="go")]
    run.begin_step(1, messages)
    await frame.progress("step 1/30")
    message = ChatMessage(role="assistant", content="", step=1)
    messages.append(message)
    run.stamp(messages)
    await frame.progress("step 2/30")
    # an agent called as a tool: its run's id is its call's own, nothing in it says "a run" -- it streams
    as_a_tool = StatusEventForwarder()
    await as_a_tool.start_forwarding("r1_007")
    try:
        async with status_scope(status_bus, "helper.run()", "r1_007") as called:
            async with status_scope(status_bus, "file_ops.read_file()", "r1_007_003") as inner:
                await inner.end("Read c.py")
            await called.end("helper answered")
    finally:
        await as_a_tool.stop_forwarding()
    # a run whose id says it is one: the call that started it is this run's, what it does is its own -- also
    # when it does not stream (a coding CLI's view lets go of its stream at once, and its hundreds of tool lines
    # filled the step of the call that started it)
    async with status_scope(status_bus, "sub_agent_manager.spawn()", "r1_004") as spawn:
        for run_below in ("r1_004_sub_a", "r1_004_async_b", "r1_004_sub_cont_c", "r1_004_sub_a_minlen_1"):
            async with status_scope(status_bus, "file_ops.read_file()", f"{run_below}_003") as inner:
                await inner.end("Read b.py")
        await spawn.end("helper answered")
    await frame.__aexit__(None, None, None)
    assert _said(message) == [("r1_004", "end", "helper answered")]


async def test_a_run_below_keeps_the_lines_of_its_own_steps():
    """Its own id says "a run below": that is itself, not a run below it."""
    below = StatusEventForwarder()
    await below.start_forwarding("r1_004_sub_a")
    try:
        messages = [ChatMessage(role="user", content="go")]
        message = _step(below, messages, 1)
        async with status_scope(status_bus, "file_ops.read_file()", "r1_004_sub_a_003") as tool:
            await tool.end("Read b.py")
        async with status_scope(status_bus, "sub_agent_manager.spawn()", "r1_004_sub_a_004") as spawn:
            async with status_scope(status_bus, "file_ops.read_file()", "r1_004_sub_a_004_sub_b_003") as deeper:
                await deeper.end("Read c.py")
            await spawn.end("answered")
        # the run that tries this one again is a run of its own, though its id goes on from this one's
        async with status_scope(status_bus, "file_ops.read_file()", "r1_004_sub_a_minlen_1_003") as retried:
            await retried.end("Read b.py again")
    finally:
        await below.stop_forwarding()
    assert _said(message) == [("r1_004_sub_a_003", "end", "Read b.py"), ("r1_004_sub_a_004", "end", "answered")]


async def test_a_run_below_that_nothing_else_keeps_leaves_its_lines_with_its_caller(run):
    """A coding CLI that answers as the model is one run per model call, kept nowhere: what its tools said is the
    calling run's to keep, or the session read back shows a step in which nothing was called."""
    messages = [ChatMessage(role="user", content="go")]
    run.begin_step(1, messages)
    lines_stay_with_the_caller("r1_sub_abc123")
    try:
        async with status_scope(status_bus, "claude_code", "r1_sub_abc123_001") as tool:
            await tool.end("Read parser.c")
        async with status_scope(status_bus, "claude_code", "r1_sub_other_001") as kept_elsewhere:
            await kept_elsewhere.end("Read lexer.c")
    finally:
        lines_stay_with_the_caller("r1_sub_abc123", False)
    async with status_scope(status_bus, "claude_code", "r1_sub_abc123_002") as late:
        await late.end("Read too late")
    message = ChatMessage(role="assistant", content="done", step=1)
    messages.append(message)
    run.stamp(messages)
    assert _said(message) == [("r1_sub_abc123_001", "end", "Read parser.c")]


async def test_a_step_keeps_so_many_lines_of_so_many_characters(run, monkeypatch):
    monkeypatch.setattr(status_forwarding, "MAX_STEP_LINES", 2)
    monkeypatch.setattr(status_forwarding, "MAX_LINE_CHARS", 10)
    messages = [ChatMessage(role="user", content="go")]
    message = _step(run, messages, 1)
    for number in (3, 4, 5):
        async with status_scope(status_bus, "file_ops.read_file()", f"r1_00{number}") as tool:
            await tool.end("Read a file with a very long name")
    assert _said(message) == [("r1_003", "end", "Read a fil"), ("r1_004", "end", "Read a fil")]
    # the next step has its own count
    second = _step(run, messages, 2)
    async with status_scope(status_bus, "file_ops.read_file()", "r1_006") as tool:
        await tool.end("Read")
    assert _said(second) == [("r1_006", "end", "Read")]


async def test_an_operation_that_starts_again_after_its_end_is_a_line_of_its_own(run):
    """As the run's stream shows it: the row of an operation that ended is done, a start after it opens a new one
    -- a coding CLI as the model says its run_task and its get_run under the run's own id."""
    messages = [ChatMessage(role="user", content="go")]
    message = _step(run, messages, 1)
    for server, said in (("coding_cli.run_task()", "run started"), ("coding_cli.get_run()", "run done: 3 turns")):
        async with status_scope(status_bus, server, "r1") as tool:
            await tool.end(said)
    assert _said(message) == [("r1", "end", "run started"), ("r1", "end", "run done: 3 turns")]


async def test_a_run_that_starts_again_under_the_forwarder_starts_with_no_lines(run):
    messages = [ChatMessage(role="user", content="go")]
    run.begin_step(1, messages)
    async with status_scope(status_bus, "file_ops.read_file()", "r1_003") as tool:
        await tool.progress("reading")
        await run.stop_forwarding()
        await run.start_forwarding("r1")
        message = ChatMessage(role="assistant", content="", step=1)
        run.stamp([message])
        assert message.status_lines is None


class _ModelThatRunsTools(_Model):
    """A model that runs tools by itself while it answers, as a coding CLI does: each call of it says a status line
    under the run, then asks for a tool once and answers."""

    def __init__(self):
        super().__init__()
        self.seen: list[list] = []

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.seen.append(list(messages))
        call = len(self.seen)
        async with _scope(status_bus, "coding_cli.run_task()", f"{current_request_id.get()}_9{call:02d}") as own:
            await own.end(f"model call {call}: 2 tools")
        if call == 1:
            yield {"type": "final", "assistant": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "nothing", "arguments": "{}"}}]}}
            return
        yield {"type": "final", "assistant": {"role": "assistant", "content": "done"}}


async def test_a_run_keeps_what_its_model_calls_said_with_the_steps_they_were(monkeypatch):
    agent, model = _agent(), _ModelThatRunsTools()
    saved: list[list] = []
    persist = type(agent)._persist_conversation

    async def persisted(self, sid, messages, **options):
        saved.append(list(messages))
        return await persist(self, sid, messages, **options)

    monkeypatch.setattr(type(agent), "_persist_conversation", persisted)
    # how the first step's message stood while its tools ran: as the second step began, before that stamped it
    during: dict[int, list] = {}
    begin = StatusEventForwarder.begin_step

    def begins(self, step, messages):
        during[step] = [_said(message) for message in messages if getattr(message, "role", None) == "assistant"]
        begin(self, step, messages)

    monkeypatch.setattr(StatusEventForwarder, "begin_step", begins)
    events = [event async for event in agent.run_events("go", session_id="lines-1", llm_override=model,
                                                        request_id="rq-lines")]
    assert during[2] == [[("rq-lines_901", "end", "model call 1: 2 tools")]], \
        "what the model call said was not on the step's message while the step went on (a reader saw none of it)"
    assert any(event.get("type") == "final" for event in events)
    steps = {message.step: message for message in saved[-1] if getattr(message, "role", None) == "assistant"}
    assert _said(steps[1]) == [("rq-lines_901", "end", "model call 1: 2 tools")]
    # the step that only answered: nothing else says that anything ran in it
    assert _said(steps[2]) == [("rq-lines_902", "end", "model call 2: 2 tools")]
    assert (steps[2].content, steps[2].tool_calls) == ("done", None)
    # the second call was sent the first step's message, lines and all: a client leaves them out by the list
    first = next(message for message in model.seen[1] if getattr(message, "role", None) == "assistant")
    assert _said(first) == _said(steps[1])


class _ModelUnderACompaction(_Model):
    """Its first call starts an operation that goes on; during its second, a tool replaces the session's history
    with copies (session_tracker.set_compacted_messages, as a compaction does) -- then the operation ends."""

    def __init__(self, agent):
        super().__init__()
        self.agent = agent
        self.calls = 0
        self.going = None

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.calls += 1
        if self.calls == 1:
            self.going = StatusScope(status_bus, "terminal.run()", f"{current_request_id.get()}_901")
            await self.going.__aenter__()
            await self.going.progress("make: compiling")
            yield {"type": "final", "assistant": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "nothing", "arguments": "{}"}}]}}
            return
        self.agent._session_tracker.set_compacted_messages(
            "lines-2", [message for message in copy.deepcopy(list(messages)) if message.role != "system"])
        await self.going.end("make: done")
        await self.going.__aexit__(None, None, None)
        yield {"type": "final", "assistant": {"role": "assistant", "content": "done"}}


async def test_the_last_save_of_a_history_a_tool_replaced_has_the_lines_as_they_ended(monkeypatch):
    """A tool that replaces the history hands the run's last save copies of its messages: the lines stood there
    as they were when the copies were made -- an operation that ended afterwards was "still going" in the session
    for good."""
    agent = _agent()
    model = _ModelUnderACompaction(agent)
    saved: list[list] = []

    async def to_disk(self, sid):
        saved.append(list(self._session_tracker.get_session_messages(sid)))
        return True

    monkeypatch.setattr(type(agent), "_save_session_to_disk", to_disk)
    [event async for event in agent.run_events("go", session_id="lines-2", llm_override=model, request_id="rq-two")]
    assert saved, "fixture: the run did not save the history the tool replaced"
    first = next(message for message in saved[-1] if getattr(message, "role", None) == "assistant")
    assert _said(first) == [("rq-two_901", "end", "make: done")]
