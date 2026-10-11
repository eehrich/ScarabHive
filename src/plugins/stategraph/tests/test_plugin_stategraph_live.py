"""A caller's live objects for call functions (``sg.live``) and ``service.run_to_end``.

A Python caller that runs a machine as a step of its own work (the writer's
chapter gate) hands it objects that cannot go through the journal -- its
pipeline instance. They reach call functions as ``sg.live``, never the journal,
and a resume without them sees an empty mapping. ``run_to_end`` starts the run
and waits for its end in this process; a cancelled caller terminates it.
"""
from __future__ import annotations

import asyncio

import pytest

from plugins.stategraph.service import ServiceError
from plugins.stategraph.tests.stategraph_testkit import Harness, runnable, settle, until
from plugins.stategraph.tests.test_plugin_stategraph_server import server  # noqa: F401 -- a fixture

USES_LIVE = """\
stategraph: 1
id: uses_live
python: uses_live.py
initial: use
states:
  use:
    do: {call: use, args: {}}
    transitions: [{target: done, effect: ctx.got = out}]
  done: {type: final, output: "{{ ctx.got }}"}
context: {got: null}
"""

USES_LIVE_PY = """\
def use(sg):
    return sg.live["counter"].bump()
"""

WAITS = """\
stategraph: 1
id: waits
python: waits.py
initial: wait
states:
  wait:
    do: {call: wait, args: {}}
    transitions: [{target: done, effect: ctx.got = out}]
  done: {type: final, output: "{{ ctx.got }}"}
context: {got: null}
"""

WAITS_PY = """\
async def wait(sg):
    await sg.live["gate"].wait()
    return sg.live["counter"].bump()
"""


class Counter:
    """A live object: not JSON, and its identity is what a copy would lose."""

    def __init__(self):
        self.calls = 0

    def bump(self) -> int:
        self.calls += 1
        return self.calls


def _machine(tmp_path, name, yaml_text, py_text):
    (tmp_path / "machines" / f"{name}.yaml").write_text(yaml_text, encoding="utf-8")
    (tmp_path / "machines" / f"{name}.py").write_text(py_text, encoding="utf-8")


async def test_a_call_function_works_on_the_callers_own_object(server, tmp_path):
    _machine(tmp_path, "uses_live", USES_LIVE, USES_LIVE_PY)
    counter = Counter()
    row = await server.service.run_to_end("uses_live", {}, live={"counter": counter})
    assert row["status"] == "succeeded", row.get("error")
    assert row["output"] == 1 and counter.calls == 1   # the same object, not a copy


async def test_without_live_objects_the_function_fails_loudly(server, tmp_path):
    _machine(tmp_path, "uses_live", USES_LIVE, USES_LIVE_PY)
    row = await server.service.run_to_end("uses_live", {})
    assert row["status"] == "failed"
    assert "counter" in str(row.get("error")), row.get("error")


async def test_only_the_names_of_live_objects_reach_the_journal(server, tmp_path):
    _machine(tmp_path, "uses_live", USES_LIVE, USES_LIVE_PY)
    row = await server.service.run_to_end("uses_live", {}, live={"counter": Counter()})
    store = server.service.run_store
    rows = store.rows(row["id"])
    assert "Counter object" not in str(rows) and "counter" not in str(row.get("params"))
    (named,) = [r for r in rows if r["kind"] == "trace" and r["status"] == "live_objects"]
    assert named["data"] == {"names": ["counter"]}


async def test_run_to_end_returns_an_ended_keys_outcome_without_running_again(server, tmp_path):
    _machine(tmp_path, "uses_live", USES_LIVE, USES_LIVE_PY)
    counter = Counter()
    first = await server.service.run_to_end("uses_live", {}, run_key="k1", live={"counter": counter})
    again = await server.service.run_to_end("uses_live", {}, run_key="k1", live={"counter": counter})
    assert again["id"] == first["id"] and again["output"] == 1 and counter.calls == 1


async def test_a_cancelled_caller_terminates_its_run(server, tmp_path):
    _machine(tmp_path, "waits", WAITS, WAITS_PY)
    gate = asyncio.Event()
    caller = asyncio.ensure_future(server.service.run_to_end(
        "waits", {}, run_id="cancelme", live={"gate": gate, "counter": Counter()}))
    await until(lambda: "cancelme" in server.service.runs.live, what="the run started")
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert server.service.run_store.get_run("cancelme")["status"] == "cancelled"


