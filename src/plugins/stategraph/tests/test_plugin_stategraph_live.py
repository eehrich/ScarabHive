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
