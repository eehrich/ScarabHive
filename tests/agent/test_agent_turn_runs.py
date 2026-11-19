"""A turn is told as a run (agent_system.core.turn_runs): who works now, on which step, below whom.

That a session was at work showed as a dot beside it; what it was doing only inside its chat, at the place of the
call -- and a sub-agent three levels down nowhere else at all. Real path: Agent.run_events, the production tool
dispatch for the nested agents, a real run event store; only the LLM is scripted.
"""
from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading

import pytest

from agent_system import run_events
from agent_system.core import turn_runs
from agent_system.core.cancellation import get_cancellation_manager
from agent_system.core.request_context import (register_request_user, release_request_user_tree,
                                               release_run_attended, set_run_attended)
from agent_system.plugins.agent_tasks import cancel_run
from agent_system.run_events import RunEventBus, RunEventStore, RunReporter, reporter_for
from agent_system.servers.agent.server import tool_session_id
from agent_system.services.background_job_manager import get_background_job_manager
from agent_system.tools.status import current_request_id
from test_agent_as_a_tool_runs_on_a_sub_session import SPAWN, world  # noqa: F401 - fixture
from test_agent_session_end_outcome import _agent, _BrokenModel, _Model


@pytest.fixture
def store(tmp_path):
    store = RunEventStore(tmp_path / "run_events.db")
    run_events.use_run_events(store, RunEventBus())
    turn_runs.report_turns()
    yield store
    turn_runs.settle()
    turn_runs._ENDING.clear()
    run_events.use_run_events(None)


@contextlib.contextmanager
def _attended(request_id):
    """A person started the run of ``request_id``, as the chat marks the turns it sends."""
    set_run_attended(request_id, True)
    try:
        yield request_id
    finally:
        release_run_attended(request_id)


async def _run(agent, model, session="turn-1", task="go\nand more", attended=True):
    """One turn to its end; ``attended``: started by a person."""
    request_id = f"rq-{session}"
    with _attended(request_id) if attended else contextlib.nullcontext():
        events = [event async for event in agent.run_events(task, session_id=session, llm_override=model,
                                                            request_id=request_id)]
    turn_runs.settle()
    return events


def _types(store, session):
    return [event["type"] for event in store.events(session)]


async def test_a_turn_is_a_run_of_its_session_with_its_steps_and_its_end(store):
    await _run(_agent(), _Model())

    run = store.run("turn-1")
    assert (run["state"], run["agent"], run["source"], run["task"]) == ("done", "test_agent", "chat", "turn")
    assert run["title"] == "go", "a turn is named after the first line of what it was asked"
    assert run["parent_session"] == ""
    events = store.events("turn-1")
    assert [e["type"] for e in events] == ["run.started", "run.step", "run.done"]
    assert events[1]["step"]["label"] == "Thinking"
    assert events[-1]["payload"] == {"summary": "done"}


async def test_a_process_nobody_is_at_tells_nothing(store, monkeypatch):
    """Only a process a person may be at tells its turns (the server and agent-cli say so at their start)."""
    turn_runs.report_turns(False)
    await _run(_agent(), _Model())
    assert store.run("turn-1") is None

    monkeypatch.setenv(turn_runs.ENV, "1")      # ... unless it is told to tell every turn
    await _run(_agent(), _Model(), attended=False)
    assert store.run("turn-1")["state"] == "done"

    turn_runs.report_turns()
    monkeypatch.setenv(turn_runs.ENV, "0")      # and one that would tell can be silenced
    await _run(_agent(), _Model(), session="turn-2")
    assert store.run("turn-2") is None


