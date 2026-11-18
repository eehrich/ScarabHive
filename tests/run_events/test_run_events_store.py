"""Run events (agent_system.run_events): a run's events are numbered without gaps, carry its state and its step in
plain words, end once, and show a run that went quiet as silent."""
import asyncio
import os
import time

import pytest

from agent_system import run_events
from agent_system.plugins.agent_tasks import report_end
from agent_system.run_events import (
    MAX_LABEL,
    RunEventBus,
    RunEventStore,
    RunReporter,
    reporter_for,
)


@pytest.fixture
def store(tmp_path):
    return RunEventStore(tmp_path / "run_events.db")


@pytest.fixture
def bus():
    return RunEventBus()


def reporter(store, bus, run_id="s1", **kwargs):
    return RunReporter(run_id, "code_analyst", who="alice", title="Code analysis", source="code_audit",
                       task="analyse", store=store, bus=bus, **kwargs)


async def test_a_run_says_its_state_and_its_step_in_order(store, bus):
    run = reporter(store, bus)
    await run.started(["Read the code", "Find the entry points", "Write the report"])
    await run.step("Read the code", touching=["src/app.py"])
    await run.step("Find the entry points")
    await run.done("12 attack vectors")

    events = store.events("s1")
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert [e["type"] for e in events] == ["run.started", "run.step", "run.step", "run.done"]
    assert [e["state"] for e in events] == ["working", "working", "working", "done"]
    assert events[0]["payload"] == {"plan": ["Read the code", "Find the entry points", "Write the report"]}
    assert events[1]["step"] == {"index": 1, "total": 3, "key": "", "label": "Read the code"}
    assert "labels" not in events[1]["step"]
    assert events[1]["touching"] == ["src/app.py"]
    assert events[2]["step"]["index"] == 2 and events[2]["progress"] == pytest.approx(1 / 3, abs=0.001)
    assert events[3]["step"] is None and events[3]["payload"] == {"summary": "12 attack vectors"}
    shown = store.run("s1")
    assert shown["state"] == "done" and shown["ended"] and shown["last_seq"] == 4 and not shown["silent"]


async def test_the_same_label_again_is_the_same_step(store, bus):
    run = reporter(store, bus)
    await run.started()
    await run.step("Read the code")
    await run.step("Read the code", touching=["src/b.py"])
    await run.step("Write the report")
    assert [e["step"]["index"] for e in store.events("s1")[1:]] == [1, 1, 2]


async def test_a_long_label_is_cut_for_a_glance(store, bus):
    run = reporter(store, bus)
    await run.started()
    await run.step("x" * 200)
    assert len(store.events("s1")[-1]["step"]["label"]) == MAX_LABEL


async def test_a_run_ends_once_and_says_nothing_after(store, bus):
    run = reporter(store, bus)
    await run.started()
    assert await run.failed("the model answered nothing") is not None
    assert await run.done("late") is None
    assert await run.step("late") is None
    assert await run.heartbeat() is None
    assert [e["type"] for e in store.events("s1")] == ["run.started", "run.failed"]
    assert store.events("s1")[-1]["payload"] == {"reason": "the model answered nothing"}


async def test_an_answered_run_goes_on_with_the_next_numbers(store, bus):
    first = reporter(store, bus)
    await first.started()
    await first.done()
    again = reporter(store, bus)
    await again.started()
    await again.step("Read the answer")
    events = store.events("s1")
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert store.run("s1")["state"] == "working" and store.run("s1")["ended"] is None
    assert events[-1]["step"]["index"] == 1


def test_only_a_start_opens_a_run(store):
    assert store.append("nobody", "run.step", step={"index": 1, "label": "x"}) is None
    assert store.run("nobody") is None
    with pytest.raises(ValueError):
        store.append("s1", "run.started_badly")


async def test_a_run_that_went_quiet_is_shown_silent(store, bus, monkeypatch):
    run = reporter(store, bus)
    await run.started()
    assert store.run("s1")["silent"] is False
    later = time.time() + run_events.SILENT_S + 1
    monkeypatch.setattr(run_events.time, "time", lambda: later)
    assert store.run("s1")["silent"] is True
    assert store.runs("alice")[0]["silent"] is True


async def test_an_ended_run_is_never_silent(store, bus, monkeypatch):
    run = reporter(store, bus)
    await run.started()
    await run.done()
    later = time.time() + run_events.SILENT_S + 1
    monkeypatch.setattr(run_events.time, "time", lambda: later)
    assert store.run("s1")["silent"] is False


