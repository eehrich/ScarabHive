"""Final review 2026-10-16, area service: the resume lease of the scheduler (SVC-1), set before the resume of a run
that failed resumably (SVC-2), one rule for a failure a resume may get past (SVC-4) -- through the real
StateGraphServer and RunStore under tmp_path.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, settle, utc_at
from plugins.stategraph.tests.test_plugin_stategraph_r15_life_lifecycle import (  # noqa: F401 -- fixtures
    _interrupted, _open, gate, make_server, servers)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

RETRY = """\
stategraph: 1
id: retry
python: retry.py
context: {x: 0}
initial: a
states:
  a:
    do: {call: quick, args: {}}
    transitions: [{target: b}]
  b:
    do: {call: boom, args: {x: "{{ ctx.x }}"}}
    transitions: [{target: done}]
  done: {type: final, output: {x: "{{ ctx.x }}"}}
"""

RETRY_PY = """\
def quick():
    return 1


def boom(sg, x):
    if x != 5:
        raise sg.Error("call_failed", "needs x = 5", resumable=True)
    return x
"""


# ------------------------------------------------------------------ SVC-1: resuming has a lease of its own

async def test_svc1_a_non_driver_holding_the_slot_lease_does_not_stop_the_resumes(servers, monkeypatch):
    from plugins.stategraph import schedules
    from plugins.stategraph.schedules import Scheduler, _stamp, resume_lease

    first = servers()
    run_id = await _interrupted(first, "hold", wait_for="s0")
    api = servers(auto_resume=["hold"])
    _open()
    now = time.time() + 1
    # a worker or agent-cli with schedules took the slots' lease while the API was down
    assert api.run_store.take_scheduler(api.name, "otherhost:1:abc", _stamp(now + 90), now=_stamp(now))
    monkeypatch.setattr(schedules, "_drives", lambda: True)

    assert await Scheduler(api, []).tick(now) == [run_id]
    assert (await settle(api.run_manager, run_id))["status"] == "succeeded"

    # a process that does not drive never takes the resume lease: the next driver gets it at once
    monkeypatch.setattr(schedules, "_drives", lambda: False)
    worker = servers()
    await Scheduler(worker, []).tick(now + 200)
    assert api.run_store.take_scheduler(resume_lease(api.name), "driver:2:x", _stamp(now + 400), now=_stamp(now + 200))


# ------------------------------------------------------------------ SVC-2: set on a run that failed resumably

async def test_svc2_set_before_the_resume_of_a_resumably_failed_run_feeds_the_rerun(servers, tmp_path):
    server = servers()
    (tmp_path / "machines" / "retry.yaml").write_text(RETRY, encoding="utf-8")
    (tmp_path / "machines" / "retry.py").write_text(RETRY_PY, encoding="utf-8")
    run_id = (await server.service.start_run("retry"))["run_id"]
    row = await settle(server.run_manager, run_id)
    assert row["status"] == "failed" and row["error"]["resumable"], "fixture: a resumable failure in step b"

    assert await server.service.control_run(run_id, "set", path="ctx.x", expr="5", user_id="ops_ada") == {"value": 5}
    await server.service.control_run(run_id, "resume")
    row = await settle(server.run_manager, run_id)

    # the failed activity runs again after the rewind: the value is there before it, not after it
    assert row["status"] == "succeeded" and row["output"] == {"x": 5}, row["error"] or row["output"]


# ------------------------------------------------------------------ SVC-4: an internal failure is not the end

async def test_svc4_an_internally_failed_caller_keeps_its_interrupted_children(tmp_path, monkeypatch):
    server = make_server(tmp_path)
    store = server.run_store
    ended: list[str] = []

    async def end_children(run_id, **_):
        ended.append(run_id)

    monkeypatch.setattr(server.service, "end_children", end_children)
    try:
        for parent, error in (("internal", {"type": "internal", "message": "engine bug"}),
                              ("broken", {"type": "call_failed", "message": "for good"})):
            store.create_run(parent, "parent", {}, owner="o", status="failed")
            store.update_run(parent, error=error)
            store.create_run(f"{parent}-child", "hold", {}, owner="o", status="interrupted")
            store.update_run(f"{parent}-child", caller_run=parent, caller_request=f"{parent}_001")
            server._run_finished(parent)
        await asyncio.gather(*server._ending)
    finally:
        store.close()

    assert ended == ["broken"], "a resume may get past an internal failure (API-10): its children stay"


def test_svc4_retention_keeps_the_journal_of_an_internally_failed_run(tmp_path):
    from plugins.stategraph.engine.journal import RunStore

    store = RunStore(tmp_path / "runs.db")
    for run_id, error in (("internal", {"type": "internal", "message": "x"}), ("broken", {"type": "x"})):
        store.create_run(run_id, "m", {}, owner="o", status="failed")
        store.update_run(run_id, error=error, finished_at=utc_at(-40 * 86400))
        store.record(run_id, "activity", "s0", status="done", data={})
    try:
        assert store.compact_ended(before=utc_at(-30 * 86400)) == ["broken"]
    finally:
        store.close()


async def test_svc4_a_terminate_closes_an_internally_failed_run(servers):
    server = servers()
    run_id = (await server.service.start_run("fails"))["run_id"]
    await settle(server.run_manager, run_id)
    server.run_store.update_run(run_id, error={"type": "internal", "message": "engine bug"})

    answer = await server.service.control_run(run_id, "terminate", user_id="ops_ada")

    # it kept its children for a resume: only a terminate that closes it ends them (FEAT-8)
    assert answer["status"] == "cancelled" and answer["error"]["closed"]["type"] == "internal", answer


# ------------------------------------------------------------------ SVC-3: snapshots load off the event loop, once

async def test_svc3_the_scheduler_loads_a_runs_snapshot_once_and_off_the_loop(servers, tmp_path, monkeypatch):
    import threading

    from plugins.stategraph.model import loader
    from plugins.stategraph.schedules import Scheduler
    from plugins.stategraph.tests.stategraph_testkit import until
    from plugins.stategraph.tests.test_plugin_stategraph_r15_life_lifecycle import HOLD

    monkeypatch.setenv("STATEGRAPH_DRIVER", "1")
    first = servers()
    (tmp_path / "machines" / "autohold.yaml").write_text(
        HOLD.replace("id: hold", "id: autohold\nresume: auto"), encoding="utf-8")
    # resume: auto, but it holds its caller's live objects: the scheduler looks at it on every tick, resumes it never
    run_id = (await first.service.start_run("autohold", live={"thing": object()}))["run_id"]
    await until(lambda: first.run_store.has_row(run_id, "activity", "s0"), what="the run started")
    await first.run_manager.shutdown()
    second = servers()
    loads, real = [], loader.load_snapshot

    def counted(*args, **kwargs):
        loads.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(loader, "load_snapshot", counted)
    scheduler = Scheduler(second, [])
    now = time.time() + 1
    assert await scheduler.resume_interrupted(now) == [] and await scheduler.resume_interrupted(now + 30) == []

    assert len(loads) == 1 and loads[0] != threading.get_ident(), loads


async def test_svc3_a_resume_compares_the_code_off_the_loop(servers, monkeypatch):
    import threading

    first, second = servers(), servers()
    run_id = await _interrupted(first, "hold", wait_for="s0")
    seen = []
    monkeypatch.setattr(second.service, "_skew", lambda row: seen.append(threading.get_ident()) or [])
    _open()
    await second.service.control_run(run_id, "resume")
    await settle(second.run_manager, run_id)

    assert seen and seen[0] != threading.get_ident(), seen


# ------------------------------------------------------------------ SVC-5: another pid namespace is not judged

def test_svc5_an_owner_in_another_pid_namespace_of_this_host_is_never_judged_gone(monkeypatch):
    import subprocess
    import sys

    from plugins.stategraph.engine import runner
    from plugins.stategraph.service import owner_gone

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    monkeypatch.setattr(runner, "host_identity", lambda: "box/4026531836")

    assert runner.owner_id().startswith("box/4026531836:"), "the owner id carries the namespace"
    assert owner_gone(f"box/4026531836:{dead.pid}:abc", utc_at(60)), "fixture: the same namespace is judged"
    # a container that shares the hostname: its pid does not exist here, and it may well run
    assert not owner_gone(f"box/4026532999:{dead.pid}:abc", utc_at(60))
    assert not owner_gone(f"box:{dead.pid}:abc", utc_at(60)), "an owner id without one is no proof either"


@pytest.mark.skipif(os.name != "nt", reason="status() is slow on Windows only; elsewhere it finds the zombies")
def test_owner_gone_judges_without_the_slow_status_on_windows(monkeypatch):
    import psutil
    import subprocess
    import sys

    from plugins.stategraph.engine import runner
    from plugins.stategraph.service import owner_gone

    monkeypatch.setattr(runner, "host_identity", lambda: "box")

    def forbidden(self):
        raise AssertionError("status() walks every process of the system")
    monkeypatch.setattr(psutil.Process, "status", forbidden)

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert not owner_gone(f"box:{child.pid}:abc", utc_at(60))
    finally:
        child.kill()
    child.wait()
    assert owner_gone(f"box:{child.pid}:abc", utc_at(60))

    still_active = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(259)"])  # 259 reads as STILL_ACTIVE
    still_active.wait()  # its handle is held here, too
    assert owner_gone(f"box:{still_active.pid}:abc", utc_at(60))
