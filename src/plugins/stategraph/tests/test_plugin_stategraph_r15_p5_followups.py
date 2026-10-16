"""Review 2026-10-15, phase 5 (engine): the last parts of FEAT-2 (a semaphore key: one run per ticket), FEAT-1
(a parent's pause, continue and end reach the runs it started) and FEAT-6 (get_run carries the run's own
limits.max_cost and run.cost).

Engine tests go through the real RunManager on a RunStore under tmp_path (FakeBackend behind it); the child-run
tests through the real StateGraphServer, whose call activities start their children with service.start_run.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, activity_rows, held,
                                                         runnable, seed_run, settle, tool_config, until, utc_at)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


# ------------------------------------------------------------------ FEAT-2: a semaphore key

KEYED = """\
stategraph: 1
id: m
params: {ticket: {type: string}}
context: {slot: null}
initial: take
states:
  take:
    do: {acquire: t, key: "{{ params.ticket }}"}
    transitions: [{target: work, effect: ctx.slot = out}]
  work:
    do: {agent: w, task: go}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.slot }}"}
"""

TWO_KEYS = """\
stategraph: 1
id: m
context: {back: null}
initial: take_a
states:
  take_a:
    do: {acquire: t, key: a}
    transitions: [{target: take_b}]
  take_b:
    do: {acquire: t, key: b}
    transitions: [{target: give_a}]
  give_a:
    do: {release: t, key: a}
    transitions: [{target: work, effect: ctx.back = out}]
  work:
    do: {agent: w, task: go}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.back }}"}
