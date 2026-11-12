"""Final review, engine: run.cost over a resumed retry, nested slots of a replayed composite, a stopped run's set
that meets an error, and budget() after the run (ENG-1 .. ENG-4)."""

from __future__ import annotations

import asyncio

import pytest

from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, activity_rows, held,
                                                         resume, runnable, settle, until)
from plugins.stategraph.tests.test_plugin_stategraph_r15_life_lifecycle import (  # noqa: F401
    gate, make_server, servers)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


# ------------------------------------------------------------------ ENG-1
RETRIED = """\
stategraph: 1
id: m
python: m.py
imports: {sub: ./sub.yaml}
initial: j
states:
  j:
    do:
      parallel:
        a: {call: spend}
        b: {machine: sub}
      retry: {attempts: 2, backoff: 0, errors: [call_failed, join_failed]}
    transitions: [{target: done}, {trigger: error, target: done}]
  done: {type: final, output: "{{ run.cost }}"}
"""

SUB = """\
stategraph: 1
id: sub
context: {c: null}
initial: one
states:
  one:
    do: {tool: quick, idempotent: true}
    transitions: [{target: two, effect: ctx.c = run.cost}]
  two:
    do: {tool: slow, idempotent: true}
    transitions: [{target: end}]
  end: {type: final}
"""

SPEND = """\
from plugins.stategraph.engine.backend import CALL_SPEND

calls = []


async def spend():
    calls.append(1)
    if len(calls) == 1:
        CALL_SPEND.get().cost += 0.5
        raise RuntimeError("first")
    return len(calls)
"""


async def test_a_resume_in_a_retry_counts_what_the_earlier_attempts_spent(harness):
    """Attempt 1 spent 0.5 and failed; the crash comes while attempt 2's submachine waits in its second state,
    whose first one recorded ctx.c = run.cost. The resume counts attempt 1 again: the step replays, run.cost ends
    at what the live run saw."""
    manager = harness.manager()
    gate = asyncio.Event()
    backend = FakeBackend({"j/b/one": "ok", "j/b/two": held(gate)})
    run_id = await manager.start(runnable({"m.yaml": RETRIED, "sub.yaml": SUB, "m.py": SPEND}), backend=backend)
    await until(lambda: any(key.startswith("s0/a2/") and row["status"] == "started" and row["data"]["path"] == "j/b/two"
                            for key, row in activity_rows(harness.store, run_id).items()),
                what="attempt 2's submachine in its second state")
    assert manager.live[run_id].ctx.cost == 0.5
    await manager.shutdown()
    row = await resume(harness, run_id, FakeBackend({"j/b/one": "ok", "j/b/two": "ok"}))
    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == 0.5


# ------------------------------------------------------------------ ENG-2
NESTED = """\
stategraph: 1
id: m
context: {slot: null}
initial: take
states:
  take:
    do:
      parallel:
        a: {acquire: p}
        b: {agent: w, task: side}
    transitions: [{target: work}, {trigger: error, target: work}]
  work:
    do: {agent: w, task: go}
    transitions: [{target: done}]
  done: {type: final}
"""


@pytest.mark.parametrize("side", ["ok", ActivityError("agent_failed", "no")], ids=["done", "failed"])
async def test_a_fork_holds_the_slot_a_branch_of_its_prefix_took(harness, side):
    """The parallel replays from its recorded outcome (done, or the join's error), never branch by branch: the
    acquire inside it claims its slot all the same, so the fork waits for the source to give it back."""
    manager = harness.manager()
    manager.semaphores = {"p": 1}
    gate = asyncio.Event()
    source = await manager.start(runnable({"m.yaml": NESTED}), backend=FakeBackend({"work": held(gate),
                                                                                   "take/b": side}))
    await until(lambda: activity_rows(harness.store, source).get("s1", {}).get("status") == "started",
                what="the source works with slot 0")
    backend = FakeBackend({"work": "done", "take/b": side})
    fork = await manager.fork(source, at_step=1, backend=backend)
    await asyncio.sleep(1.0)
    ran = backend.count("work")
    gate.set()
    await settle(manager, source)
    await settle(manager, fork)
    assert ran == 0, "the fork worked beside the source on the same slot"
    assert backend.count("work") == 1, "the fork never went on once the source gave the slot back"


# ------------------------------------------------------------------ ENG-3
FRAGILE = """\
stategraph: 1
id: fragile
python: hold.py
context: {x: 0, seen: null}
initial: work
states:
  work:
    do: {call: work, args: {x: 1}, idempotent: false}
    transitions:
      - {target: done, effect: ctx.seen = out}
      - {trigger: error, target: done, effect: ctx.seen = error.type}
  done: {type: final, output: {x: "{{ ctx.x }}", seen: "{{ ctx.seen }}"}}
"""


async def test_a_set_on_a_stopped_run_holds_when_its_activity_ends_with_an_error(servers, gate, tmp_path):
    """The set waits at the exit of the activity in flight (service._frontier); not idempotent, that activity ends
    interrupted on the resume -- the error hook applies the set all the same."""
    (tmp_path / "machines").mkdir(exist_ok=True)
    first = servers()
    (tmp_path / "machines" / "fragile.yaml").write_text(FRAGILE, encoding="utf-8")
    second = servers()
    run_id = (await first.service.start_run("fragile"))["run_id"]
    await until(lambda: any(r["key"] == "s0" and r["status"] == "started"
                            for r in first.run_store.rows(run_id, kinds=("activity",))), what="s0 running")
    await first.run_manager.shutdown()
    await second.service.control_run(run_id, "set", path="ctx.x", expr="41")
    await second.service.control_run(run_id, "resume")
    row = await settle(second.run_manager, run_id)
    assert row["output"] == {"x": 41, "seen": "interrupted"}, row["output"]


# ------------------------------------------------------------------ ENG-4
CUT = """\
stategraph: 1
id: m
python: m.py
initial: j
states:
  j:
    do:
      parallel:
        a: {call: fail_later}
        b: {call: spend_and_hang}
      fail: fast
    transitions: [{target: done}, {trigger: error, target: done}]
  done: {type: final, output: "{{ run.cost }}"}
"""

HANG = """\
import asyncio
from plugins.stategraph.engine.backend import CALL_SPEND


async def fail_later():
    await asyncio.sleep(0.2)
    raise RuntimeError("no")


async def spend_and_hang():
    CALL_SPEND.get().cost += 0.7
    await asyncio.sleep(3600)
"""


async def test_budget_after_the_run_counts_what_a_stop_cut(harness):
    """A fail-fast join cut b after it spent 0.7: its started row keeps that money, no outcome took it over, so
    run.cost -- what expressions read -- never saw it. limits.max_cost counts it (RunContext.spent), and budget()
    says what the limit counts, live and after the end alike."""
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": CUT, "m.py": HANG}))
    row = await settle(manager, run_id)
    assert row["output"] == 0.0  # run.cost as the run's own code saw it
    assert activity_rows(harness.store, run_id)["s0/b.b"]["data"]["meta"]["cost"] == 0.7
    assert manager.budget(run_id) == (None, 0.7)
    assert (harness.store.spend(run_id) or {}).get("cost") == 0.7