def _sent(queue):
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def test_a_quiet_run_gets_a_heartbeat_until_it_ends(store, bus):
    queue = bus.subscribe()
    run = reporter(store, bus, heartbeat_s=0.05)
    await run.started()
    run.keep_alive()
    await asyncio.sleep(0.18)
    await run.done()
    await asyncio.sleep(0)
    count = sum(e["type"] == "run.heartbeat" for e in _sent(queue))
    await asyncio.sleep(0.12)
    assert count >= 2
    assert not any(e["type"] == "run.heartbeat" for e in _sent(queue))   # none after the end
    assert store.events("s1")[-1]["type"] == "run.done"


async def test_a_heartbeat_keeps_the_step_it_is_on(store, bus):
    queue = bus.subscribe()
    run = reporter(store, bus)
    await run.started()
    await run.step("Read the code")
    await run.heartbeat()
    await asyncio.sleep(0)
    beat = _sent(queue)[-1]
    assert beat["type"] == "run.heartbeat" and beat["state"] == "working"
    assert beat["step"]["label"] == "Read the code"


async def test_a_heartbeat_is_sent_not_kept_and_keeps_the_run_from_going_silent(store, bus, monkeypatch):
    queue = bus.subscribe()
    run = reporter(store, bus)
    await run.started()
    await run.step("Read the code")
    kept = store.events("s1")
    later = time.time() + run_events.SILENT_S + 1
    monkeypatch.setattr(run_events.time, "time", lambda: later)
    assert store.run("s1")["silent"] is True
    await run.heartbeat()
    await asyncio.sleep(0)
    beat = _sent(queue)[-1]
    assert store.events("s1") == kept                    # one every few seconds per run: not in the timeline
    assert beat["cursor"] is None and beat["seq"] == kept[-1]["seq"]
    assert store.run("s1")["silent"] is False             # but the run is alive again


async def test_a_new_store_ends_the_runs_whose_process_is_gone(tmp_path, monkeypatch):
    path = tmp_path / "run_events.db"
    first = RunEventStore(path)
    bus = RunEventBus()
    for run_id in ("working", "asking", "checking", "done"):
        await RunReporter(run_id, "code_analyst", who="alice", store=first, bus=bus).started()
    # work below a session (core/background_runs): its session shows it, its starter may take it over
    await RunReporter("background", "claude_code", who="alice", parent="s-chat", store=first, bus=bus).started()
    await RunReporter("asking", "code_analyst", who="alice", store=first, bus=bus).needs_input(
        "Which one?", question_id="q1", answer_url="/plugins/ask_user/answer")
    await RunReporter("checking", "code_analyst", who="alice", store=first, bus=bus).review_ready("look")
    await RunReporter("done", "code_analyst", who="alice", store=first, bus=bus).done()
    # the same process: nothing is ended
    assert RunEventStore(path).swept == []
    # the next interpreter -- here one that re-executed in place (os.execv: same id, same start time): what was at
    # work or asking is cut off; a result to check and an end stay
    pid, since, _mark, space = run_events.OWNER.split(":")
    monkeypatch.setattr(run_events, "OWNER", f"{pid}:{since}:another:{space}")
    swept = RunEventStore(path).swept
    assert sorted(e["run_id"] for e in swept) == ["asking", "background", "working"]
    assert [e["needs_you"] for e in swept if e["run_id"] == "background"] == [False],         "a background run about to be taken over raised an alarm"
    runs = {r["run_id"]: r for r in RunEventStore(path).runs("alice")}
    assert (runs["working"]["state"], runs["working"]["needs_you"]) == ("failed", True)
    assert (runs["background"]["state"], runs["background"]["needs_you"]) == ("failed", False)
    assert runs["asking"]["state"] == "failed"
    assert (runs["checking"]["state"], runs["done"]["state"]) == ("review", "done")
    assert "interrupted" in RunEventStore(path).events("working")[-1]["payload"]["reason"]


async def test_a_run_waiting_for_an_answer_is_not_acknowledged(store, bus):
    run = reporter(store, bus)
    await run.started()
    await run.needs_input("Which one?", question_id="q1", answer_url="/plugins/ask_user/answer")
    kept = store.events("s1")
    assert store.acknowledge("s1", "alice") is None
    assert store.append("s1", "run.acknowledged") is None
    assert store.events("s1") == kept and store.run("s1")["needs_you"] is True