async def test_a_session_woken_as_a_process_of_its_own_tells_its_turn(store, monkeypatch):
    """A wake that comes from outside the server starts an agent-cli for the session. Nobody is at that process,
    so its turn counted as nobody's: the session list said the session was at work, what works now did not."""
    from agent_system.cli_utils.commands import run as run_command

    class _Presence:
        taken = []

        def hold(self, session_id, user, entry):
            pass

        def pending(self, session_id, user):
            return True

        def take_for_wake(self, session_id, user):
            self.taken.append(session_id)

    monkeypatch.setattr(turn_runs, "_every", False)      # set back when the test ends
    hold = dict(session_id="turn-1", session_user="alice", entry_name="coder", force=False, vprint=lambda *a, **k: None)
    # a run somebody typed (a script, a person): as before, nothing is told that nobody attends
    assert run_command.hold_session(_Presence(), woken=False, **hold) is True
    await _run(_agent(), _Model(), attended=False)
    assert store.run("turn-1") is None, "a one-shot run nobody attends was told"

    assert run_command.hold_session(_Presence(), woken=True, **hold) is True
    assert _Presence.taken == ["turn-1"]
    await _run(_agent(), _BrokenModel(), attended=False)
    told = store.run("turn-1")
    assert told is not None and told["source"] == "chat", "the woken turn was told nowhere"
    assert told["needs_you"] is True, "nobody watched it fail: it is theirs to hear of"


async def test_a_turn_no_person_started_tells_nothing_nor_does_what_runs_below_it(store, world):  # noqa: F811
    """A worker sends its jobs to the server by the thousand: told, each wrote its steps into the one file every
    process writes, pushed the chats out of the list, and rang for its person at every failure."""
    await _run(_agent(), _Model(), attended=False)
    assert store.run("turn-1") is None

    world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    register_request_user("rq-job", "alice")
    try:
        [_ async for _ in caller.run_events(task="a job", request_id="rq-job", session_id="job")]
    finally:
        release_request_user_tree("rq-job")
    turn_runs.settle()
    with sqlite3.connect(store.path) as db:      # every run there is -- the list leaves ended sub-agents out
        assert db.execute("SELECT run_id FROM agent_runs").fetchall() == []


async def test_a_cancelled_turn_ends_cancelled(store):
    await _run(_agent(), _Model(cancel=True))
    assert store.run("turn-1")["state"] == "cancelled"


async def test_a_turn_that_crashed_after_it_was_stopped_ends_cancelled(store):
    """Stopped, its tools and its model stop on the same token -- and one of them may raise over it: that is the
    stop, not a failure that waits for its person."""
    class Stopped(_Model):
        async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
            get_cancellation_manager().cancel_request(current_request_id.get())
            raise ValueError("the connection was closed")
            yield  # pragma: no cover - makes this an async generator

    events = await _run(_agent(), Stopped())
    assert any(e.get("type") == "error" for e in events), "fixture: the run did not crash"
    assert store.run("turn-1")["state"] == "cancelled"


async def test_a_failed_turn_asks_for_its_person_unless_they_watch_it(store, monkeypatch):
    """A failure in front of its person is seen. One whose person left meanwhile -- the tab closed, a session
    woken while nobody looked -- waits to be seen (the pill, the list of what needs you)."""
    await _run(_agent(), _BrokenModel(), session="watched")
    run = store.run("watched")
    assert run["state"] == "failed" and run["needs_you"] is False
    assert "the model is down" in store.events("watched")[-1]["payload"]["reason"]

    # no stream has read its job for a while
    monkeypatch.setattr(get_background_job_manager(), "unread_for", lambda request_id: 12.0)
    await _run(_agent(), _BrokenModel(), session="left")
    assert store.run("left")["needs_you"] is True


async def test_a_sub_agents_failure_is_its_callers_to_hear_of(store, world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_background_job_manager(), "unread_for", lambda request_id: 12.0)
    helper = world.called()

    async def broken(messages, tools, cancellation_token=None, status_scope=None):
        raise ValueError("the model is down")
        yield  # pragma: no cover - makes this an async generator

    helper.llm.chat_tools_streaming = broken
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    register_request_user("rq-top", "alice")
    try:
        with _attended("rq-top"):
            [_ async for _ in caller.run_events(task="the question", request_id="rq-top", session_id="top")]
    finally:
        release_request_user_tree("rq-top")
    turn_runs.settle()
    below = store.run(tool_session_id("top", "helper"))
    assert (below["state"], below["needs_you"]) == ("failed", False)