"""


def keyed(manager, hold: float = 300.0):
    manager.semaphores = {"t": 1}
    manager.semaphore_hold = hold
    return manager


def working(store, run_id: str):
    return lambda: activity_rows(store, run_id).get("s1", {}).get("status") == "started"


async def test_feat2_a_keyed_semaphore_counts_its_capacity_per_key(harness):
    manager = keyed(harness.manager())
    gate = asyncio.Event()
    first = await manager.start(runnable({"m.yaml": KEYED}), params={"ticket": "KAN-1"},
                                backend=FakeBackend({"work": held(gate)}))
    await until(working(harness.store, first), what="KAN-1 holds the only slot of its key")

    other = await manager.start(runnable({"m.yaml": KEYED}), params={"ticket": "KAN-2"},
                                backend=FakeBackend({"work": "done"}))
    row = await settle(manager, other)  # another ticket: a slot of its own key
    assert row["status"] == "succeeded" and row["output"] == 0, row["error"]

    same = await manager.start(runnable({"m.yaml": KEYED}), params={"ticket": "KAN-1"},
                               backend=FakeBackend({"work": "done"}))
    await until(lambda: manager.live[same].ctx.status == "waiting", what="a second run of KAN-1 waits")
    waits = harness.store.get_run(same)["view"]["slot_waits"]
    assert [(w["semaphore"], w.get("sem_key")) for w in waits] == [("t", "KAN-1")], waits
    assert [(h["run_id"], h["sem_key"]) for h in harness.store.semaphore_holders("t")] == [(first, "KAN-1")]

    gate.set()
    assert (await settle(manager, first))["status"] == "succeeded"
    row = await settle(manager, same)
    assert row["status"] == "succeeded" and row["output"] == 0, row["error"]


async def test_feat2_a_keyed_release_gives_back_only_that_keys_slot(harness):
    manager = keyed(harness.manager())
    gate = asyncio.Event()
    run_id = await manager.start(runnable({"m.yaml": TWO_KEYS}), backend=FakeBackend({"work": held(gate)}))
    await until(lambda: activity_rows(harness.store, run_id).get("s3", {}).get("status") == "started",
                what="the run works after its release of key a")

    assert [h["sem_key"] for h in harness.store.semaphore_holders("t")] == ["b"]
    gate.set()
    row = await settle(manager, run_id)
    assert row["status"] == "succeeded" and row["output"] == 0, row["error"]
    assert harness.store.semaphore_holders("t") == []


async def test_feat2_a_resumed_keyed_holder_waits_for_its_keys_slot_another_run_took(harness):
    crashed = keyed(harness.manager())
    holder = await crashed.start(runnable({"m.yaml": KEYED}), params={"ticket": "KAN-1"},
                                 backend=FakeBackend({"work": held(asyncio.Event())}))
    await until(working(harness.store, holder), what="the holder works with its slot")
    await crashed.shutdown()

    other = keyed(harness.manager(), hold=0)  # its hold has run out: another run of KAN-1 takes the slot
    gate = asyncio.Event()
    taker = await other.start(runnable({"m.yaml": KEYED}), params={"ticket": "KAN-1"},
                              backend=FakeBackend({"work": held(gate)}))
    await until(working(harness.store, taker), timeout=3, what="the other run takes KAN-1's slot")

    back = keyed(harness.manager(), hold=0)
    await back.resume(holder, backend=FakeBackend({"work": "done"}))
    await until(lambda: back.live[holder].ctx.status == "waiting", what="the resumed holder waits for its slot")
    waits = harness.store.get_run(holder)["view"]["slot_waits"]
    assert [(w["semaphore"], w.get("sem_key"), w["slot"]) for w in waits] == [("t", "KAN-1", 0)], waits
    gate.set()

    assert (await settle(other, taker))["status"] == "succeeded"
    row = await settle(back, holder)
    assert row["status"] == "succeeded" and row["output"] == 0, row["error"]
    slots = [r["data"] for r in harness.store.rows(holder) if r["kind"] == "lifecycle" and r["status"] == "slots"]
    assert [[(h["name"], h.get("sem_key")) for h in r["held"]] for r in slots] == [[("t", "KAN-1")]], slots


def test_feat2_a_holders_table_from_before_the_key_gets_it_when_opened(tmp_path):
    from plugins.stategraph.engine.journal import RunStore

    path = tmp_path / "runs.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE semaphore_holders (name TEXT NOT NULL, slot INTEGER NOT NULL, run_id TEXT NOT NULL,"
                " key TEXT NOT NULL, frame TEXT NOT NULL, since TEXT NOT NULL, PRIMARY KEY (run_id, key))")
    old.execute("INSERT INTO semaphore_holders VALUES ('t', 0, 'r', 's0', '', 'x')")
    old.commit()
    old.close()

    store = RunStore(path)
    try:
        before = store.semaphore_holders("t")
        store.create_run("r2", "m", {}, owner="o", lease_until=utc_at(60))
        slot = store.acquire_slot("t", 1, "r2", "s0", "", cutoff=utc_at(-300), sem_key="KAN-1")
        after = store.semaphore_holders("t")
    finally:
        store.close()
    assert [(h["run_id"], h["sem_key"]) for h in before] == [("r", "")], before
    assert slot == 0  # (the row of the run that is gone held nothing: dropped by the acquire)
    assert [(h["run_id"], h["sem_key"]) for h in after] == [("r2", "KAN-1")], after


# ------------------------------------------------------------------ FEAT-1: a parent's pause, continue and end

PARENT = """\
stategraph: 1
id: parent
python: parent.py
initial: run
states:
  run:
    do: {call: run, args: {}}
    transitions: [{target: done}]
  done: {type: final}
"""

PARENT_PY = """\
import asyncio
import os


async def run(sg):
    await sg.live["service"].start_run("child", run_key=sg.live.get("key"))
    while not os.path.exists(os.environ["SG_P5_PARENT_GATE"]):
        await asyncio.sleep(0.01)
    if sg.live.get("fail"):
        raise sg.Error("call_failed", "stopped for now", resumable=True)
"""

CHILD = """\
stategraph: 1
id: child
python: child.py
initial: work
states:
  work:
    do: {call: work, args: {}}
    transitions: [{target: next}]
  next:
    do: {call: quick, args: {}}
    transitions: [{target: done}]
  done: {type: final}
