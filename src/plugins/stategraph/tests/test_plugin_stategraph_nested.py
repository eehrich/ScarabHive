"""Submachines two levels deep, each in its own file: a caller's live objects (``sg.live``) reach the deepest call,
and a run stopped inside it resumes there from its journal -- the steps before it are not run again.

The writer's book end is built so (a parent that only sequences, a file per phase): these are the guarantees it
stands on.
"""
from __future__ import annotations

import asyncio

from plugins.stategraph.tests.stategraph_testkit import Harness, runnable, settle, until

PARENT = """\
stategraph: 1
id: parent
python: parent.py
imports: {mid: ./mid.yaml}
initial: before
states:
  before:
    do: {call: before, args: {}}
    transitions: [{target: phase, effect: ctx.before = out}]
  phase:
    do: {machine: mid, params: {seed: "{{ ctx.before }}"}}
    transitions: [{target: done, effect: ctx.got = out}]
  done: {type: final, output: "{{ ctx.got }}"}
context: {before: null, got: null}
"""

PARENT_PY = """\
def before(sg):
    return sg.live["before"].bump()
"""

MID = """\
stategraph: 1
id: mid
python: mid.py
imports: {leaf: ./leaf.yaml}
params: {seed: {type: integer}}
initial: own
states:
  own:
    do: {call: own, args: {}}
    transitions: [{target: deeper}]
  deeper:
    do: {machine: leaf, params: {}}
    transitions: [{target: done, effect: ctx.got = out}]
  done: {type: final, output: "{{ params.seed * 100 + ctx.got }}"}
context: {got: null}
"""

MID_PY = """\
def own(sg):
    return sg.live["mid"].bump()
"""

LEAF = """\
stategraph: 1
id: leaf
python: leaf.py
initial: wait
states:
  wait:
    do: {call: wait, args: {}}
    transitions: [{target: done, effect: ctx.got = out}]
  done: {type: final, output: "{{ ctx.got }}"}
context: {got: null}
"""

LEAF_PY = """\
async def wait(sg):
    await sg.live["gate"].wait()
    return sg.live["leaf"].bump()
"""

FILES = {"parent.yaml": PARENT, "parent.py": PARENT_PY, "mid.yaml": MID, "mid.py": MID_PY,
         "leaf.yaml": LEAF, "leaf.py": LEAF_PY}


class Counter:
    def __init__(self):
        self.calls = 0

    def bump(self) -> int:
        self.calls += 1
        return self.calls


def objects(gate: asyncio.Event) -> dict:
    return {"gate": gate, "before": Counter(), "mid": Counter(), "leaf": Counter()}


async def test_live_objects_reach_a_call_two_submachines_deep(tmp_path):
    harness = Harness(tmp_path)
    try:
        gate = asyncio.Event()
        gate.set()
        live = objects(gate)
        manager = harness.manager()
        run_id = await manager.start(runnable(FILES, "parent.yaml"), live=live)
        row = await settle(manager, run_id, timeout=10.0)
        assert row["status"] == "succeeded", row.get("error")
        assert row["output"] == 101                       # seed 1 from the parent, 1 from the leaf
        assert [live[name].calls for name in ("before", "mid", "leaf")] == [1, 1, 1]
    finally:
        await harness.close()


async def test_a_restart_inside_a_nested_submachine_resumes_it_there(tmp_path):
    """The process stops while the leaf's call waits; a new process resumes the run with its own objects: the
    parent's and the middle machine's calls are replayed from the journal, only the leaf's runs again."""
    harness = Harness(tmp_path)
    try:
        first = harness.manager()
        started = objects(asyncio.Event())
        run_id = await first.start(runnable(FILES, "parent.yaml"), live=started)
        await until(lambda: started["mid"].calls == 1, what="the middle machine's call ran")
        await asyncio.sleep(0.05)                         # the leaf's call is waiting now
        await first.shutdown()
        assert harness.store.get_run(run_id)["status"] == "interrupted"

        second = harness.manager()
        gate = asyncio.Event()
        gate.set()
        again = objects(gate)
        await second.resume(run_id, live=again)
        row = await settle(second, run_id, timeout=10.0)
        assert row["status"] == "succeeded", row.get("error")
        assert row["output"] == 101, "the parent's journaled result went into the middle machine's params"
        assert [again[name].calls for name in ("before", "mid", "leaf")] == [0, 0, 1], \
            "only the call the stop cut off runs again"
        assert [started[name].calls for name in ("before", "mid", "leaf")] == [1, 1, 0]
    finally:
        await harness.close()


async def test_a_non_idempotent_call_cut_off_in_a_nested_submachine_is_not_started_again(tmp_path):
    """A call that writes (the writer's fix) and was cut off is left half done: a resume must not run it twice.
    Inside a submachine two levels down it fails as ``interrupted`` like at the top, and the steps before it stay
    replayed."""
    files = {**FILES, "leaf.yaml": LEAF.replace("do: {call: wait, args: {}}",
                                                "do: {call: wait, args: {}, idempotent: false}")}
    harness = Harness(tmp_path)
    try:
        first = harness.manager()
        started = objects(asyncio.Event())
        run_id = await first.start(runnable(files, "parent.yaml"), live=started)
        await until(lambda: started["mid"].calls == 1, what="the middle machine's call ran")
        await asyncio.sleep(0.05)
        await first.shutdown()

        second = harness.manager()
        gate = asyncio.Event()
        gate.set()
        again = objects(gate)
        await second.resume(run_id, live=again)
        row = await settle(second, run_id, timeout=10.0)
        assert row["status"] == "failed", row
        chain, error = [], row["error"]
        while error:                                      # each machine wraps what failed below it
            chain.append(error["type"])
            error = error.get("cause")
        assert chain == ["submachine_failed", "submachine_failed", "interrupted"], row["error"]
        assert [again[name].calls for name in ("before", "mid", "leaf")] == [0, 0, 0], "nothing ran twice"
    finally:
        await harness.close()