async def test_every_event_reaches_an_open_stream(store, bus):
    queue = bus.subscribe()
    run = reporter(store, bus)
    await run.started()
    await run.step("Read the code")
    await asyncio.sleep(0)
    got = [queue.get_nowait()["type"], queue.get_nowait()["type"]]
    assert got == ["run.started", "run.step"]
    bus.unsubscribe(queue)
    assert bus.subscribers == 0


async def test_a_dropped_stream_reads_on_from_where_it_was_and_only_its_own(store, bus):
    mine = reporter(store, bus)
    other = RunReporter("s2", "inventory_agent", who="someone", store=store, bus=bus)
    first = await mine.started()
    await other.started()
    await mine.step("Read the code")
    missed = store.since(store.cursor_of(first["event_id"]), "alice")
    assert [(e["run_id"], e["type"]) for e in missed] == [("s1", "run.step")]
    assert {e["run_id"] for e in store.since(0, None)} == {"s1", "s2"}


async def test_the_running_reporter_is_found_by_its_session_until_it_ends(store, bus):
    run = reporter(store, bus, run_id="s9")
    assert reporter_for("s9") is None
    await run.started()
    assert reporter_for("s9") is run
    await run.cancelled("alice")
    assert reporter_for("s9") is None


async def test_a_store_that_fails_never_breaks_the_run(bus, tmp_path, caplog):
    class Broken(RunEventStore):
        def append(self, *args, **kwargs):
            raise OSError("disk full")

    run = reporter(Broken(tmp_path / "x.db"), bus)
    assert await run.started() is None
    assert await run.step("Read the code") is None
    assert "not kept" in caplog.text


@pytest.mark.parametrize("record, kind, payload", [
    ({"state": "done", "summary": "3 issues"}, "run.done", {"summary": "3 issues"}),
    ({"state": "cancelled"}, "run.cancelled", None),
    ({"state": "refused", "error": "no permission"}, "run.failed", {"reason": "refused: no permission"}),
    ({"state": "failed", "error": "boom"}, "run.failed", {"reason": "boom"}),
])
async def test_how_a_panel_run_ended_becomes_its_last_event(store, bus, record, kind, payload):
    run = reporter(store, bus)
    await run.started()
    await report_end(run, record)
    last = store.events("s1")[-1]
    assert (last["type"], last["payload"]) == (kind, payload)


async def test_a_result_to_check_waits_for_its_person_until_checked(store, bus):
    run = reporter(store, bus)
    await run.started()
    ready = await run.review_ready("3 issues assessed", "Assess the issues")
    assert (ready["state"], ready["needs_you"]) == ("review", True)
    assert ready["payload"] == {"summary": "3 issues assessed", "check_in": "Assess the issues"}
    assert store.run("s1")["needs_you"] and store.run("s1")["ended"] is None
    done = store.acknowledge("s1", "alice")
    assert (done["type"], done["state"], done["needs_you"], done["payload"]) == ("run.done", "done", False,
                                                                                  {"checked_by": "alice"})
    assert store.acknowledge("s1", "alice") is None


async def test_a_failure_asks_for_its_person_until_seen_and_stays_failed(store, bus):
    run = reporter(store, bus)
    await run.started()
    failed = await run.failed("no model answered")
    assert failed["needs_you"] is True
    seen = store.acknowledge("s1", "alice")
    assert (seen["type"], seen["state"], seen["needs_you"]) == ("run.acknowledged", "failed", False)
    assert store.run("s1")["needs_you"] is False and store.acknowledge("s1", "alice") is None


async def test_a_run_that_waits_for_its_person_is_never_silent(store, bus, monkeypatch):
    run = reporter(store, bus)
    await run.started()
    await run.review_ready("done")
    later = time.time() + run_events.SILENT_S + 1
    monkeypatch.setattr(run_events.time, "time", lambda: later)
    assert store.run("s1")["silent"] is False


async def test_a_done_or_working_run_waits_for_nobody(store, bus):
    run = reporter(store, bus)
    await run.started()
    assert store.acknowledge("s1", "alice") is None
    await run.done()
    assert store.acknowledge("s1", "alice") is None and store.run("s1")["needs_you"] is False