"""

CHILD_PY = """\
import asyncio
import os


async def work():
    while not os.path.exists(os.environ["SG_P5_CHILD_GATE"]):
        await asyncio.sleep(0.01)
    return 1


def quick():
    return 2
"""


@pytest.fixture
def gates(tmp_path, monkeypatch):
    """The files whose existence lets the parent's call and the child's work end."""
    parent, child = tmp_path / "parent_gate", tmp_path / "child_gate"
    monkeypatch.setenv("SG_P5_PARENT_GATE", str(parent))
    monkeypatch.setenv("SG_P5_CHILD_GATE", str(child))
    return parent, child


@pytest.fixture
async def server(tmp_path, gates):
    from agent_system.config.models import AgentSystemConfig

    made = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    machines = tmp_path / "machines"
    for name, text in (("parent.yaml", PARENT), ("parent.py", PARENT_PY), ("child.yaml", CHILD),
                       ("child.py", CHILD_PY)):
        (machines / name).write_text(text, encoding="utf-8")
    yield made
    gates[0].touch()
    gates[1].touch()
    await made.stop_plugin()


async def _parent_with_child(server, **live) -> tuple[str, str]:
    parent = (await server.service.start_run("parent", live={"service": server.service, **live}))["run_id"]
    await until(lambda: server.run_store.children(parent), what="the child started")
    [child] = server.run_store.children(parent)
    await until(lambda: child["id"] in server.run_manager.live, what="the child runs here")
    return parent, child["id"]


async def test_feat1_a_parents_pause_and_continue_reach_its_live_child_and_not_the_other_way(server, gates):
    parent, child = await _parent_with_child(server)
    live = server.run_manager.live

    def halted(run_id: str) -> bool:
        # pending, or taken: control_run reads its answer off the loop, and a child not yet in its work halts at a
        # hook meanwhile -- a pause that halts goes back to mode "run"
        debugger = live[run_id].ctx.debugger
        return debugger.mode == "pause" or debugger.paused is not None

    await server.service.control_run(child, "pause")  # a child's pause is its own
    assert not halted(parent) and halted(child)
    await server.service.control_run(child, "continue")

    await server.service.control_run(parent, "pause")
    assert halted(child)
    gates[1].touch()  # the child's work ends: its pause takes effect at the next hook
    await until(lambda: server.run_store.get_run(child)["status"] == "paused", what="the child pauses")

    await server.service.control_run(parent, "continue")
    await until(lambda: server.run_store.get_run(child)["status"] == "succeeded", what="the child goes on")


async def test_feat1_a_parent_that_succeeds_ends_its_interrupted_children_and_leaves_live_ones(server, gates):
    parent, child = await _parent_with_child(server)
    # one a restart left interrupted and nobody resumes any more
    server.run_store.create_run("orphan", "child", server.run_store.get_run(child)["definition"], params={},
                                mocks={}, owner="gone:1:x", lease_until=utc_at(-1), status="interrupted")
    server.run_store.update_run("orphan", caller_run=parent, caller_request=f"{parent}_009")

    gates[0].touch()
    assert (await settle(server.run_manager, parent))["status"] == "succeeded"
    await until(lambda: server.run_store.get_run("orphan")["status"] == "cancelled", what="the orphan ends")

    error = server.run_store.get_run("orphan")["error"]
    assert error["message"] == f"terminated by its caller run {parent} ending", error
    assert server.run_store.get_run(child)["status"] == "running"  # started in the background: it goes on


async def test_feat1_a_parent_that_fails_resumably_keeps_its_interrupted_children(server, gates):
    parent, child = await _parent_with_child(server, fail=True)
    server.run_store.create_run("orphan", "child", server.run_store.get_run(child)["definition"], params={},
                                mocks={}, owner="gone:1:x", lease_until=utc_at(-1), status="interrupted")
    server.run_store.update_run("orphan", caller_run=parent, caller_request=f"{parent}_009")

    gates[0].touch()
    row = await settle(server.run_manager, parent)
    assert row["status"] == "failed" and row["error"]["resumable"], row["error"]
    await asyncio.sleep(0.5)  # an end_children would have resumed it into its end by now
    assert server.run_store.get_run("orphan")["status"] == "interrupted"

    # closed for good (a terminate of the stopped parent): no task of it ends, its children end all the same
    await server.service.control_run(parent, "terminate")
    assert server.run_store.get_run(parent)["status"] == "cancelled"
    await until(lambda: server.run_store.get_run("orphan")["status"] == "cancelled", what="the orphan ends")
    assert server.run_store.get_run("orphan")["error"]["message"] == f"terminated by its caller run {parent} ending"


async def test_feat1_a_parents_continue_leaves_a_child_paused_on_its_own(server, gates):
    parent, child = await _parent_with_child(server)
    await server.service.control_run(child, "pause")
    gates[1].touch()
    await until(lambda: server.run_store.get_run(child)["status"] == "paused", what="the child pauses on its own")

    await server.service.control_run(parent, "pause")
    await server.service.control_run(parent, "continue")
    await server.service.control_run(parent, "continue")  # a continue of a parent that runs: nothing to release
    await asyncio.sleep(0.3)
    assert server.run_store.get_run(child)["status"] == "paused"  # its own pause: only its own continue
    await server.service.control_run(child, "continue")
    await until(lambda: server.run_store.get_run(child)["status"] == "succeeded", what="the child goes on")


async def test_feat1_a_child_resumed_by_key_under_another_parent_survives_the_first_parents_end(server, gates):
    first, first_child = await _parent_with_child(server)
    # a restart left a child of the first parent interrupted; its key is what its caller resumes it by
    server.run_store.create_run("kept", "child", server.run_store.get_run(first_child)["definition"], params={},
                                mocks={}, owner="gone:1:x", lease_until=utc_at(-1), status="interrupted",
                                run_key="chapter-7")
    server.run_store.update_run("kept", caller_run=first, caller_request=f"{first}_009")

    second = (await server.service.start_run("parent", live={"service": server.service, "key": "chapter-7"}))["run_id"]
    await until(lambda: "kept" in server.run_manager.live, what="the second parent resumed it")
    assert server.run_store.get_run("kept")["caller_run"] == second

    await server.service.control_run(first, "terminate")
    assert server.run_store.get_run(first)["status"] == "cancelled"
    await asyncio.sleep(0.3)
    assert server.run_store.get_run("kept")["status"] == "running"  # the second parent's child now
    assert [c["id"] for c in server.run_store.children(second)] == ["kept"]


async def test_feat1_a_callers_end_reaches_its_unended_children_beyond_the_first_two_hundred(server):
    store = server.run_store
    definition = runnable({"m.yaml": LIMITED}).snapshot()
    seed_run(store, "p", {"m.yaml": LIMITED}, owner="o", lease=-1, status="succeeded")
    for n in range(200):  # the oldest ones ended long ago
        store.create_run(f"a{n:03}", "m", definition, owner="o", status="succeeded")
        store.update_run(f"a{n:03}", caller_run="p")
    store.create_run("z", "m", definition, owner="gone:1:x", lease_until=utc_at(-1), status="interrupted")
    store.update_run("z", caller_run="p")

    assert [c["id"] for c in store.children("p", unended=True)] == ["z"]
    await server.service.end_children("p", interrupted_only=True)
    await until(lambda: store.get_run("z")["status"] == "cancelled", what="the newest child ends")


async def test_feat1_unended_children_are_all_of_them_not_a_page(harness):
    store = harness.store
    for n in range(201):
        store.create_run(f"c{n:03}", "m", {}, owner="o", status="interrupted")
        store.update_run(f"c{n:03}", caller_run="p")
    assert len(store.children("p", unended=True)) == 201
    assert len(store.children("p")) == 200  # the listing get_run shows stays a page


# ------------------------------------------------------------------ FEAT-6: the run's own limit and cost

LIMITED = """\
stategraph: 1
id: m
limits: {max_cost: 4}
initial: done
states:
  done: {type: final}
