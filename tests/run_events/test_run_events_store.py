"""Run events (agent_system.run_events): a run's events are numbered without gaps, carry its state and its step in
plain words, end once, and show a run that went quiet as silent."""
import asyncio
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
    await RunReporter("asking", "code_analyst", who="alice", store=first, bus=bus).needs_input(
        "Which one?", question_id="q1", answer_url="/plugins/ask_user/answer")
    await RunReporter("checking", "code_analyst", who="alice", store=first, bus=bus).review_ready("look")
    await RunReporter("done", "code_analyst", who="alice", store=first, bus=bus).done()
    # the same process: nothing is ended
    assert RunEventStore(path).swept == []
    # the next interpreter -- here one that re-executed in place (os.execv: same id, same start time): what was at
    # work or asking is cut off; a result to check and an end stay
    monkeypatch.setattr(run_events, "OWNER", run_events.OWNER.rsplit(":", 1)[0] + ":another")
    swept = RunEventStore(path).swept
    assert sorted(e["run_id"] for e in swept) == ["asking", "working"]
    runs = {r["run_id"]: r for r in RunEventStore(path).runs("alice")}
    assert (runs["working"]["state"], runs["working"]["needs_you"]) == ("failed", True)
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