@pytest.mark.parametrize("keep, status", [(True, "interrupted"), (False, "cancelled")])
async def test_a_caller_cut_from_outside_leaves_its_run_resumable(server, tmp_path, keep, status):
    # a graceful shutdown cancels the request that waits in run_to_end: the run is not over, a resume under its key
    # goes on from the step in flight; a cancel of the caller's own work (keep_on_cancel says no) still ends it
    _machine(tmp_path, "waits", WAITS, SLOW_STOP_PY)
    gate, counter = asyncio.Event(), Counter()
    caller = asyncio.ensure_future(server.service.run_to_end(
        "waits", {}, run_id="cut", run_key="k-cut", live={"gate": gate, "counter": counter},
        keep_on_cancel=lambda: keep))
    await until(lambda: "cut" in server.service.runs.live, what="the run started")
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    # no wait of our own: when the caller's cancel goes on, the row already says how the run ended
    assert server.service.run_store.get_run("cut")["status"] == status
    if keep:
        gate.set()
        row = await server.service.run_to_end("waits", {}, run_key="k-cut", live={"gate": gate, "counter": counter})
        assert row["id"] == "cut" and row["status"] == "succeeded" and counter.calls == 1, row


SLOW_STOP_PY = """\
import asyncio


async def wait(sg):
    try:
        await sg.live["gate"].wait()
    except asyncio.CancelledError:
        await asyncio.sleep(0.3)   # cleans up before it lets the cancel through (an agent activity's grace)
        raise
    return sg.live["counter"].bump()
"""


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
async def run(sg):
    row = await sg.live["service"].run_to_end("waits", {}, run_id="child", run_key="k-child",
                                              live={"gate": sg.live["gate"], "counter": sg.live["counter"]},
                                              keep_on_cancel=sg.live["keep"])
    return row["status"]