async def test_an_answered_run_asks_again_only_if_it_fails_again(store, bus):
    first = reporter(store, bus)
    await first.started()
    await first.failed("x")
    store.acknowledge("s1", "alice")
    again = reporter(store, bus)
    await again.started()
    assert store.run("s1")["acked"] == 0
    await again.failed("y")
    assert store.run("s1")["needs_you"] is True


def test_a_store_of_stage_one_gets_the_new_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE agent_runs (run_id TEXT PRIMARY KEY, agent TEXT NOT NULL, who TEXT NOT NULL, "
                   "title TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', task TEXT NOT NULL DEFAULT '', "
                   "state TEXT NOT NULL, started TEXT NOT NULL, updated TEXT NOT NULL, ended TEXT, "
                   "last_seq INTEGER NOT NULL DEFAULT 0, step_index INTEGER, step_total INTEGER, "
                   "step_label TEXT NOT NULL DEFAULT '', step_key TEXT NOT NULL DEFAULT '', "
                   "step_labels TEXT NOT NULL DEFAULT '', progress REAL, eta_s INTEGER, updated_mono REAL)")
        db.execute("INSERT INTO agent_runs (run_id, agent, who, state, started, updated) "
                   "VALUES ('old', 'a', 'alice', 'failed', 'x', 'x')")
    store = RunEventStore(path)
    assert store.run("old")["needs_you"] is True


async def test_a_reviewed_task_ends_waiting_for_its_person(store, bus):
    run = reporter(store, bus)
    await run.started()
    await report_end(run, {"state": "done", "summary": "2 proposed"}, review=True, check_in="Assess the issues")
    last = store.events("s1")[-1]
    assert (last["type"], last["payload"]["check_in"]) == ("run.review_ready", "Assess the issues")


async def test_a_cancelled_run_says_who_cancelled(store, bus):
    run = reporter(store, bus)
    await run.started()
    await report_end(run, {"state": "cancelled", "cancelled_by": "alice"})
    assert store.events("s1")[-1]["payload"] == {"by": "alice"}


async def test_a_question_waits_with_the_run_and_its_answer_puts_it_back_to_work(store, bus, monkeypatch):
    run = reporter(store, bus)
    await run.started()
    await run.step("Reading the findings")
    await run.needs_input("Is the gateway reachable from the internet?", question_id="q1",
                          answer_url="/plugins/ask_user/answer", options=["Yes", "No"], wait_s=1800)
    waiting = store.run("s1")
    assert (waiting["state"], waiting["needs_you"], waiting["ended"]) == ("needs_input", True, None)
    assert waiting["question"] == {"question": "Is the gateway reachable from the internet?", "options": ["Yes", "No"],
                                   "multi_select": False, "question_id": "q1",
                                   "answer_url": "/plugins/ask_user/answer", "wait_s": 1800}
    assert [r["question"]["question_id"] for r in store.runs("alice")] == ["q1"]
    await run.heartbeat()   # a sign of life does not take the question away
    later = time.time() + run_events.SILENT_S + 1
    monkeypatch.setattr(run_events.time, "time", lambda: later)
    assert store.run("s1")["question"]["question_id"] == "q1" and store.run("s1")["silent"] is False
    monkeypatch.undo()
    await run.answered("alice")
    back = store.run("s1")
    assert (back["state"], back["needs_you"], back["step_label"]) == ("working", False, "Reading the findings")
    assert "question" not in back and store.events("s1")[-1]["payload"] == {"outcome": "answered", "by": "alice"}
    await run.done()
    assert await run.needs_input("Too late?", question_id="q2", answer_url="/x") is None


