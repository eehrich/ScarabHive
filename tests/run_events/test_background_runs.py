"""Background runs (agent_system.core.background_runs): work a turn started that goes on after it is kept as a
sub-session the chat reads like a sub-agent's run, and as a run in the run events that says how it stands, below the
session whose turn started it."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from agent_system import app_state, run_events
from agent_system.core import background_runs
from agent_system.core.background_runs import BackgroundRun
from agent_system.plugins.agent_tasks import cancel_run
from agent_system.run_events import RunEventBus, RunEventStore
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService

USER = "alice"
AUTH_ON = SimpleNamespace(auth=SimpleNamespace(enabled=True))


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = RunEventStore(tmp_path / "run_events.db")
    run_events.use_run_events(store, RunEventBus())
    yield store
    run_events.use_run_events(None)


@pytest.fixture
async def sessions(tmp_path, monkeypatch, store):
    monkeypatch.setattr(background_runs, "SAVE_S", 0.05)
    monkeypatch.setattr(background_runs, "LIST_RETRY_S", 0.05)
    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    monkeypatch.setattr(app_state, "session_service", SessionService(session_manager=manager))
    parent = await manager.create_session(user_id=USER, title="the chat", agent_name="coder")
    return manager, parent["session_id"]


async def _opened(manager, parent, **kwargs):
    stopped = []

    async def on_cancel():
        stopped.append(True)

    run = await BackgroundRun.open(system_config=AUTH_ON, parent=parent, user_id=USER, run_id="r1_003_sub_abc123",
                                   agent="claude_code", task="fix the parser", on_cancel=on_cancel, **kwargs)
    assert run is not None
    return run, stopped


async def test_a_run_is_kept_as_its_box_reads_it_and_listed_below_its_session(sessions, store):
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    await run.event({"type": "thinking", "step": 1})
    await run.event({"type": "reasoning_delta", "step": 1, "delta": "Reading it first.\n"})
    await run.event({"type": "tool_call", "request_id": "r1_003_sub_abc123_001", "action": "Read", "step": 1,
                     "params": {"file_path": "a.py"}}, label="Read a.py")
    await run.event({"type": "tool_result", "request_id": "r1_003_sub_abc123_001", "action": "Read",
                     "result": {"content": "print(1)"}})
    # written soon, not at every result: the sub-session is written whole
    assert len((await manager.load_session(USER, run.session_id))["messages"]) == 1, "written at the result itself"
    await asyncio.sleep(0.3)

    # at work: the sub-session holds what it did so far, the run its step, the parent's listing the run
    kept = await manager.load_session(USER, run.session_id)
    assert kept["parent_session"]["session_id"] == parent
    # the chat finds it as it finds a sub-agent's: among the parent's children, by the id its box carries
    children = await manager.list_child_sessions(USER, parent)
    assert [(c["session_id"], c.get("runs")) for c in children] == [(run.session_id, ["r1_003_sub_abc123"])]
    task, step, answer = kept["messages"]
    assert (task["role"], task["request_id"], task["content"]) == ("user", "r1_003_sub_abc123", "fix the parser")
    assert step["role"] == "assistant" and step["step"] == 1 and step["reasoning_content"] == "Reading it first.\n"
    assert step["tool_calls"][0]["function"]["name"] == "Read"
    assert step["tool_request_ids"] == {"r1_003_sub_abc123_001": "r1_003_sub_abc123_001"}
    assert (answer["role"], answer["tool_call_id"]) == ("tool", "r1_003_sub_abc123_001")
    assert store.background([parent], USER) == {parent: [{
        "run_id": run.session_id, "agent": "claude_code", "state": "working", "step_label": "Read a.py", "seq": 2}]}
    assert store.background([parent], "bob") == {}, "another user's run was reported"
    listing = (await manager.load_session(USER, parent))["metadata"][background_runs.METADATA_KEY]
    assert listing[run.session_id]["run_id"] == "r1_003_sub_abc123"
    assert listing[run.session_id]["state"] == "running"

    await run.close("done", "Fixed it.")
    kept = await manager.load_session(USER, run.session_id)
    assert kept["messages"][-1]["content"] == "Fixed it."
    assert kept["metadata"]["background"]["state"] == "done"
    assert store.run(run.session_id)["state"] == "done"
    assert store.background([parent], USER) == {}, "an ended run is still reported at work"
    listing = (await manager.load_session(USER, parent))["metadata"][background_runs.METADATA_KEY]
    assert listing[run.session_id]["state"] == "done"


async def test_a_failure_says_why_and_a_run_is_stopped_through_whoever_started_it(sessions, store):
    manager, parent = sessions
    run, stopped = await _opened(manager, parent)
    assert await cancel_run(run.session_id, USER) is True
    assert stopped == [True]
    await run.close("failed", reason="Claude Code: out of quota")
    kept = await manager.load_session(USER, run.session_id)
    assert kept["metadata"]["background"] == {**kept["metadata"]["background"], "state": "failed",
                                              "reason": "Claude Code: out of quota"}
    assert store.run(run.session_id)["state"] == "failed"
    assert await cancel_run(run.session_id, USER) is False, "an ended run was stopped again"


async def test_a_run_taken_over_writes_its_sub_session_anew(sessions, store):
    """A process that takes the work over reads its stream again from the start: what the first wrote is replaced,
    not doubled."""
    manager, parent = sessions
    first, _ = await _opened(manager, parent)
    await first.event({"type": "thinking", "step": 1})
    await first.event({"type": "tool_result", "request_id": "x", "action": "Read", "result": "a"})
    await asyncio.sleep(0.3)
    first.detach()   # its watch stopped
    again, _ = await _opened(manager, parent, session_id=first.session_id)
    await again.event({"type": "thinking", "step": 1})
    await again.event({"type": "tool_result", "request_id": "x", "action": "Read", "result": "a"})
    await asyncio.sleep(0.3)
    kept = await manager.load_session(USER, first.session_id)
    assert [m["role"] for m in kept["messages"]] == ["user", "assistant", "tool"]
    assert store.run(first.session_id)["state"] == "working"


async def test_without_authentication_a_run_is_everyones_as_the_run_events_name_them(sessions, store):
    manager, parent = sessions
    run = await BackgroundRun.open(system_config=SimpleNamespace(auth=SimpleNamespace(enabled=False)), parent=parent,
                                   user_id=USER, run_id="r2_sub_x", agent="claude_code", task="t")
    assert store.run(run.session_id)["who"] == "local"
    await run.close("cancelled")


async def test_a_process_that_keeps_no_sessions_keeps_no_run(monkeypatch, store):
    monkeypatch.setattr(app_state, "session_service", None)
    assert await BackgroundRun.open(system_config=AUTH_ON, parent="p", user_id=USER, run_id="r_sub_x",
                                    agent="claude_code", task="t") is None


async def test_a_new_chats_first_turn_lists_its_run_once_the_chat_is_saved(sessions, store):
    """The web chat saves a new session at the end of its first turn: a run started in that turn was opened for a
    parent not on disk yet -- and opened all the same, listed there once it is."""
    manager, _ = sessions
    going = await BackgroundRun.open(system_config=AUTH_ON, parent="s-new", user_id=USER, run_id="n1_sub_a",
                                     agent="claude_code", task="t")
    ended = await BackgroundRun.open(system_config=AUTH_ON, parent="s-new", user_id=USER, run_id="n2_sub_b",
                                     agent="claude_code", task="t")
    assert going is not None and ended is not None, "a run of a chat not saved yet was not opened"
    assert {r["run_id"] for r in store.background(["s-new"], USER)["s-new"]} == {going.session_id, ended.session_id}
    await ended.close("done", "ok")   # ends before the chat is saved
    await manager.create_session(user_id=USER, title="new chat", agent_name="coder", session_id="s-new")
    await going.event({"type": "tool_result", "request_id": "n1_sub_a_001", "action": "Read", "result": "a"})
    await asyncio.sleep(0.4)
    listing = (await manager.load_session(USER, "s-new"))["metadata"][background_runs.METADATA_KEY]
    assert listing[going.session_id]["state"] == "running"
    assert listing[ended.session_id]["state"] == "done", "a run ended before its chat was saved is never listed"
    await going.close("cancelled")


async def test_a_run_let_go_of_still_ends_when_its_end_comes(sessions, store):
    """A watch that stopped (detach) and then saw the run end after all: the run ended, not "at work" for good."""
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    run.detach()
    await run.event({"type": "tool_result", "request_id": "x", "action": "Read", "result": "late"})
    await run.close("cancelled")
    assert store.run(run.session_id)["state"] == "cancelled"
    assert store.background([parent], USER) == {}
    kept = await manager.load_session(USER, run.session_id)
    assert kept["metadata"]["background"]["state"] == "cancelled"
    assert [m["role"] for m in kept["messages"]] == ["user"], "a step came in after the run was let go of"


async def test_a_process_without_sessions_that_takes_a_run_over_keeps_its_state(sessions, store, monkeypatch):
    manager, parent = sessions
    first, _ = await _opened(manager, parent)
    first.detach()
    monkeypatch.setattr(app_state, "session_service", None)
    again = await BackgroundRun.open(system_config=AUTH_ON, parent=parent, user_id=USER, run_id=first.run_id,
                                     agent="claude_code", task="fix the parser", session_id=first.session_id)
    assert again is not None, "a run taken over where no sessions are kept fell silent"
    assert store.background([parent], USER)[parent][0]["run_id"] == first.session_id
    await again.close("done", "fixed")
    assert store.run(first.session_id)["state"] == "done"


async def test_a_run_ended_where_no_view_held_it_is_ended_after_what_it_kept(sessions, store):
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    await run.event({"type": "tool_result", "request_id": "x", "action": "Read", "result": "a"})
    await asyncio.sleep(0.3)
    run.detach()
    await BackgroundRun.ended(system_config=AUTH_ON, parent=parent, user_id=USER, run_id=run.run_id,
                              agent="claude_code", task="fix the parser", session_id=run.session_id, state="done",
                              answer="Fixed.")
    kept = await manager.load_session(USER, run.session_id)
    assert [m["role"] for m in kept["messages"]] == ["user", "tool", "assistant"]
    assert kept["messages"][-1]["content"] == "Fixed."
    assert store.run(run.session_id)["state"] == "done"


async def test_the_run_events_carry_a_short_task_not_the_whole_one(sessions, store):
    manager, parent = sessions
    run = await BackgroundRun.open(system_config=AUTH_ON, parent=parent, user_id=USER, run_id="t_sub_x",
                                   agent="claude_code", task="x" * 5000)
    assert store.events(run.session_id)[-1]["task"] == background_runs.SOURCE
    await run.close("cancelled")


async def test_a_run_whose_sub_session_is_gone_still_ends(sessions, store, caplog):
    """Its chat was deleted with its sub-sessions: the run still ends, or it stays at work for good -- and that is
    no error to log: nothing is left to write to."""
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    run.detach()
    await manager.delete_session(USER, run.session_id)
    await BackgroundRun.ended(system_config=AUTH_ON, parent=parent, user_id=USER, run_id=run.run_id,
                              agent="claude_code", task="fix the parser", session_id=run.session_id, state="failed",
                              reason="gone")
    assert store.run(run.session_id)["state"] == "failed"
    assert not [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_run_the_sweep_cut_off_ends_as_it_ended(sessions, store, monkeypatch, tmp_path):
    """The API restarted while the work went on and ended: the new store's sweep says "interrupted", the end found
    afterwards says how it really ended."""
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    run.detach()
    monkeypatch.setattr(run_events, "OWNER", run_events.OWNER.rsplit(":", 1)[0] + ":the next one")
    restarted = RunEventStore(store.path)
    run_events.use_run_events(restarted, RunEventBus())
    assert restarted.run(run.session_id)["state"] == "failed", "fixture: the sweep did not cut it off"
    await BackgroundRun.ended(system_config=AUTH_ON, parent=parent, user_id=USER, run_id=run.run_id,
                              agent="claude_code", task="fix the parser", session_id=run.session_id, state="done",
                              answer="Fixed.")
    assert restarted.run(run.session_id)["state"] == "done"


async def test_what_came_last_before_a_run_is_let_go_of_is_kept(sessions, store, monkeypatch):
    manager, parent = sessions
    monkeypatch.setattr(background_runs, "SAVE_S", 30)   # kept at the let-go, not when the wait would have ended
    run, _ = await _opened(manager, parent)
    await run.event({"type": "tool_result", "request_id": "x", "action": "Read", "result": "last"})
    run.detach()
    await asyncio.sleep(0.5)
    assert [m["role"] for m in (await manager.load_session(USER, run.session_id))["messages"]] == ["user", "tool"]


async def test_a_result_that_came_while_the_sub_session_was_written_is_written_too(sessions, store, monkeypatch):
    """A save under way starts no second one (_save_soon): what came meanwhile waited for the next result, or the
    run's end -- with a call that takes an hour, an hour."""
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    writing, go_on, saving = asyncio.Event(), asyncio.Event(), manager.save_session

    async def slow_save(data):
        if not writing.is_set():
            writing.set()
            await go_on.wait()
        return await saving(data)

    monkeypatch.setattr(manager, "save_session", slow_save)
    await run.event({"type": "tool_result", "request_id": "x", "action": "Read", "result": "a"})
    await asyncio.wait_for(writing.wait(), 2)
    await run.event({"type": "tool_result", "request_id": "y", "action": "Read", "result": "b"})
    go_on.set()
    await asyncio.sleep(0.4)
    kept = await manager.load_session(USER, run.session_id)
    assert [m["content"] for m in kept["messages"][1:]] == ["a", "b"]
    await run.close("cancelled")