"""


async def test_feat6_get_run_carries_the_runs_own_max_cost_and_cost_beside_the_spend(server):
    store = server.run_store
    seed_run(store, "p", {"m.yaml": LIMITED}, owner="o", lease=-1, status="succeeded")
    store.create_run("c", "child", {}, owner="o")
    store.update_run("c", caller_run="p", caller_request="p_001")
    store.record("p", "activity", "s0", status="done", data={"meta": {"cost": 0.5, "request_ids": ["p_001"]}})
    store.record("c", "activity", "s0", status="done", data={"meta": {"cost": 2.0}})

    seen = server.service.get_run("p")
    assert (seen["max_cost"], seen["cost"], seen["spend"]["cost"]) == (4.0, 0.5, 2.5), seen
    plain = server.service.get_run("c")  # no definition to read: no limit, and the answer still says so
    assert "max_cost" in plain and plain["max_cost"] is None and plain["cost"] == 2.0


SPENDS = """\
stategraph: 1
id: m
python: m.py
limits: {max_cost: 5}
initial: a
states:
  a:
    do: {call: spend}
    transitions: [{target: b}]
  b:
    do: {agent: w, task: go}
    transitions: [{target: done}]
  done: {type: final}
"""

SPENDS_PY = """\
from plugins.stategraph.engine.backend import CALL_SPEND