async def test_a_run_held_where_this_process_cannot_look_ends_only_once_it_went_quiet(tmp_path, bus, monkeypatch):
    """A server in a container and an agent-cli beside it share the run store, not their process ids: the id a run
    names counts where it was written. Looked for here it names nothing -- and the first told turn of the agent-cli
    ended every run of the server as interrupted. Such a run is gone once it gave no sign of life for a while."""
    from agent_system.plugins.sqlite_guard import connection

    path = tmp_path / "run_events.db"
    here = run_events.OWNER
    assert run_events.owner_visible(here) and run_events.owner_visible("4242:1700000000.000:theirs")
    with monkeypatch.context() as there:      # as a process of another container reads this one's runs
        there.setattr(run_events, "NAMESPACE", "elsewhere")
        assert not run_events.owner_visible(here), "this process's runs do not say where its id counts"
    with monkeypatch.context() as there:
        # no such process here, and none it could be mistaken for
        there.setattr(run_events, "OWNER", "999999:1.000:theirs:elsewhere")
        first = RunEventStore(path)
        await RunReporter("far", "coder", who="alice", source="chat", task="turn", store=first, bus=bus).started()
        await RunReporter("far-quiet", "coder", who="alice", source="chat", task="turn", store=first, bus=bus).started()
    assert not run_events.owner_visible("999999:1.000:theirs:elsewhere")
    assert run_events.OWNER == here
    assert RunEventStore(path).swept == [], "a run of a process that cannot be seen from here was ended on a guess"
    with connection(path) as db:
        db.execute("UPDATE agent_runs SET updated_mono = updated_mono - ? WHERE run_id = 'far-quiet'",
                   (run_events.ORPHAN_S + 1,))
    assert [e["run_id"] for e in RunEventStore(path).swept] == ["far-quiet"]
    runs = {r["run_id"]: r["state"] for r in RunEventStore(path, sweep=False).runs("alice")}
    assert runs == {"far": "working", "far-quiet": "failed"}


async def test_the_store_says_whether_this_process_holds_a_run(store, bus, monkeypatch):
    await reporter(store, bus).started()
    with monkeypatch.context() as other:
        other.setattr(run_events, "OWNER", "999999:1.000:theirs:" + run_events.NAMESPACE)
        await RunReporter("theirs", "coder", who="alice", store=store, bus=bus).started()
    assert store.held_here("s1") and not store.held_here("theirs") and not store.held_here("nobody")


def test_a_run_of_another_process_lives_as_long_as_that_process():
    import subprocess
    import sys

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        owner = f"{run_events._process_key(other.pid)}:theirs"
        assert run_events.owner_alive(owner)
    finally:
        other.kill()
        other.wait()
    assert not run_events.owner_alive(owner) and not run_events.owner_alive("")


async def test_a_run_shown_to_a_person_does_not_name_its_process(store, bus):
    await reporter(store, bus).started()
    assert "owner" not in store.run("s1") and all("owner" not in r for r in store.runs("alice"))


def test_a_process_that_cannot_be_inspected_is_not_taken_for_dead(monkeypatch):
    import psutil

    class Hidden:
        def __init__(self, pid):
            raise psutil.AccessDenied(pid)

    monkeypatch.setattr(run_events.psutil, "Process", Hidden)
    assert run_events.owner_alive("4242:1700000000.000:theirs")


async def test_an_end_the_store_refused_once_is_kept_at_the_next_try(bus, tmp_path, monkeypatch):
    """An end that was not kept left the run at work for as long as its process lived: nothing says an end again."""
    from agent_system import run_events

    class Busy(RunEventStore):
        refused = 0

        def append(self, run_id, type_, **kwargs):
            if type_ == "run.done" and not self.refused:
                self.refused += 1
                raise OSError("database is locked")
            return super().append(run_id, type_, **kwargs)

    monkeypatch.setattr(run_events, "END_RETRY_S", 0.01)
    store = Busy(tmp_path / "busy.db")
    run = reporter(store, bus)
    await run.started()
    await run.done("3 issues")
    assert store.refused == 1, "fixture: the store never refused"
    assert store.run("s1")["state"] == "done"


def _as_another_process(monkeypatch):
    """What is written from here on is another process's, as the store names its runs' owners."""
    from agent_system import run_events
    monkeypatch.setattr(run_events, "OWNER", f"{os.getpid()}:1.000:another")