async def test_a_call_is_in_its_sub_session_while_it_runs(sessions, store):
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    await run.event({"type": "tool_call", "step": 1, "request_id": "c1", "action": "Bash", "params": {"cmd": "make"}})
    await asyncio.sleep(0.3)
    kept = await manager.load_session(USER, run.session_id)
    assert [m["role"] for m in kept["messages"]] == ["user", "assistant"], "a call shows only once it has answered"
    await run.close("cancelled")


async def test_a_session_read_while_its_run_works_has_the_steps_not_written_yet(sessions, store, monkeypatch):
    """A chat that reads the sub-session along asks as the run says it took a step -- before the step is written.
    It showed each call only once the next one came, and one that took ten minutes not while it ran."""
    from agent_system.api.session_endpoints import get_session

    manager, parent = sessions
    monkeypatch.setattr(background_runs, "SAVE_S", 30)       # not written while the test looks
    run, _ = await _opened(manager, parent)
    await run.event({"type": "tool_call", "step": 1, "request_id": "c1", "action": "Bash", "params": {"cmd": "make"}})

    async def read(session_id):
        return await get_session(session_id, descendants=False, current_user=SimpleNamespace(username=USER),
                                 session_manager=manager, default_agent=None, tool_registry=None)

    assert [m["role"] for m in (await manager.load_session(USER, run.session_id))["messages"]] == ["user"]
    shown = await read(run.session_id)
    assert [m["role"] for m in shown["messages"]] == ["user", "assistant"], "the call at hand is not handed out"
    assert shown["messages"][1]["tool_calls"][0]["function"]["name"] == "Bash"
    # a copy: what a reader does with it is not the run's
    shown["messages"].clear()
    assert len(run._messages) == 2
    # the session that called it is no such work, and is handed out as it is written
    assert [m["role"] for m in (await read(parent))["messages"]] == []
    # once the run is over nothing is held: the session is read as it was written at the end
    await run.close("cancelled")
    assert background_runs.kept_so_far(run.session_id) is None
    assert [m["role"] for m in (await read(run.session_id))["messages"]] == ["user", "assistant"]


