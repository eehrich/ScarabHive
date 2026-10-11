"""A call that holds the event loop longer than the lease keeps its run.

The lease says the process lives (docs/stategraph_design.md §5.7). Renewed on the event loop, a synchronous call of
minutes -- the writer's whole-book passes -- let it run out: a sweep marked the run interrupted, and a resume in
another process could take it while it still worked here. The heartbeat runs on a thread of its own.
"""
from __future__ import annotations

import threading
import time

from plugins.stategraph.engine import runner as runner_module
from plugins.stategraph.engine.runner import _utc
from plugins.stategraph.tests.stategraph_testkit import Harness, runnable, settle

BLOCKS = """\
stategraph: 1
id: blocks
python: blocks.py
initial: work
states:
  work:
    do: {call: work, args: {}}
    transitions: [{target: done, effect: ctx.got = out}]
  done: {type: final, output: "{{ ctx.got }}"}
context: {got: null}
"""

BLOCKS_PY = """\
async def work(sg):  # async: it runs on the event loop (a plain def runs in a worker thread and holds nothing)
    return sg.live["work"]()
"""


async def test_a_call_that_holds_the_loop_past_the_lease_keeps_its_run(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "LEASE_SECONDS", 1)
    monkeypatch.setattr(runner_module, "HEARTBEAT_SECONDS", 0.2)
    harness = Harness(tmp_path)
    seen = {}

    def work():
        time.sleep(2.5)                                   # synchronous: the event loop stands still meanwhile
        seen["lease_until"] = harness.store.get_run(run_id)["lease_until"]
        seen["now"] = _utc()
        seen["swept"] = harness.store.mark_expired(now=_utc())   # another process starting a run sweeps
        seen["taken"] = harness.store.take_lease(run_id, "other-process", _utc(60), now=_utc())
        return "done"

    try:
        manager = harness.manager()
        run_id = await manager.start(runnable({"blocks.yaml": BLOCKS, "blocks.py": BLOCKS_PY}, "blocks.yaml"),
                                     live={"work": work})
        row = await settle(manager, run_id, timeout=15.0)
        assert seen["lease_until"] > seen["now"], seen   # renewed while the loop was held
        assert seen["swept"] == [] and seen["taken"] is False, seen
        assert row["status"] == "succeeded" and row["output"] == "done", row.get("error")
    finally:
        await harness.close()


async def test_the_heartbeat_thread_ends_with_the_manager(tmp_path):
    harness = Harness(tmp_path)
    manager = harness.manager()
    run_id = await manager.start(runnable({"blocks.yaml": BLOCKS, "blocks.py": BLOCKS_PY}, "blocks.yaml"),
                                 live={"work": lambda: "x"})
    await settle(manager, run_id)
    thread = manager._heartbeat
    assert thread is not None and thread.is_alive()
    await harness.close()
    thread.join(timeout=2)
    assert not thread.is_alive()


WAITS = """\
stategraph: 1
id: waits
python: waits.py
initial: wait
states:
  wait:
    do: {call: wait, args: {}}
    transitions: [{target: done}]
  done: {type: final}
"""

WAITS_PY = """\
async def wait(sg):
    await sg.live["gate"].wait()
"""


async def test_a_renew_never_falls_between_a_runs_end_and_its_write(tmp_path):
    """The thread's renew waits for the store; a run that ended meanwhile is not renewed -- its end (here
    interrupted, the process stopping) keeps its status and its released lease (review of the thread)."""
    import asyncio

    harness = Harness(tmp_path)
    manager = harness.manager()
    gate = asyncio.Event()
    try:
        run_id = await manager.start(runnable({"waits.yaml": WAITS, "waits.py": WAITS_PY}, "waits.yaml"),
                                     live={"gate": gate})
        ctx = manager.live[run_id].ctx
        with harness.store._lock:                         # the loop writes the end while the thread waits
            renewing = threading.Thread(target=manager._renewed)
            renewing.start()
            time.sleep(0.1)
            ctx.finished = True
            harness.store.update_run(run_id, status="interrupted", lease_until=_utc(-1))
        renewing.join(timeout=5)
        row = harness.store.get_run(run_id)
        assert row["status"] == "interrupted" and row["lease_until"] < _utc(), row
    finally:
        ctx.finished = False
        gate.set()
        await settle(manager, run_id)
        await harness.close()


async def test_a_manager_started_again_after_shutdown_gets_a_heartbeat(tmp_path):
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        files = {"blocks.yaml": BLOCKS, "blocks.py": BLOCKS_PY}
        await settle(manager, await manager.start(runnable(files, "blocks.yaml"), live={"work": lambda: "x"}))
        await manager.shutdown()
        # the old thread may still be alive for a moment after its stop: held so here
        lingering = threading.Thread(target=time.sleep, args=(1.0,), daemon=True)
        lingering.start()
        manager._heartbeat = lingering
        manager._stopping = False                         # what start_plugin does
        await settle(manager, await manager.start(runnable(files, "blocks.yaml"), live={"work": lambda: "x"}))
        assert manager._heartbeat is not lingering, "the stopped thread was kept: no heartbeat once it ends"
        assert manager._heartbeat.is_alive() and not manager._heartbeat_stop.is_set()
    finally:
        await harness.close()


async def test_a_run_started_inside_a_call_is_not_part_of_its_spend(tmp_path):
    """A run a call function starts (run_to_end, a tool that runs a machine) counts its own LLM calls; the call's
    meter must not reach its tasks (review of the call spend)."""
    from plugins.stategraph.engine.backend import CALL_SPEND

    harness = Harness(tmp_path)
    manager = harness.manager()
    seen = {}

    async def outer():
        seen["outer"] = CALL_SPEND.get()
        nested = await manager.start(runnable({"blocks.yaml": BLOCKS, "blocks.py": BLOCKS_PY}, "blocks.yaml"),
                                     live={"work": lambda: "inner"})
        seen["nested"] = manager.live[nested].task.get_context().get(CALL_SPEND)
        await settle(manager, nested)
        return "outer"

    files = {"blocks.yaml": BLOCKS, "blocks.py": BLOCKS_PY.replace('return sg.live["work"]()',
                                                                   'return await sg.live["work"]()')}
    try:
        await settle(manager, await manager.start(runnable(files, "blocks.yaml"), live={"work": outer}))
        assert seen["outer"] is not None and seen["nested"] is None, seen
    finally:
        await harness.close()