async def test_a_run_begun_again_starts_its_clock_and_is_named_anew(store, bus, monkeypatch):
    """A session's next turn is the same run: how long it has been at it is this turn's time, what it is about
    this turn's question -- and its events do not pile up for the life of the session."""
    from agent_system import run_events
    monkeypatch.setattr(run_events, "KEEP_EVENTS", 2)
    first = RunReporter("s1", "coder", who="alice", title="the first question", source="chat", task="turn",
                        store=store, bus=bus)
    await first.started()
    for label in ("a", "b", "c"):
        await first.step(label)
    await first.done("one")
    before = store.run("s1")
    await asyncio.sleep(0.01)
    again = RunReporter("s1", "reviewer", who="alice", title="the second question", source="sub_agent", task="step",
                        parent="above", store=store, bus=bus)
    await again.started()
    run = store.run("s1")
    assert (run["title"], run["agent"], run["state"]) == ("the second question", "reviewer", "working")
    assert (run["source"], run["task"], run["parent_session"]) == ("sub_agent", "step", "above")
    assert run["started"] > before["started"]
    assert [e["seq"] for e in store.events("s1")] == [5, 6], "the run kept every event of every turn"
    await again.done("two")
    # gone on in the chat by its person: a turn of its own, no longer below the session that once called it
    own = RunReporter("s1", "reviewer", who="alice", title="the third", source="chat", task="turn", store=store, bus=bus)
    await own.started()
    assert store.run("s1")["parent_session"] == ""
    await own.done("three")
    # any other run keeps what it works below where its restart does not say (a panel's task begun again)
    work = RunReporter("w1", "claude_code", who="alice", source="background", task="background", parent="s1", store=store, bus=bus)
    await work.started()
    await work.done("ok")
    more = RunReporter("w1", "claude_code", who="alice", source="background", task="background", store=store, bus=bus)
    await more.started()
    assert store.run("w1")["parent_session"] == "s1"
    await more.done("ok")


async def test_turns_and_the_other_runs_are_kept_apart_and_none_at_work_is_dropped(store, bus, monkeypatch):
    """The newest runs are kept: counted together, a day of chat turns pushed out the panel run that waited for
    its person -- and a run pruned while it worked said the rest of its life to nobody."""
    from agent_system import run_events
    monkeypatch.setattr(run_events, "MAX_RUNS", 2)
    held = RunReporter("held", "coder", who="alice", source="chat", task="turn", store=store, bus=bus)
    await held.started()
    waits = RunReporter("panel-0", "code_analyst", who="alice", source="code_audit", task="assess", store=store,
                        bus=bus)
    await waits.started()
    await waits.review_ready("please check")      # ended? no: it waits for its person, and stays
    for n in range(4):
        turn = RunReporter(f"turn-{n}", "coder", who="alice", source="chat", task="turn", store=store, bus=bus)
        await turn.started()
        await turn.done("ok")
        await asyncio.sleep(0.005)
    for n in range(1, 4):
        panel = RunReporter(f"panel-{n}", "code_analyst", who="alice", source="code_audit", task="assess",
                            store=store, bus=bus)
        await panel.started()
        await panel.done("ok")
        await asyncio.sleep(0.005)
    last = RunReporter("turn-9", "coder", who="alice", source="chat", task="turn", store=store, bus=bus)
    await last.started()
    kept = {run["run_id"] for run in store.runs(None, 50)}
    assert kept == {"held", "turn-9", "turn-2", "turn-3", "panel-0", "panel-2", "panel-3"}, sorted(kept)
    assert await held.step("still here") is not None
    await held.done("ok")
    await last.done("ok")


async def test_a_run_another_process_holds_gives_its_sign_of_life(store, bus, monkeypatch):
    """A heartbeat goes to the streams of the process that sends it, and is not kept: a stream elsewhere reads
    that the run is alive from the run itself -- only of those at work, and not of its own."""
    mine = reporter(store, bus, "mine")
    await mine.started()
    _as_another_process(monkeypatch)
    theirs = RunReporter("theirs", "coder", who="alice", title="their turn", source="chat", task="turn",
                         parent="above", store=store, bus=bus)
    await theirs.started()
    await theirs.step("Thinking")
    gone = RunReporter("gone", "coder", who="alice", store=store, bus=bus)
    await gone.started()
    monkeypatch.undo()
    after = time.time()
    await asyncio.sleep(0.02)
    assert store.beats("alice", after) == [], "a run that said nothing since gave a sign of life"
    await mine.heartbeat()
    _as_another_process(monkeypatch)
    await theirs.heartbeat()
    await gone.done("ok")
    monkeypatch.undo()
    [beat] = store.beats("alice", after)
    assert (beat["run_id"], beat["type"], beat["state"], beat["cursor"]) == ("theirs", "run.heartbeat", "working", None)
    assert beat["step"]["label"] == "Thinking" and beat["parent_session"] == "above"
    assert store.beats("someone else", after) == []
    await mine.done("ok")