async def _end(run, parent, **kwargs):
    return await BackgroundRun.ended(system_config=AUTH_ON, parent=parent, user_id=USER, run_id=run.run_id,
                              agent="claude_code", task="fix the parser", session_id=run.session_id, **kwargs)


async def test_an_end_asked_for_again_changes_nothing(sessions, store):
    """A view file its ender could not remove asks for the end again: the run is not begun and ended anew."""
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    run.detach()
    await _end(run, parent, state="done", answer="Fixed.")
    events = len(store.events(run.session_id))
    await _end(run, parent, state="done", answer="Fixed.")
    kept = await manager.load_session(USER, run.session_id)
    assert [m["role"] for m in kept["messages"]] == ["user", "assistant"]
    assert len(store.events(run.session_id)) == events, "an ended run was begun again"


async def test_an_end_taken_over_writes_the_answer_once(sessions, store):
    """The process that ended it died between the answer and the listing: whoever takes the end over finds the
    answer written and the run still listed at work."""
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    run.detach()
    kept = await manager.load_session(USER, run.session_id)
    kept["messages"].append({"role": "assistant", "content": "Fixed."})
    await manager.save_session(kept)
    await _end(run, parent, state="done", answer="Fixed.")
    kept = await manager.load_session(USER, run.session_id)
    assert [m["role"] for m in kept["messages"]] == ["user", "assistant"]
    assert kept["metadata"]["background"]["state"] == "done"