async def test_a_turn_whose_reader_left_at_an_error_still_ends(store):
    """sub_agent_manager and stategraph stop reading at the first error event: the run ends all the same."""
    with _attended("rq-left"):
        stream = _agent().run_events("go", session_id="left", llm_override=_BrokenModel(), request_id="rq-left")
        async for event in stream:
            if event.get("type") == "error":
                break
        await stream.aclose()
        for _ in range(200):
            if (store.run("left") or {}).get("state") == "failed":
                break
            await asyncio.sleep(0.01)
            turn_runs.settle()
    assert store.run("left")["state"] == "failed"


class _Hanging(_Model):
    """A model that never answers: the turn stays at work until it is stopped."""

    def __init__(self):
        super().__init__()
        self.waiting = asyncio.Event()

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        self.waiting.set()
        while not (cancellation_token is not None and cancellation_token.is_cancelled):
            await asyncio.sleep(0.02)
        yield {"type": "final", "assistant": {"role": "assistant", "content": "stopped"}}


async def _reading(session, model, request_id):
    with _attended(request_id):
        async for _ in _agent().run_events("go", session_id=session, llm_override=model, request_id=request_id):
            pass


async def test_a_turn_stopped_with_its_task_still_ends(store):
    """A client gone: the task that runs the turn is cancelled, and the turn ends in its ``finally`` -- where an
    await would be cut off before it wrote anything, and the run stayed at work until its process was gone."""
    model = _Hanging()
    reading = asyncio.ensure_future(_reading("stopped", model, "rq-stopped"))
    await asyncio.wait_for(model.waiting.wait(), 5)
    assert store.run("stopped")["state"] == "working"
    reading.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reading
    turn_runs.settle()
    assert store.run("stopped")["state"] == "cancelled"
    assert "stopped" not in turn_runs._TURNS


async def test_a_turn_is_stopped_through_its_run(store):
    """Stop in the list of what works: the run's cancel reaches the turn as the chat's own Stop does."""
    model = _Hanging()
    register_request_user("rq-stop", "alice")
    reading = asyncio.ensure_future(_reading("to-stop", model, "rq-stop"))
    try:
        await asyncio.wait_for(model.waiting.wait(), 5)
        assert not await cancel_run("no-such-session", "alice")
        assert await cancel_run("to-stop", "alice"), "the run's Stop found no turn"
        await asyncio.wait_for(reading, 10)
    finally:
        release_request_user_tree("rq-stop")
    turn_runs.settle()
    assert store.run("to-stop")["state"] == "cancelled"


async def test_a_start_cut_off_while_it_is_written_leaves_no_run_at_work(store, monkeypatch):
    """Cancelled while the start is on its way into the store: it is written all the same -- and with nobody to
    end it, the run stood at work until its process was gone."""
    writing, go_on, appending = threading.Event(), threading.Event(), store.append

    def slow(run_id, type_, **kwargs):
        if type_ == "run.started":
            writing.set()
            go_on.wait(5)
        return appending(run_id, type_, **kwargs)

    monkeypatch.setattr(store, "append", slow)
    reading = asyncio.ensure_future(_reading("cut", _Model(), "rq-cut"))
    await asyncio.to_thread(writing.wait, 5)
    reading.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reading
    go_on.set()
    for _ in range(100):
        if (store.run("cut") or {}).get("state") == "cancelled":
            break
        await asyncio.sleep(0.02)
    assert store.run("cut")["state"] == "cancelled"
    assert "cut" in turn_runs._ENDING, "the session's next turn does not wait for this end"


async def test_the_next_turn_of_a_session_begins_its_run_again(store):
    await _run(_agent(), _Model(), task="the first question")
    first = store.run("turn-1")
    await asyncio.sleep(0.01)
    await _run(_agent(), _Model(), task="the second question")
    second = store.run("turn-1")
    assert second["state"] == "done" and second["title"] == "the second question"
    assert second["started"] > first["started"], "the run's clock is the first turn's"
    assert _types(store, "turn-1").count("run.started") == 2