async def test_an_end_that_cannot_wait_is_written_by_a_thread_of_its_own(bus, tmp_path, monkeypatch):
    from agent_system import run_events

    class Busy(RunEventStore):
        refused = 0

        def append(self, run_id, type_, **kwargs):
            if type_ == "run.failed" and not self.refused:
                self.refused += 1
                raise OSError("database is locked")
            return super().append(run_id, type_, **kwargs)

    monkeypatch.setattr(run_events, "END_RETRY_S", 0.01)
    store = Busy(tmp_path / "busy.db")
    run = reporter(store, bus)
    await run.started()
    run.keep_alive()
    queue = bus.subscribe()
    thread = run.end_detached(*run.ended_payload("failed", "the model is down"), seen=True)
    thread.join(5)
    assert store.refused == 1, "fixture: the store never refused"
    row = store.run("s1")
    assert (row["state"], row["needs_you"]) == ("failed", False)
    assert store.events("s1")[-1]["payload"] == {"reason": "the model is down"}
    assert (await asyncio.wait_for(queue.get(), 2))["type"] == "run.failed", "the end never reached the streams"
    assert run.ended and run._alive is None and reporter_for("s1") is None
    assert run.end_detached("run.done") is None, "a run ended twice"


async def test_a_reporter_that_is_not_listed_is_found_by_nobody(store, bus):
    own = reporter(store, bus, "s1", listed=False)
    await own.started()
    assert reporter_for("s1") is None
    await own.done("ok")


async def test_a_failure_nobody_saw_stays_at_the_top_of_the_list(store, bus):
    """The list a page starts with is cut: a failure not seen yet has ended, and among the ended runs it went out
    of the list with the next few turns -- gone from the pill and from what needs you after a reload."""
    failed = reporter(store, bus, "old-failure")
    await failed.started()
    await failed.failed("no access")
    seen = reporter(store, bus, "seen-failure")
    await seen.started()
    await seen.failed("no access")
    store.acknowledge("seen-failure", "alice")
    for n in range(3):
        await asyncio.sleep(0.005)
        run = reporter(store, bus, f"later-{n}")
        await run.started()
        await run.done("ok")
    at_work = reporter(store, bus, "at-work")
    await at_work.started()
    listed = [run["run_id"] for run in store.runs("alice", 3)]
    assert listed == ["at-work", "old-failure", "later-2"], listed
    await at_work.done("ok")


async def test_a_turn_stays_in_the_list_whatever_it_called(store, bus):
    """The list a page starts with is cut. A turn that called many sub-agents began before all of them: counted
    with them, it was cut -- and they are shown nowhere once they have ended."""
    turn = RunReporter("turn", "coder", who="alice", source="chat", task="turn", store=store, bus=bus)
    await turn.started()
    for n in range(4):
        await asyncio.sleep(0.005)
        sub = RunReporter(f"sub-{n}", "explorer", who="alice", source="sub_agent", task="turn", parent="turn",
                          store=store, bus=bus)
        await sub.started()
        if n in (1, 2):
            await sub.done("ok")
    await asyncio.sleep(0.005)
    later = RunReporter("later", "coder", who="alice", source="chat", task="turn", store=store, bus=bus)
    await later.started()
    await later.done("ok")
    await asyncio.sleep(0.005)
    await sub.done("ok")      # the last sub-agent ends after that turn: counted, it would stand before it
    await asyncio.sleep(0.005)
    await turn.done("ok")
    # at work first (the sub-agent still going), then what ended -- the one that ended last first, whenever it began
    assert [run["run_id"] for run in store.runs("alice", 3)] == ["sub-0", "turn", "later"]
    assert store.run("sub-1")["state"] == "done", "an ended sub-agent is still there to be read by its id"


async def test_a_run_that_waits_says_why_and_since_when(store, bus):
    """What needs its person is listed with what it wants -- after a reload too, when the event that said so is
    long past: the question and since when, why a run failed."""
    asks = reporter(store, bus, "asks")
    await asks.started()
    asked = await asks.needs_input("Which branch?", question_id="q1", answer_url="/answer")
    await asks.heartbeat()
    failed = reporter(store, bus, "failed")
    await failed.started()
    await failed.failed("the model is down")
    listed = {run["run_id"]: run for run in store.runs("alice")}
    assert listed["asks"]["question"]["question"] == "Which branch?" and listed["asks"]["asked"] == asked["ts"]
    assert listed["failed"]["reason"] == "the model is down" and store.run("failed")["reason"] == "the model is down"
    store.acknowledge("failed", "alice")
    assert store.run("failed")["reason"] == "the model is down", "seen, it no longer says why"
    await asks.answered("alice")
    await asks.done("ok")