async def test_how_a_run_ended_is_listed_where_its_last_steps_could_not_be_written(sessions, store, monkeypatch):
    """The chat reads how a run ended from its listing: a save that failed left it "at work" there, with the run
    events saying it had ended -- shown unfinished."""
    manager, parent = sessions
    run, _ = await _opened(manager, parent)

    async def refused(data):
        raise OSError("the file is held")

    monkeypatch.setattr(manager, "save_session", refused)
    await run.close("failed", reason="the subscription is used up")
    kept = await manager.load_session(USER, run.session_id)
    assert kept["metadata"]["background"]["state"] == "failed"
    assert kept["metadata"]["background"]["reason"] == "the subscription is used up"


async def test_an_end_found_later_waits_for_a_save_under_way(sessions, store):
    """The watch that let go writes what came last (detach) while the end is found elsewhere: read before that
    save was through, the end would write the steps from before it over it."""
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    run.detach()
    lock = app_state.session_service.save_lock(run.session_id)
    await lock.acquire()
    ending = asyncio.ensure_future(_end(run, parent, state="done", answer="Fixed."))
    await asyncio.sleep(0.05)
    kept = await manager.load_session(USER, run.session_id)
    kept["messages"].append({"role": "tool", "tool_call_id": "x", "name": "Read", "content": "the last step"})
    await manager.save_session(kept)
    lock.release()
    await ending
    kept = await manager.load_session(USER, run.session_id)
    assert [m["role"] for m in kept["messages"]] == ["user", "tool", "assistant"]