async def test_the_next_turn_waits_for_the_end_of_the_one_before(store, monkeypatch):
    """The end is written by a thread of its own. A turn begun before that end was written got it as its own:
    ended at once, every step refused."""
    appending = store.append

    def slow_end(run_id, type_, **kwargs):
        if type_ == "run.done":
            threading.Event().wait(0.4)
        return appending(run_id, type_, **kwargs)

    monkeypatch.setattr(store, "append", slow_end)
    with _attended("rq-a"):
        [_ async for _ in _agent().run_events("first", session_id="quick", llm_override=_Model(), request_id="rq-a")]
    with _attended("rq-b"):
        [_ async for _ in _agent().run_events("second", session_id="quick", llm_override=_Model(), request_id="rq-b")]
    turn_runs.settle()
    assert _types(store, "quick") == ["run.started", "run.step", "run.done"] * 2, _types(store, "quick")


async def test_a_panels_task_tells_its_own_run(store):
    """Its session has a reporter already (plugins/agent_tasks.py): the turn in it starts no second run and ends
    none -- the panel says how its task ended (a result to check)."""
    panel = RunReporter("turn-1", "code_analyst", who="alice", source="code_audit", task="assess")
    await panel.started()
    await _run(_agent(), _Model())
    run = store.run("turn-1")
    assert (run["state"], run["source"]) == ("working", "code_audit")
    assert not panel.ended and "turn-1" not in turn_runs._TURNS
    await panel.done("checked")


@pytest.mark.parametrize("waits", ["review", "failed"])
async def test_a_chat_in_a_session_whose_run_waits_for_its_person_leaves_that_run(store, waits):
    """A result to check, opened in the chat and asked about: the turn took the run over, and at its end the
    result was "done" unchecked -- a failure nobody had seen, a chat turn that went well."""
    panel = RunReporter("turn-1", "code_analyst", who="alice", title="Assess the issues", source="code_audit",
                        task="assess")
    await panel.started()
    await (panel.review_ready("please check") if waits == "review" else panel.failed("no access"))
    await _run(_agent(), _Model())
    run = store.run("turn-1")
    assert (run["state"], run["source"], run["title"], run["needs_you"]) == (waits, "code_audit", "Assess the issues", True)


async def test_a_chat_goes_on_in_a_session_whose_panel_run_is_through(store):
    panel = RunReporter("turn-1", "code_analyst", who="alice", source="code_audit", task="assess")
    await panel.started()
    await panel.done("3 issues")
    await _run(_agent(), _Model(), task="and the fourth?")
    run = store.run("turn-1")
    assert (run["state"], run["source"], run["task"], run["title"]) == ("done", "chat", "turn", "and the fourth?")


async def test_nobody_else_speaks_through_a_turns_reporter(store):
    """``reporter_for`` is how a tool asks its person through a panel's run (ask_user) and how a hook names a
    panel run's steps: a chat's turn found there, a question nobody watches waited in a panel that is not open."""
    found = []

    class Looking(_Model):
        async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
            found.append(reporter_for("turn-1"))
            found.append("turn-1" in turn_runs._TURNS)
            yield {"type": "final", "assistant": {"role": "assistant", "content": "done"}}

    await _run(_agent(), Looking())
    assert found == [None, True]


async def test_an_agent_called_by_a_turn_runs_below_it(store, world):  # noqa: F811
    """The tree: the caller's turn, the tool it calls as its step, and the called agent's turn below the caller's
    session."""
    world.called()
    caller = world.called(name="caller", first_call=("helper", {"task": "look it up"}), allowed=["helper"])
    register_request_user("rq-top", "alice")
    try:
        with _attended("rq-top"):
            [_ async for _ in caller.run_events(task="the question", request_id="rq-top", session_id="top")]
    finally:
        release_request_user_tree("rq-top")
    turn_runs.settle()

    top, below = store.run("top"), store.run(tool_session_id("top", "helper"))
    assert (top["source"], top["parent_session"], top["state"]) == ("chat", "", "done")
    assert (below["source"], below["parent_session"], below["state"]) == ("sub_agent", "top", "done")
    assert below["agent"] == "helper" and below["title"] == "look it up"
    labels = [e["step"]["label"] for e in store.events("top") if e["type"] == "run.step"]
    assert labels == ["Thinking", "helper · look it up", "Thinking"], labels


