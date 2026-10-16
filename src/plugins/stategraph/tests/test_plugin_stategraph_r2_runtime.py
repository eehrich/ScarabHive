"""Runtime review before the commit (2026-10-16): what costs the event loop or recurses without end.

RT-1 the semaphore cleanup looks up each holder's run instead of walking every run; RT-2 describe and budget parse
a run's definition once between them, and the get_run tool reads off the loop; RT-3 a resume loads its snapshot
off the loop, and a second resume of the same run meanwhile is refused; RT-4 a pause passed down a caller cycle
ends.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import plugins.stategraph.engine.runner as runner_module
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, Harness, seed_run, until, utc_at
from plugins.stategraph.tests.test_plugin_stategraph_r15_p5_followups import (  # noqa: F401 (fixtures)
    _parent_with_child, gates, server)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

PLAIN = """\
stategraph: 1
id: m
limits: {max_cost: 2.5}
params:
  engine: {type: string, default: agent}
initial: done
states:
  done: {type: final}
"""

WAITING = """\
stategraph: 1
id: m
events: {go: {}}
initial: wait
states:
  wait:
    transitions:
      - trigger: go
        target: done
  done: {type: final}
"""


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


def _counting_loads(monkeypatch) -> list[int]:
    """The thread of each load_snapshot the runner makes."""
    threads: list[int] = []
    real = runner_module.load_snapshot
    monkeypatch.setattr(runner_module, "load_snapshot",
                        lambda *a, **k: threads.append(threading.get_ident()) or real(*a, **k))
    return threads


# ------------------------------------------------------------------ RT-1

def test_rt1_the_semaphore_cleanup_looks_up_each_holders_run_and_drops_only_ended_and_deleted_ones(harness):
    store = harness.store
    for run_id, status in (("ended", "succeeded"), ("waits", "interrupted"), ("me", "running")):
        store.create_run(run_id, "m", {}, owner="o", lease_until=utc_at(60), status=status)
    for run_id in ("ended", "gone", "waits"):  # the rows as earlier acquires left them
        store._db().execute("INSERT INTO semaphore_holders (name, sem_key, slot, run_id, key, frame, since)"
                            " VALUES ('t', '', ?, ?, 's0', '', ?)", ({"ended": 0, "gone": 1, "waits": 2}[run_id],
                                                                     run_id, utc_at(0)))
    statements: list[str] = []
    store._db().set_trace_callback(statements.append)
    try:
        slot = store.acquire_slot("t", 3, "me", "s0", "", cutoff=utc_at(-300))
    finally:
        store._db().set_trace_callback(None)

    assert slot == 0, "the ended run's slot is free again"
    assert sorted(h["run_id"] for h in store.semaphore_holders("t")) == ["me", "waits"]
    [cleanup] = [s for s in statements if s.lstrip().upper().startswith("DELETE FROM SEMAPHORE_HOLDERS")]
    plan = store._db().execute(f"EXPLAIN QUERY PLAN {cleanup}", ("t",) if "?" in cleanup else ()).fetchall()
    details = [row["detail"] for row in plan]
    assert not any(d.startswith("SCAN runs") for d in details), details  # every run, on every try of every waiter


# ------------------------------------------------------------------ RT-2

def test_rt2_describe_and_budget_parse_a_runs_definition_once_between_them(harness, monkeypatch):
    seed_run(harness.store, "r", {"m.yaml": PLAIN}, owner="gone:1:x", lease=-1, status="interrupted")
    manager = harness.manager()
    threads = _counting_loads(monkeypatch)

    assert manager.describe("r")["param_defaults"] == {"engine": "agent"}
    assert manager.budget("r") == (2.5, 0.0)
    assert manager.describe("r")["param_defaults"] == {"engine": "agent"}
    assert len(threads) == 1, f"parsed {len(threads)} times"


def test_rt2_a_fork_tells_its_own_limit_and_its_origins_defaults(harness, monkeypatch):
    from plugins.stategraph.tests.stategraph_testkit import runnable

    for run_id, cost, default, parent in (("origin", "1.0", "agent", None), ("fork", "4.0", "claude", "origin")):
        text = PLAIN.replace("max_cost: 2.5", f"max_cost: {cost}").replace("default: agent", f"default: {default}")
        harness.store.create_run(run_id, "m", runnable({"m.yaml": text}).snapshot(), params={}, mocks={},
                                 owner="gone:1:x", lease_until=utc_at(-1), status="interrupted",
                                 session_id=f"sg_{run_id}", parent_run=parent)
    manager = harness.manager()

    assert manager.budget("fork")[0] == 4.0
    assert manager.describe("fork")["param_defaults"] == {"engine": "agent"}


async def test_rt2_the_get_run_tool_reads_the_run_off_the_event_loop(server, monkeypatch):
    from plugins.stategraph.service import ServiceError

    threads: list[int] = []

    def read(*args, **kwargs):
        threads.append(threading.get_ident())
        raise ServiceError(404, "no run 'x'")

    monkeypatch.setattr(server.service, "get_run", read)
    await server.get_run({"run_id": "x"})

    assert threads and threading.get_ident() not in threads, "read on the event loop"


async def test_rt2_control_run_reads_its_answer_off_the_event_loop(server, gates):
    parent, _child = await _parent_with_child(server)
    threads: list[int] = []
    real = server.service.get_run

    def read(*args, **kwargs):
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    server.service.get_run = read  # the instance's: control_run reads its answer through self.get_run
    try:
        answer = await server.service.control_run(parent, "pause")
    finally:
        del server.service.get_run

    assert answer["id"] == parent
    assert threads and threading.get_ident() not in threads, "the answer read on the event loop"
    await server.service.control_run(parent, "continue")


# ------------------------------------------------------------------ RT-3

async def test_rt3_a_resume_loads_its_snapshot_off_the_loop_and_a_second_one_meanwhile_is_refused(harness,
                                                                                                    monkeypatch):
    seed_run(harness.store, "r", {"m.yaml": WAITING}, owner="gone:1:x", lease=-1, status="interrupted")
    manager = harness.manager()
    threads = _counting_loads(monkeypatch)

    first, second = await asyncio.gather(manager.resume("r"), manager.resume("r"), return_exceptions=True)

    assert first == "r", first
    assert isinstance(second, ValueError) and "active in this process" in str(second), second
    assert threads and threading.get_ident() not in threads, "the snapshot loaded on the event loop"
    await until(lambda: harness.store.get_run("r")["status"] == "waiting", what="the resumed run waits for go")


# ------------------------------------------------------------------ RT-4

async def test_rt4_a_pause_down_a_caller_cycle_ends(server, gates):
    parent, child = await _parent_with_child(server)
    # what a resume by run_key leaves when the child resumes its own caller: each is the other's caller
    server.run_store.update_run(parent, caller_run=child, caller_request=f"{child}_001")
    live = server.run_manager.live

    def halted(run_id: str) -> bool:  # pending, or taken: a pause that halts at a hook goes back to mode "run"
        debugger = live[run_id].ctx.debugger
        return debugger.mode == "pause" or debugger.paused is not None

    await server.service.control_run(parent, "pause")
    assert halted(parent) and halted(child)
    await server.service.control_run(parent, "continue")
    assert live[child].ctx.debugger.mode == "run"