async def test_an_end_written_before_the_run_events_heard_of_it_is_said_there(sessions, store):
    """Its ender died between the sub-session and the run events: asked again, the end is written once and the
    run -- at work there, or cut off by a sweep -- ends as it ended."""
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    run.detach()
    kept = await manager.load_session(USER, run.session_id)
    kept["messages"] += [{"role": "tool", "tool_call_id": "x", "name": "Read", "content": "a step"},
                         {"role": "assistant", "content": "Fixed."}]
    await manager.save_session(kept)
    await manager.update_session_metadata(USER, run.session_id, {"background": {"state": "done"}}, touch=False)
    assert store.run(run.session_id)["state"] == "working", "fixture: the run events heard of the end"
    await _end(run, parent, state="done", answer="Fixed.")
    assert store.run(run.session_id)["state"] == "done"
    kept = await manager.load_session(USER, run.session_id)
    assert [m["role"] for m in kept["messages"]] == ["user", "tool", "assistant"]


def _restarted(store, monkeypatch):
    """The run events as a later process opens them: what the process before it held is ended as interrupted."""
    monkeypatch.setattr(run_events, "OWNER", run_events.OWNER.rsplit(":", 1)[0] + ":the next one")
    restarted = RunEventStore(store.path)
    run_events.use_run_events(restarted, RunEventBus())
    return restarted