async def test_a_sub_agent_runs_below_the_turn_that_started_it(store, world):  # noqa: F811
    world.sam()
    boss = world.called(name="boss", first_call=SPAWN, allowed=["sam/*"])
    register_request_user("rq-boss", "alice")
    try:
        with _attended("rq-boss"):
            [_ async for _ in boss.run_events(task="get it done", request_id="rq-boss", session_id="boss-1")]
    finally:
        release_request_user_tree("rq-boss")
    turn_runs.settle()

    with sqlite3.connect(store.path) as db:      # an ended sub-agent is not in the list of runs
        [[sub_id]] = db.execute("SELECT run_id FROM agent_runs WHERE parent_session = 'boss-1'").fetchall()
    sub = store.run(sub_id)
    assert (sub["source"], sub["agent"], sub["state"], sub["title"]) == ("sub_agent", "worker", "done", "dig")
    assert store.run("boss-1")["state"] == "done"


def test_a_tool_call_in_a_line():
    assert turn_runs.tool_label("coder_fs", "coder_fs_read_file", {"path": "src/a.py"}) == "coder_fs: read_file · src/a.py"
    assert turn_runs.tool_label("helper", "helper", {"task": "look\n it up"}) == "helper · look it up"
    assert turn_runs.tool_label("datetime", "datetime_operations", {"_session_id": "s", "n": 3}) == "datetime: operations"
    assert turn_runs.tool_label("", "", None) == "tool"


async def test_calls_side_by_side_are_one_step(store):
    turn = await turn_runs.started(agent="a", system_config=None, session_id="s1", user_id="u", task="t",
                                   parent="s1", attended=True)
    assert store.run("s1")["parent_session"] == "", "a run below itself"
    turn_runs.tool_step("s1", [("fs", "fs_read", {"path": "a"}), ("fs", "fs_read", {"path": "b"}),
                               ("web", "web_get", {"url": "c"})])
    turn_runs.tool_step("unknown", [("fs", "fs_read", {"path": "a"})])
    await asyncio.sleep(0.2)
    turn_runs.ended(turn, "s1", {"summary": "ok"}, False)
    turn_runs.settle()
    assert [e["step"]["label"] for e in store.events("s1") if e["type"] == "run.step"] == ["fs: read · a (+2)"]


async def test_what_runs_below_a_panels_task_is_told_too(store):
    """The panel tells its task; the sub-agents that task calls are told below it, as below a chat's turn."""
    panel = RunReporter("panel-s", "code_analyst", who="alice", source="code_audit", task="assess")
    await panel.started()
    below = await turn_runs.started(agent="explorer", system_config=None, session_id="below-panel", user_id="alice",
                                    task="read the code", parent="panel-s", attended=False)
    assert below is not None, "a sub-agent of a panel's task is told nowhere"
    assert store.run("below-panel")["parent_session"] == "panel-s"
    turn_runs.ended(below, "below-panel", {"summary": "ok"}, False)
    await panel.done("checked")


async def test_a_chat_goes_on_after_its_own_failure_nobody_saw(store, monkeypatch):
    """A failure that waits to be seen holds the run for a PANEL's task; a chat's own failed turn does not keep
    the session's next turn from being told."""
    monkeypatch.setattr(get_background_job_manager(), "unread_for", lambda request_id: 12.0)
    await _run(_agent(), _BrokenModel())
    assert store.run("turn-1")["needs_you"] is True
    await _run(_agent(), _Model(), task="once more")
    run = store.run("turn-1")
    assert (run["state"], run["title"], run["needs_you"]) == ("done", "once more", False)


async def test_a_turn_no_person_started_that_is_told_all_the_same_asks_for_its_person(store, monkeypatch):
    """A process told to tell every turn (AGENT_TURN_RUNS=1): a failure of a turn nobody started is nobody's to
    have seen."""
    monkeypatch.setenv(turn_runs.ENV, "1")
    await _run(_agent(), _BrokenModel(), attended=False)
    assert store.run("turn-1")["needs_you"] is True