def spend():
    CALL_SPEND.get().cost += 0.6
"""


async def test_feat6_a_live_runs_budget_is_its_running_run_cost(harness):
    manager = harness.manager()
    gate = asyncio.Event()
    run_id = await manager.start(runnable({"m.yaml": SPENDS, "m.py": SPENDS_PY}),
                                 backend=FakeBackend({"b": held(gate)}))
    await until(lambda: activity_rows(harness.store, run_id).get("s1", {}).get("status") == "started",
                what="b in flight")
    assert manager.budget(run_id) == (5.0, 0.6)
    gate.set()
    assert (await settle(manager, run_id))["status"] == "succeeded"
    assert manager.budget(run_id) == (5.0, 0.6)  # ended: its journal says the same


async def test_feat6_a_forks_budget_counts_its_copied_steps_running_and_ended_alike(harness):
    manager = harness.manager()
    files = {"m.yaml": SPENDS, "m.py": SPENDS_PY}
    source = await manager.start(runnable(files), backend=FakeBackend({"b": "ok"}))
    assert (await settle(manager, source))["status"] == "succeeded"

    gate = asyncio.Event()
    fork = await manager.fork(source, at_step=1, backend=FakeBackend({"b": held(gate)}))
    await until(lambda: activity_rows(harness.store, fork).get("s1", {}).get("status") == "started",
                what="b in flight in the fork")
    assert manager.budget(fork) == (5.0, 0.6)  # its guard counts the replayed spend
    gate.set()
    assert (await settle(manager, fork))["status"] == "succeeded"
    assert manager.budget(fork) == (5.0, 0.6)  # ended: the same from its journal
    assert harness.store.spend(fork) is None  # what it spent itself: nothing (the source spent it)


async def test_feat6_a_budget_leaves_out_what_a_resume_set_aside(harness):
    store = harness.store
    seed_run(store, "r", {"m.yaml": LIMITED}, owner="o", lease=-1, status="interrupted")
    store.record("r", "activity", "s0", status="done", data={"meta": {"cost": 0.25}})
    store.record("r", "activity", "rewound.1/s1", status="done", data={"meta": {"cost": 1.0}})
    assert harness.manager().budget("r") == (4.0, 0.25)
