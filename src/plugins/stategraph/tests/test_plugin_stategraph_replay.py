"""Durability (docs/stategraph_design.md §5.3-§5.7): resume, divergence, fork, leases, run_key.

A "crash" is real process behaviour on a real store: the run's backend call
blocks on an ``asyncio.Event``, ``RunManager.shutdown()`` cancels the run task
(status ``interrupted``, the in-flight activity row stays ``started``), and a
NEW ``RunManager`` on the same ``RunStore`` resumes it from the journal. The
backends count their calls, so "never repeats a finished activity" is counted,
not assumed.

Mutation checks run (each turned the named tests red, then was restored from a copy):
- activity.execute: the recorded-outcome lookup skipped                         -> test_resume_never_repeats_a_finished_activity
- activity.execute: attempt offset from the started row ignored                -> test_resume_continues_the_attempt_count_*
- activity.execute: non-idempotent started rows run again                      -> test_in_flight_tool_is_not_started_again
- activity._check: ``path`` not compared                                        -> test_changed_guard_after_a_crash_diverges
- runner.after_step: recorded ctx hash not compared                             -> test_changed_effect_after_a_crash_diverges_on_the_context_hash
- runner.fork: rows at or after at_step kept                                    -> test_fork_replays_the_prefix_and_runs_the_rest_live
- runner.fork: ``tree`` ignored (always the snapshot)                          -> test_fork_onto_the_current_definition
- journal.take_lease: ``lease_until < ?`` clause -> always true                 -> test_take_lease_refuses_a_live_foreign_lease
- journal.mark_expired: lease clause dropped                                    -> test_sweep_marks_only_expired_runs
- service.start_run: the run_key lookup removed                                -> test_run_key_attaches_then_resumes
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.service import ServiceError
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, activity_rows, crash_in,
                                                         held, resume, runnable, seed_run, settle, tool_config, until,
                                                         utc_at)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

CHAIN = """\
stategraph: 1
id: m
context: {a: null, b: null, c: null}
initial: a
states:
  a:
    do: {agent: w, task: first}
    transitions:
      - target: b
        effect: ctx.a = out
  b:
    do: {agent: w, task: second}
    transitions:
      - target: c
        effect: ctx.b = out
  c:
    do: {agent: w, task: third}
    transitions:
      - target: done
        effect: ctx.c = out
  done: {type: final, output: {a: "{{ ctx.a }}", b: "{{ ctx.b }}", c: "{{ ctx.c }}"}}
"""


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


def changed_snapshot(harness: Harness, run_id: str, old: str, new: str) -> None:
    """Edit the run's stored definition (what a resume loads) -- the 'someone changed the machine' case."""
    snapshot = harness.store.get_run(run_id)["definition"]
    text = snapshot["files"][snapshot["root"]]
    assert text.count(old) == 1, f"fixture: {old!r} not in the stored definition"
    snapshot["files"][snapshot["root"]] = text.replace(old, new)
    harness.store.update_run(run_id, definition=snapshot)


# ------------------------------------------------------------------ resume (§5.3, §5.5)