async def test_a_failure_the_sweep_cut_off_says_why_and_asks_for_its_person(sessions, store, monkeypatch):
    """Its ender died after the sub-session had the failure and before the run events did; the sweep then ended
    the run as interrupted, seen. Both say "failed" -- but the run events say the wrong reason, to nobody."""
    manager, parent = sessions
    run, _ = await _opened(manager, parent)
    run.detach()
    await manager.update_session_metadata(USER, run.session_id, {"background": {"state": "failed"}}, touch=False)
    restarted = _restarted(store, monkeypatch)
    assert restarted.run(run.session_id)["acked"] == 1, "fixture: the sweep did not cut it off"
    assert await _end(run, parent, state="failed", reason="the subscription is used up")
    assert restarted.events(run.session_id)[-1]["payload"]["reason"] == "the subscription is used up"
    assert restarted.run(run.session_id)["acked"] == 0, "a failure nobody saw asks for nobody"


@pytest.mark.parametrize("refused", ["run.started", "run.done"])
async def test_an_end_the_run_events_did_not_take_is_said_so_and_taken_when_asked_again(sessions, store, monkeypatch,
                                                                                        refused):
    """Whoever asked for the end keeps what asks for it (a view file) until the run events have it: said "ended"
    all the same, the run stayed at work there for as long as its process lived."""
    manager, parent = sessions
    monkeypatch.setattr(run_events, "END_RETRY_S", 0.01)
    run, _ = await _opened(manager, parent)
    run.detach()
    appending, busy = store.append, [True]

    def append(run_id, type_, **kwargs):
        if busy and type_ == refused:
            raise OSError("database is locked")
        return appending(run_id, type_, **kwargs)

    monkeypatch.setattr(store, "append", append)
    assert await _end(run, parent, state="done", answer="Fixed.") is False
    assert store.run(run.session_id)["state"] == "working"
    busy.clear()
    assert await _end(run, parent, state="done", answer="Fixed.") is True
    assert store.run(run.session_id)["state"] == "done"
    kept = await manager.load_session(USER, run.session_id)
    assert [m["role"] for m in kept["messages"]] == ["user", "assistant"]


async def test_a_run_begun_again_to_be_ended_is_tried_as_its_end_is(sessions, store, monkeypatch):
    """Cut off by the sweep, the run is said begun again and then ended as it ended: a start the store refused
    once left it "interrupted" -- an end is refused on an ended run."""
    manager, parent = sessions
    monkeypatch.setattr(run_events, "END_RETRY_S", 0.01)
    run, _ = await _opened(manager, parent)
    run.detach()
    restarted = _restarted(store, monkeypatch)
    appending, refused = restarted.append, []

    def append(run_id, type_, **kwargs):
        if type_ == "run.started" and not refused:
            refused.append(True)
            raise OSError("database is locked")
        return appending(run_id, type_, **kwargs)

    monkeypatch.setattr(restarted, "append", append)
    assert await _end(run, parent, state="done", answer="Fixed.") is True
    assert refused and restarted.run(run.session_id)["state"] == "done"