"""


@pytest.mark.parametrize("how, keep, child", [("terminate", True, "cancelled"), ("interrupt", False, "interrupted")])
async def test_a_nested_run_goes_with_the_ending_of_the_run_that_waits_on_it(server, tmp_path, how, keep, child):
    # a terminate from the panel reaches no token of the waiting caller: the child must not be left resumable;
    # a parent that stops (shutdown, interrupt) leaves its child resumable whatever keep_on_cancel would say
    _machine(tmp_path, "waits", WAITS, WAITS_PY)
    _machine(tmp_path, "parent", PARENT, PARENT_PY)
    service = server.service
    started = await service.start_run("parent", params={}, run_id="parent-run",
                                      live={"service": service, "gate": asyncio.Event(), "counter": Counter(),
                                            "keep": lambda: keep})
    await until(lambda: "child" in service.runs.live, what="the child started")
    if how == "terminate":
        await service.control_run(started["run_id"], "terminate")
    else:
        service.runs.interrupt(started["run_id"])
    await until(lambda: not service.runs.live, what="both runs ended")
    assert service.run_store.get_run("child")["status"] == child
    assert service.run_store.get_run("parent-run")["status"] == ("cancelled" if how == "terminate" else "interrupted")


async def test_a_keep_on_cancel_that_breaks_terminates(server, tmp_path):
    _machine(tmp_path, "waits", WAITS, WAITS_PY)

    def broken():
        raise RuntimeError("no answer")

    caller = asyncio.ensure_future(server.service.run_to_end(
        "waits", {}, run_id="broken", live={"gate": asyncio.Event(), "counter": Counter()}, keep_on_cancel=broken))
    await until(lambda: "broken" in server.service.runs.live, what="the run started")
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert server.service.run_store.get_run("broken")["status"] == "cancelled"


async def test_a_key_another_process_holds_is_refused(server, tmp_path, monkeypatch):
    _machine(tmp_path, "uses_live", USES_LIVE, USES_LIVE_PY)

    async def elsewhere(*a, **k):
        return {"run_id": "r", "attached": False, "owner": "other:1"}

    monkeypatch.setattr(server.service, "start_run", elsewhere)
    with pytest.raises(ServiceError) as exc:
        await server.service.run_to_end("uses_live", {}, run_key="k")
    assert exc.value.status == 409


async def test_a_resume_gets_the_live_objects_handed_again(tmp_path):
    # The first process "crashes" while the call waits; the second resumes the
    # run from its journal with its own objects -- the function runs again and
    # sees them, the first process's objects are gone.
    files = {"waits.yaml": WAITS, "waits.py": WAITS_PY}
    harness = Harness(tmp_path)
    try:
        first = harness.manager()
        run_id = await first.start(runnable(files, "waits.yaml"),
                                   live={"gate": asyncio.Event(), "counter": Counter()})
        await until(lambda: run_id in first.live, what="the run started")
        await asyncio.sleep(0.05)
        await first.shutdown()
        assert harness.store.get_run(run_id)["status"] == "interrupted"
        second = harness.manager()
        gate, counter = asyncio.Event(), Counter()
        gate.set()
        await second.resume(run_id, live={"gate": gate, "counter": counter})
        row = await settle(second, run_id, timeout=10.0)
        assert row["status"] == "succeeded", row.get("error")
        assert row["output"] == 1 and counter.calls == 1
        resumed = [r for r in harness.store.rows(run_id) if r["key"].startswith("live_objects:resumed:")]
        assert [r["data"] for r in resumed] == [{"names": ["counter", "gate"]}]
    finally:
        await harness.close()


async def test_a_resume_without_the_live_objects_says_so_in_the_history(tmp_path):
    files = {"waits.yaml": WAITS, "waits.py": WAITS_PY}
    harness = Harness(tmp_path)
    try:
        first = harness.manager()
        run_id = await first.start(runnable(files, "waits.yaml"),
                                   live={"gate": asyncio.Event(), "counter": Counter()})
        await until(lambda: run_id in first.live, what="the run started")
        await asyncio.sleep(0.05)
        await first.shutdown()
        second = harness.manager()
        await second.resume(run_id)               # the panel's resume: nothing to hand back
        row = await settle(second, run_id, timeout=10.0)
        assert row["status"] == "failed" and "gate" in str(row.get("error"))
        resumed = [r for r in harness.store.rows(run_id) if r["key"].startswith("live_objects:resumed:")]
        assert [r["data"] for r in resumed] == [{"names": []}]
    finally:
        await harness.close()


async def test_every_resume_keeps_its_own_history_row(tmp_path):
    # resumed with the objects, interrupted again, resumed without them (the panel):
    # the history shows both, the first one is not overwritten
    files = {"waits.yaml": WAITS, "waits.py": WAITS_PY}
    harness = Harness(tmp_path)
    try:
        first = harness.manager()
        run_id = await first.start(runnable(files, "waits.yaml"),
                                   live={"gate": asyncio.Event(), "counter": Counter()})
        await until(lambda: run_id in first.live, what="the run started")
        await asyncio.sleep(0.05)
        await first.shutdown()
        second = harness.manager()
        await second.resume(run_id, live={"gate": asyncio.Event(), "counter": Counter()})
        await until(lambda: run_id in second.live, what="the run resumed")
        await asyncio.sleep(0.05)
        await second.shutdown()
        third = harness.manager()
        await third.resume(run_id)
        await settle(third, run_id, timeout=10.0)
        resumed = sorted((r["key"], r["data"]["names"]) for r in harness.store.rows(run_id)
                         if r["key"].startswith("live_objects:resumed:"))
        assert resumed == [("live_objects:resumed:1", ["counter", "gate"]), ("live_objects:resumed:2", [])]
    finally:
        await harness.close()


async def test_run_to_end_does_not_take_over_a_run_already_live_in_this_process(server, tmp_path):
    # a second caller of the same key: its objects would be dropped silently and its
    # cancel would end the first caller's run
    _machine(tmp_path, "waits", WAITS, WAITS_PY)
    gate = asyncio.Event()
    first = asyncio.ensure_future(server.service.run_to_end(
        "waits", {}, run_key="k", run_id="firstrun", live={"gate": gate, "counter": Counter()}))
    await until(lambda: "firstrun" in server.service.runs.live, what="the first run started")
    with pytest.raises(ServiceError) as exc:
        await server.service.run_to_end("waits", {}, run_key="k", live={"gate": gate, "counter": Counter()})
    assert exc.value.status == 409
    watcher = asyncio.ensure_future(server.service.run_to_end("waits", {}, run_key="k"))
    await asyncio.sleep(0.05)
    watcher.cancel()                              # attached without objects: its cancel ends only itself
    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert server.service.run_store.get_run("firstrun")["status"] == "running"
    gate.set()
    row = await asyncio.wait_for(first, 10)
    assert row["status"] == "succeeded" and row["output"] == 1


async def test_run_to_end_on_an_interrupted_key_resumes_it_with_the_callers_live_objects(tmp_path):
    """Another process ran the key and stopped while the call waited (a restart): run_to_end with the same key
    resumes that run here -- start_run's interrupted path -- and hands it this caller's objects."""
    from agent_system.config.models import AgentSystemConfig
    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config

    first = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    _machine(tmp_path, "waits", WAITS, WAITS_PY)
    second = None
    try:
        started = await first.service.start_run("waits", params={}, run_key="chapter-1",
                                                live={"gate": asyncio.Event(), "counter": Counter()})
        run_id = started["run_id"]
        await until(lambda: run_id in first.service.runs.live, what="the run started")
        await asyncio.sleep(0.05)
        await first.service.runs.shutdown()
        assert first.service.run_store.get_run(run_id)["status"] == "interrupted"

        second = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
        gate, counter = asyncio.Event(), Counter()
        gate.set()
        row = await second.service.run_to_end("waits", {}, run_key="chapter-1", live={"gate": gate, "counter": counter})

        assert row["id"] == run_id and row["status"] == "succeeded", row.get("error")
        assert row["output"] == 1 and counter.calls == 1
    finally:
        await first.stop_plugin()
        if second is not None:
            await second.stop_plugin()