async def test_resume_never_repeats_a_finished_activity(harness):
    run_id, first = await crash_in(harness, {"m.yaml": CHAIN}, "b", {"a": "A1"})
    assert first.count("a") == 1

    second = FakeBackend({"a": "A2", "b": "B2", "c": "C2"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "succeeded", row["error"]
    assert second.count("a") == 0, "the finished activity a ran again"
    assert second.count("b") == 1, "the in-flight idempotent activity b must run again"
    assert row["output"] == {"a": "A1", "b": "B2", "c": "C2"}
    assert activity_rows(harness.store, run_id)["s1"]["data"]["meta"]["attempts"] == 1
    started_attempt = [r for r in harness.store.rows(run_id, kinds=("activity",)) if r["key"] == "s1"]
    assert started_attempt and started_attempt[0]["status"] == "done"


async def test_resume_continues_the_in_flight_attempt(harness):
    """§5.5: a crash is not a failed attempt -- the resumed activity continues the attempt it was in (so a
    composite's finished children replay under that attempt's keys) and the retry budget is unchanged."""
    run_id, _ = await crash_in(harness, {"m.yaml": CHAIN}, "b", {"a": "A1"})
    before = activity_rows(harness.store, run_id)["s1"]
    assert before["status"] == "started" and before["data"]["attempt"] == 1

    gate = asyncio.Event()
    second = FakeBackend({"b": held(gate, "B2"), "c": "C2"})
    manager = harness.manager()
    await manager.resume(run_id, backend=second)
    await until(lambda: second.count("b") == 1, what="b running again")
    assert activity_rows(harness.store, run_id)["s1"]["data"]["attempt"] == 1
    gate.set()
    await settle(manager, run_id)


TOOL_MACHINE = """\
stategraph: 1
id: m
context: {err: null}
initial: a
states:
  a:
    do: {agent: w, task: first}
    transitions: [{target: t}]
  t:
    do: {tool: story_create, args: {key: "{{ run.id }}"}}
    transitions:
      - target: done
      - trigger: error
        target: reconcile
        effect: |
          ctx.err = {"type": error.type, "data": error.data}
  done: {type: final}
  reconcile: {type: final, output: "{{ ctx.err }}"}
"""


async def test_in_flight_tool_is_not_started_again(harness):
    run_id, first = await crash_in(harness, {"m.yaml": TOOL_MACHINE}, "t", {"a": "A1"})
    assert first.calls[-1]["args"] == {"key": run_id}

    second = FakeBackend({"a": "A2", "t": {"id": 7}})
    row = await resume(harness, run_id, second)

    assert second.count("t") == 0, "a non-idempotent tool in flight at the crash was started again"
    assert row["final_state"] == "reconcile", row
    assert row["output"]["type"] == "interrupted"


async def test_interrupted_error_data_is_the_rendered_inputs(harness):
    run_id, _ = await crash_in(harness, {"m.yaml": TOOL_MACHINE}, "t", {"a": "A1"})
    row = await resume(harness, run_id, FakeBackend({"t": {"id": 7}}))
    assert row["output"]["data"] == {"tool": "story_create", "args": {"key": run_id}}, row["output"]


BRANCH = """\
stategraph: 1
id: m
initial: a
states:
  a:
    do: {agent: w, task: first}
    transitions:
      - target: b1
        guard: out == "A"
      - target: b2
        guard: else
  b1:
    do: {agent: w, task: next}
    transitions: [{target: done}]
  b2:
    do: {agent: w, task: next}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_changed_guard_after_a_crash_diverges(harness):
    """b1 and b2 render the same input and no ctx changes: only the recorded state path can tell them apart."""
    run_id, _ = await crash_in(harness, {"m.yaml": BRANCH}, "b1", {"a": "A"})
    changed_snapshot(harness, run_id, 'guard: out == "A"', 'guard: out == "Z"')

    second = FakeBackend({"a": "A", "b1": "one", "b2": "two"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "failed", row
    assert row["error"]["type"] == "diverged"
    assert "s1" in row["error"]["message"]
    assert second.count("b2") == 0, "a diverged run must never continue on the other path"


async def test_changed_effect_after_a_crash_diverges_on_the_context_hash(harness):
    run_id, _ = await crash_in(harness, {"m.yaml": CHAIN}, "b", {"a": "A"})
    changed_snapshot(harness, run_id, "effect: ctx.a = out", "effect: ctx.a = out + '!'")

    row = await resume(harness, run_id, FakeBackend({"b": "B", "c": "C"}))

    assert row["status"] == "failed", row
    assert row["error"]["type"] == "diverged"
    assert "s1:step" in row["error"]["message"] and "differ from the recorded run" in row["error"]["message"]


async def test_changed_guard_into_another_wait_state_diverges(harness):
    files = {"m.yaml": """\
        stategraph: 1
        id: m
        events: {go: {}}
        initial: a
        states:
          a:
            do: {agent: w, task: first}
            transitions:
              - target: w1
                guard: out == "A"
              - target: w2
                guard: else
          w1:
            transitions: [{trigger: go, target: done1}]
          w2:
            transitions: [{trigger: go, target: done2}]
          done1: {type: final}
          done2: {type: final}
        """}
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=FakeBackend({"a": "A"}))
    await until(lambda: manager.live[run_id].ctx.status == "waiting", what="w1")
    await manager.shutdown()
    changed_snapshot(harness, run_id, 'guard: out == "A"', 'guard: out == "Z"')

    second = harness.manager()
    await second.resume(run_id, backend=FakeBackend({"a": "A"}))
    await until(lambda: run_id not in second.live or second.live[run_id].ctx.status == "waiting", what="resume")
    row = harness.store.get_run(run_id)
    assert row["status"] == "failed" and row["error"]["type"] == "diverged", (
        f"resumed on another path: status {row['status']}, state {second.live[run_id].root.leaf.name}")


async def test_run_timeout_counts_the_running_time_before_a_crash(harness):
    files = {"m.yaml": CHAIN.replace("initial: a", "limits: {timeout: 1.0}\ninitial: a")}
    backend = FakeBackend({"a": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend)
    await until(lambda: backend.count("a") == 1, what="a in flight")
    await asyncio.sleep(0.8)
    await manager.shutdown()

    gate = asyncio.Event()
    second = harness.manager()
    await second.resume(run_id, backend=FakeBackend({"a": held(gate, "A"), "b": "B", "c": "C"}))
    await asyncio.sleep(0.8)  # 1.6 s of running time in total
    gate.set()
    row = await settle(second, run_id)
    assert row["status"] == "failed" and row["error"]["type"] == "timed_out", row["status"]


# ------------------------------------------------------------------ fork (§5.6)

async def test_fork_replays_the_prefix_and_runs_the_rest_live(harness):
    manager = harness.manager()
    original = FakeBackend({"a": "A1", "b": "B1", "c": "C1"})
    run_id = await manager.start(runnable({"m.yaml": CHAIN}), backend=original)
    assert (await settle(manager, run_id))["status"] == "succeeded"

    forked = FakeBackend({"a": "A2", "b": "B2", "c": "C2"})
    fork_id = await manager.fork(run_id, at_step=1, backend=forked)
    row = await settle(manager, fork_id)

    assert row["status"] == "succeeded", row["error"]
    assert (forked.count("a"), forked.count("b"), forked.count("c")) == (0, 1, 1)
    assert row["output"] == {"a": "A1", "b": "B2", "c": "C2"}
    assert (row["parent_run"], row["fork_step"]) == (run_id, 1)
    assert row["session_id"] == f"sg_{fork_id}", "a fork has its own session"


async def test_fork_onto_the_current_definition(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": CHAIN}), backend=FakeBackend({"a": "A1", "b": "B1", "c": "C1"}))
    await settle(manager, run_id)

    fixed = CHAIN.replace("task: third", "task: third_fixed")
    forked = FakeBackend({"b": "B2", "c": "C2"})
    fork_id = await manager.fork(run_id, at_step=2, tree=runnable({"m.yaml": fixed}), backend=forked)
    row = await settle(manager, fork_id)

    assert row["status"] == "succeeded", row["error"]
    assert (forked.count("b"), forked.count("c")) == (0, 1)
    assert forked.calls[0]["task"] == "third_fixed", "the fork must run the current definition after step 2"


async def test_fork_whose_new_definition_changes_the_prefix_diverges(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": CHAIN}), backend=FakeBackend({"a": "A1", "b": "B1", "c": "C1"}))
    await settle(manager, run_id)

    changed = CHAIN.replace("task: first", "task: first_changed")
    forked = FakeBackend({"a": "A2", "b": "B2", "c": "C2"})
    fork_id = await manager.fork(run_id, at_step=2, tree=runnable({"m.yaml": changed}), backend=forked)
    row = await settle(manager, fork_id)

    assert row["status"] == "failed" and row["error"]["type"] == "diverged", row
    assert "journal key s0" in row["error"]["message"]
    assert len(forked.calls) == 0


# ------------------------------------------------------------------ leases (§5.7)

def _row(harness: Harness, run_id: str, *, owner: str, lease: float, status: str = "running", run_key=None):
    seed_run(harness.store, run_id, {"m.yaml": CHAIN}, owner=owner, lease=lease, status=status, run_key=run_key)


async def test_take_lease_refuses_a_live_foreign_lease(harness):
    _row(harness, "live1", owner="host-a:1", lease=60)
    _row(harness, "dead1", owner="host-a:1", lease=-60)

    assert harness.store.take_lease("live1", "host-b:2", utc_at(60), now=utc_at(0)) is False
    assert harness.store.take_lease("live1", "host-a:1", utc_at(60), now=utc_at(0)) is True, "the owner renews"
    assert harness.store.take_lease("dead1", "host-b:2", utc_at(60), now=utc_at(0)) is True
    assert harness.store.get_run("dead1")["owner"] == "host-b:2"


async def test_resume_refuses_a_run_another_process_holds(harness):
    _row(harness, "live2", owner="host-a:1", lease=60)
    with pytest.raises(ValueError, match="owned by host-a:1"):
        await harness.manager().resume("live2", backend=FakeBackend())


async def test_sweep_marks_only_expired_runs(harness):
    _row(harness, "fresh", owner="host-a:1", lease=60)
    _row(harness, "stale", owner="host-a:1", lease=-60)
    _row(harness, "stale_waiting", owner="host-a:1", lease=-60, status="waiting")
    _row(harness, "ended", owner="host-a:1", lease=-60, status="succeeded")

    swept = harness.manager().sweep_expired()

    assert sorted(swept) == ["stale", "stale_waiting"]
    status = {run_id: harness.store.get_run(run_id)["status"] for run_id in ("fresh", "stale", "ended")}
    assert status == {"fresh": "running", "stale": "interrupted", "ended": "succeeded"}


# ------------------------------------------------------------------ run_key (§5.7)

WAITING_MACHINE = """\
stategraph: 1
id: m
events: {go: {}}
initial: w
states:
  w:
    transitions: [{trigger: go, target: done}]
  done: {type: final}
"""


def _server(tmp_path) -> StateGraphServer:
    from agent_system.config.models import AgentSystemConfig

    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    (tmp_path / "machines" / "m.yaml").write_text(WAITING_MACHINE, encoding="utf-8")
    return server


async def test_run_key_attaches_then_resumes(tmp_path):
    process_one = _server(tmp_path)
    first = await process_one.service.start_run("m", mock_only=True, run_key="req-1")
    run_id = first["run_id"]
    await until(lambda: process_one.run_manager.live[run_id].ctx.status == "waiting", what="the wait state")

    again = await process_one.service.start_run("m", mock_only=True, run_key="req-1")
    assert again == {"run_id": run_id, "attached": True}

    await process_one.stop_plugin()  # the process goes away; the run is interrupted
    process_two = _server(tmp_path)
    try:
        resumed = await process_two.service.start_run("m", mock_only=True, run_key="req-1")
        assert resumed == {"run_id": run_id, "resumed": True}
        await until(lambda: run_id in process_two.run_manager.live
                    and process_two.run_manager.live[run_id].ctx.status == "waiting", what="the resumed wait")
        assert len([r for r in process_two.run_store.list_runs() if r["run_key"] == "req-1"]) == 1
    finally:
        await process_two.stop_plugin()


async def test_run_key_of_a_run_another_process_owns_starts_no_duplicate(tmp_path):
    server = _server(tmp_path)
    try:
        snapshot = runnable({"m.yaml": WAITING_MACHINE}).snapshot()
        server.run_store.create_run("elsewhere", "m", snapshot, params={}, mocks={"mocks": {}, "mock_only": True},
                                    owner="host-a:1", lease_until=utc_at(60), status="waiting", run_key="req-2",
                                    session_id="sg_elsewhere")
        try:
            await server.service.start_run("m", mock_only=True, run_key="req-2")
        except ServiceError:
            pass  # refusing is an acceptable answer; starting a second run is not
        keyed = [r["id"] for r in server.run_store.list_runs() if r["run_key"] == "req-2"]
        assert keyed == ["elsewhere"], f"duplicate runs for one run_key: {keyed}"
    finally:
        await server.stop_plugin()
