"""Regressions for the engine findings of the second review round (docs/stategraph_design.md §3, §5).

One test (or one parametrised group) per fixed finding E1-E9. Every machine
runs through the real RunManager on a RunStore under tmp_path; only the
boundary behind the engine is FakeBackend. A "crash" is a real
``RunManager.shutdown()`` and a resume is a NEW manager on the same store.

Mutation checks run (each turned the named tests red, then was restored byte-exactly):
- activity.child_key: the attempt left out of the key (``f"{self.key}/{label}"``) -> test_retry_runs_the_failed_child_again[*]
- activity.execute: a crash counts as a failed attempt (``first`` + 1)             -> test_resume_mid_attempt_replays_the_finished_branches
- runner.wait_event: deadline keyed ``<state>#<visits>`` again                      -> test_wait_in_a_reentered_composite_*, test_fork_*_fresh_deadline
- activity._attempt: ``deadline.expired()`` -> ``True``                            -> test_timeout_error_of_the_kind_itself[own_timeout_error_with_do_timeout]
- activity._attempt: TimeoutError without do.timeout reported as the attempt's      -> test_timeout_error_of_the_kind_itself[no_do_timeout]
- journal.renew: the ``owner = ?`` fence dropped                                    -> test_a_process_that_lost_its_run_stops_without_writing[heartbeat]
- runner.RunContext.persist: ``fence=self.owner`` -> ``fence=None``                 -> test_a_process_that_lost_its_run_stops_without_writing[persist]
- journal.renew: the status restore (CASE ... 'interrupted') dropped                -> test_the_heartbeat_undoes_a_sweep_of_its_own_live_run
- journal.take_lease: ``OR status NOT IN (active)`` escape added back              -> test_take_lease_refuses_a_live_foreign_lease_even_when_interrupted
- activity.ActivityRun.scope: the live ctx instead of a deep copy                   -> test_a_call_that_mutates_sg_ctx_leaves_the_run_ctx_alone
- runner._load_journal: trace step rows do not advance the replay frontier         -> test_replay_is_silent_at_a_do_less_state_between_activities
- debugger.pause: lock and ``paused is record`` guard both removed (the original)  -> test_a_pause_taken_during_a_continue_stays_visible
- debugger.pause: the ``_pausing`` lock removed                                     -> test_a_second_frame_waits_while_the_first_is_paused
  (the race test alone stays green without the lock -- the ``paused is record`` guard also fixes that ordering;
  removing only the guard keeps both green: under the lock it can never see another frame's record)
- activity._normalised: TypeError not turned into not_serialisable                 -> test_a_result_with_tuple_keys_is_not_serialisable
- code.plain: dict views not materialised                                          -> test_map_over_dict_items
- runner.control: terminate of a run not live here refused again                    -> test_terminate_of_an_interrupted_run_cancels_it
"""

from __future__ import annotations

import asyncio
import time

import pytest

from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, activity_rows, held,
                                                         runnable, sequence, settle, tool_config, until, utc_at)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


SUB = """\
stategraph: 1
id: sub
context: {got: null}
initial: w
states:
  w:
    do: {agent: w, task: go}
    transitions:
      - target: fin
        effect: ctx.got = out
  fin: {type: final, output: "{{ ctx.got }}"}
"""


# ------------------------------------------------------------------ E1: retry of a composite activity

RETRIED = """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
context: {out: null}
initial: p
states:
  p:
    DO
    transitions:
      - target: done
        effect: ctx.out = out
      - trigger: error
        target: failed
  done: {type: final, output: "{{ ctx.out }}"}
  failed: {type: final, status: failed}
"""


@pytest.mark.parametrize("do,path,output", [
    ("do: {parallel: {x: {agent: w, task: go}}, retry: {attempts: 3}}", "p/x", {"x": "OK"}),
    ('do: {map: "[1]", each: {agent: w, task: "go {{ item }}"}, retry: {attempts: 3}}', "p/0", ["OK"]),
    ("do: {machine: sub, retry: {attempts: 3}}", "p/w", "OK"),
], ids=["parallel", "map", "machine"])
async def test_retry_runs_the_failed_child_again(harness, do, path, output):
    """E1: attempt 2 of a composite activity runs its children live, it does not replay attempt 1's failure."""
    backend = FakeBackend({path: sequence(ActivityError("agent_failed", "transient"), "OK")})
    row = await harness.run({"m.yaml": RETRIED.replace("DO", do), "sub.yaml": SUB}, backend=backend)

    assert backend.count(path) == 2, f"the retry did not call the child again ({backend.count(path)} call)"
    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == output


PARALLEL_TWO = """\
stategraph: 1
id: m
context: {out: null}
initial: p
states:
  p:
    do: {parallel: {x: {agent: w, task: x}, y: {agent: w, task: y}}, retry: {attempts: 2}}
    transitions:
      - target: done
        effect: ctx.out = out
  done: {type: final, output: "{{ ctx.out }}"}
"""


async def test_resume_mid_attempt_replays_the_finished_branches(harness):
    """E1: a crash inside attempt 1 continues attempt 1 -- the branch that finished replays, only y runs again."""
    first = FakeBackend({"p/x": "X1", "p/y": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": PARALLEL_TWO}), backend=first)
    await until(lambda: first.count("p/y") == 1
                and activity_rows(harness.store, run_id).get("s0/b.x", {}).get("status") == "done",
                what="x finished, y in flight")
    await manager.shutdown()

    second = FakeBackend({"p/x": "X2", "p/y": "Y2"})
    resumed = harness.manager()
    await resumed.resume(run_id, backend=second)
    row = await settle(resumed, run_id)

    assert row["status"] == "succeeded", row["error"]
    assert second.count("p/x") == 0, "the branch that finished before the crash ran again"
    assert second.count("p/y") == 1
    assert row["output"] == {"x": "X1", "y": "Y2"}


# ------------------------------------------------------------------ E2: wait deadlines

REENTERED_WAIT = """\
stategraph: 1
id: m
events: {go: {}}
context: {n: 0}
initial: c
states:
  c:
    initial: w
    states:
      w:
        timeout: 0.4
        transitions:
          - trigger: go
            target: fin
      fin: {type: final}
    transitions:
      - target: done
      - trigger: error
        target: x
        effect: ctx.n += 1
  x:
    max_visits: 5
    transitions:
      - target: c
        guard: ctx.n < 2
      - target: done
        guard: else
  done: {type: final, output: "{{ ctx.n }}"}
"""


async def test_wait_in_a_reentered_composite_gets_a_fresh_deadline(harness):
    """E2: re-entering c enters w anew; its wait must not inherit the first entry's expired deadline."""
    started = time.monotonic()
    row = await harness.run({"m.yaml": REENTERED_WAIT})
    elapsed = time.monotonic() - started

    assert row["status"] == "succeeded" and row["output"] == 2, row
    timers = [r for r in harness.store.rows(row["id"], kinds=("timer",))]
    assert len(timers) == 2, "fixture: both waits must time out"
    assert elapsed >= 0.75, f"two 0.4 s waits took {elapsed:.2f} s: the second one timed out at once"


FORKED_WAIT = """\
stategraph: 1
id: m
events: {go: {}}
initial: a
states:
  a:
    do: {agent: w, task: first}
    transitions: [{target: w}]
  w:
    timeout: 1.0
    transitions:
      - trigger: go
        target: ok
      - trigger: error
        target: late
  ok: {type: final, output: ok}
  late: {type: final, output: late}
"""


async def test_fork_before_a_wait_state_gets_a_fresh_deadline(harness):
    """E2: a fork at the step that enters w waits a fresh timeout, not the source run's expired one."""
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": FORKED_WAIT}), backend=FakeBackend({"a": "A"}))
    source = await settle(manager, run_id)
    assert source["final_state"] == "late", "fixture: the source run's wait timed out"

    fork_id = await manager.fork(run_id, at_step=1, backend=FakeBackend({"a": "A2"}))
    await until(lambda: fork_id not in manager.live or manager.live[fork_id].ctx.status == "waiting",
                what="the fork's wait")
    await asyncio.sleep(0.15)
    assert fork_id in manager.live, (
        f"the fork ended {manager.store.get_run(fork_id)['final_state']!r} before its own timeout could run out")
    assert manager.send_event(fork_id, "go")["accepted"] is True
    row = await settle(manager, fork_id)
    assert row["final_state"] == "ok", row


# ------------------------------------------------------------------ E3: TimeoutError raised by the kind

TIMEOUTS = """\
stategraph: 1
id: m
python: m.py
context: {err: null}
initial: a
states:
  a:
    DO
    transitions:
      - target: done
      - trigger: error
        target: handled
        effect: ctx.err = error.type
  done: {type: final}
  handled: {type: final, output: "{{ ctx.err }}"}
"""
TIMEOUTS_PY = """\
import asyncio


def socket_timeout():
    raise TimeoutError("socket read timed out")


async def slow():
    await asyncio.sleep(5)
"""


@pytest.mark.parametrize("do,expected", [
    ("do: {call: socket_timeout}", "call_failed"),
    ("do: {call: socket_timeout, timeout: 5s}", "call_failed"),
    ("do: {call: slow, timeout: 0.2s}", "timeout"),
], ids=["no_do_timeout", "own_timeout_error_with_do_timeout", "the_attempt_deadline"])
async def test_timeout_error_of_the_kind_itself(harness, do, expected):
    """E3: only the attempt's own deadline is 'timeout'; a TimeoutError the call raises is call_failed."""
    row = await harness.run({"m.yaml": TIMEOUTS.replace("DO", do), "m.py": TIMEOUTS_PY})
    assert row["status"] == "succeeded", row["error"]
    assert (row["final_state"], row["output"]) == ("handled", expected)


# ------------------------------------------------------------------ E4: lease fencing

WAITING = """\
stategraph: 1
id: m
events: {go: {}}
initial: w
states:
  w:
    transitions: [{trigger: go, target: done}]
  done: {type: final, output: finished}
"""


@pytest.mark.parametrize("via", ["heartbeat", "persist"])
async def test_a_process_that_lost_its_run_stops_without_writing(harness, via):
    """E4: A's lease ran out, B swept and resumed the run; A's next fenced write loses it and A's copy stops."""
    manager_a = harness.manager()
    run_id = await manager_a.start(runnable({"m.yaml": WAITING}), backend=FakeBackend())
    await until(lambda: manager_a.live[run_id].ctx.status == "waiting", what="A waits")
    harness.store.update_run(run_id, lease_until=utc_at(-120))  # A's loop stalled past its lease

    manager_b = harness.manager()
    assert manager_b.sweep_expired() == [run_id]
    await manager_b.resume(run_id, backend=FakeBackend())
    await until(lambda: run_id in manager_b.live and manager_b.live[run_id].ctx.status == "waiting", what="B waits")
    assert harness.store.get_run(run_id)["owner"] == manager_b.owner

    if via == "heartbeat":
        manager_a.renew_leases()
    else:
        manager_a.live[run_id].ctx.persist()
    await until(lambda: run_id not in manager_a.live, what="A's copy to stop")

    row = harness.store.get_run(run_id)
    assert (row["owner"], row["status"]) == (manager_b.owner, "waiting"), "A wrote over the run B owns"
    manager_b.send_event(run_id, "go")
    final = await settle(manager_b, run_id)
    assert (final["status"], final["output"]) == ("succeeded", "finished")


async def test_the_heartbeat_undoes_a_sweep_of_its_own_live_run(harness):
    """E4: a sweep that marked a live run interrupted (the owner's loop stalled) is undone by its heartbeat."""
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": WAITING}), backend=FakeBackend())
    await until(lambda: manager.live[run_id].ctx.status == "waiting", what="the wait")
    harness.store.update_run(run_id, lease_until=utc_at(-120))
    assert manager.sweep_expired() == [run_id]
    assert harness.store.get_run(run_id)["status"] == "interrupted", "fixture: the sweep marked the live run"

    manager.renew_leases()

    row = harness.store.get_run(run_id)
    assert row["status"] == "waiting", "the live run still reads interrupted after its heartbeat"
    assert row["lease_until"] > utc_at(0) and run_id in manager.live


async def test_take_lease_refuses_a_live_foreign_lease_even_when_interrupted(harness):
    """E4: status is no escape -- only an expired lease (or our own) can be taken."""
    snapshot = runnable({"m.yaml": WAITING}).snapshot()
    harness.store.create_run("r1", "m", snapshot, params={}, mocks={}, owner="host-a:1", lease_until=utc_at(60),
                             status="interrupted", session_id="sg_r1")

    assert harness.store.take_lease("r1", "host-b:2", utc_at(60), now=utc_at(0)) is False
    with pytest.raises(ValueError, match="owned by host-a:1"):
        await harness.manager().resume("r1", backend=FakeBackend())
    assert harness.store.get_run("r1")["owner"] == "host-a:1"


# ------------------------------------------------------------------ E5: a call mutating sg.ctx

MUTATING_CALL = """\
stategraph: 1
id: m
python: m.py
context: {items: [1, 2], n: null}
initial: a
states:
  a:
    do: {call: bump}
    transitions:
      - target: b
        effect: ctx.n = out
  b:
    do: {agent: w, task: "{{ ctx.items }}"}
    transitions: [{target: done}]
  done: {type: final, output: {items: "{{ ctx.items }}", n: "{{ ctx.n }}"}}
"""
MUTATING_PY = """\
def bump(sg):
    sg.ctx["items"].append(99)
    return len(sg.ctx["items"])
"""


async def test_a_call_that_mutates_sg_ctx_leaves_the_run_ctx_alone(harness):
    """E5: sg gets a detached copy; the change lives only in the call, so a crash/resume does not diverge."""
    files = {"m.yaml": MUTATING_CALL, "m.py": MUTATING_PY}
    first = FakeBackend({"b": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("b") == 1, what="b in flight")
    assert first.calls[0]["task"] == "[1, 2]", "the call's mutation reached the run's ctx"
    await manager.shutdown()

    resumed = harness.manager()
    await resumed.resume(run_id, backend=FakeBackend({"b": "B"}))
    row = await settle(resumed, run_id)
    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == {"items": [1, 2], "n": 3}, "n: the call itself saw its own change"


# ------------------------------------------------------------------ E6: replay frontier

THREE_STEPS = """\
stategraph: 1
id: m
context: {n: 0}
initial: a
states:
  a:
    do: {agent: w, task: first}
    transitions: [{target: b}]
  b:
    entry: ctx.n += 1
    transitions: [{target: c}]
  c:
    do: {agent: w, task: "third {{ ctx.n }}"}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.n }}"}
"""


async def test_replay_is_silent_at_a_do_less_state_between_activities(harness):
    """E6: b has no activity, but its steps are in the journal -- its breakpoints stay silent in the replay."""
    first = FakeBackend({"a": "A", "c": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": THREE_STEPS}), backend=first)
    await until(lambda: first.count("c") == 1, what="c in flight")
    await manager.shutdown()
    manager.set_points(run_id, breakpoints=["b", "b@exit", "c"])

    resumed = harness.manager()
    await resumed.resume(run_id, backend=FakeBackend({"c": "C"}))
    debugger = resumed.live[run_id].ctx.debugger
    await until(lambda: run_id not in resumed.live or debugger.paused is not None, what="the first pause")
    assert debugger.paused is not None, "fixture: c's enter is live and must pause"
    assert (debugger.paused["state"], debugger.paused["hook"]) == ("c", "enter"), (
        f"the replay paused at a state the run had already passed: {debugger.paused}")
    resumed.control(run_id, "continue")
    row = await settle(resumed, run_id)
    assert (row["status"], row["output"]) == ("succeeded", 1)


# ------------------------------------------------------------------ E7: two frames pausing

TWO_FRAMES = """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
context: {out: null}
initial: p
states:
  p:
    do: {parallel: {x: {machine: sub}, y: {machine: sub}}}
    transitions:
      - target: done
        effect: ctx.out = out
  done: {type: final, output: "{{ ctx.out }}"}
"""
TWO_FRAMES_SUB = SUB.replace("initial: w", "initial: s").replace("  w:\n", "  s:\n")
X_FRAME, Y_FRAME = "s0/b.x/m/", "s0/b.y/m/"


async def _x_paused(harness: Harness, gate: asyncio.Event):
    backend = FakeBackend({"p/x/s": "X", "p/y/s": held(gate, "Y")})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": TWO_FRAMES, "sub.yaml": TWO_FRAMES_SUB}), backend=backend,
                                 breakpoints=[{"state": "s", "at": "exit"}])
    debugger = manager.live[run_id].ctx.debugger
    await until(lambda: debugger.paused is not None, what="x's pause")
    assert debugger.paused["frame"] == X_FRAME, "fixture: x answers first and pauses first"
    return manager, run_id, debugger


async def test_a_pause_taken_during_a_continue_stays_visible(harness):
    """E7: y reaches its breakpoint in the same loop turn as the continue of x's pause -- y's pause must show."""
    gate = asyncio.Event()
    manager, run_id, debugger = await _x_paused(harness, gate)
    first = debugger.paused

    gate.set()                           # y's answer and the operator's continue arrive together
    manager.control(run_id, "continue")
    await until(lambda: debugger.paused is not None and debugger.paused is not first, 3.0, "y's pause")

    assert debugger.paused["frame"] == Y_FRAME
    assert harness.store.get_run(run_id)["status"] == "paused"
    manager.control(run_id, "continue")
    row = await settle(manager, run_id)
    assert row["output"] == {"x": "X", "y": "Y"}


async def test_a_second_frame_waits_while_the_first_is_paused(harness):
    """E7: one visible pause at a time -- y's pause does not replace x's while x is being inspected."""
    gate = asyncio.Event()
    manager, run_id, debugger = await _x_paused(harness, gate)

    gate.set()
    await until(lambda: activity_rows(harness.store, run_id).get(Y_FRAME + "s0", {}).get("status") == "done",
                what="y's activity finished")
    await asyncio.sleep(0.05)  # y is at its exit hook now
    assert debugger.paused["frame"] == X_FRAME, "y's pause replaced x's while x was paused"
    assert manager.evaluate(run_id, "out") == "X"

    manager.control(run_id, "continue")
    await until(lambda: debugger.paused is not None and debugger.paused["frame"] == Y_FRAME, what="y's turn")
    assert manager.evaluate(run_id, "out") == "Y"
    manager.control(run_id, "continue")
    row = await settle(manager, run_id)
    assert row["output"] == {"x": "X", "y": "Y"}


# ------------------------------------------------------------------ E8: data that is not JSON

NOT_JSON = """\
stategraph: 1
id: m
python: m.py
context: {d: {a: 1, b: 2}, out: null}
initial: a
states:
  a:
    DO
    transitions:
      - target: done
        effect: ctx.out = out
      - trigger: error
        target: handled
        effect: ctx.out = error.type
  done: {type: final, output: "{{ ctx.out }}"}
  handled: {type: final, output: "{{ ctx.out }}"}
"""
NOT_JSON_PY = """\
def tuple_keys():
    return {(1, 2): "x"}


def echo(value):
    return value
"""


async def test_a_result_with_tuple_keys_is_not_serialisable(harness):
    """E8: the result cannot cross the journal boundary -- a catchable not_serialisable, not an engine crash."""
    row = await harness.run({"m.yaml": NOT_JSON.replace("DO", "do: {call: tuple_keys}"), "m.py": NOT_JSON_PY})
    assert row["status"] == "succeeded", row["error"]
    assert (row["final_state"], row["output"]) == ("handled", "not_serialisable")


async def test_map_over_dict_items(harness):
    """E8: map over a dict view (ctx.d.items()) runs one item per pair."""
    do = 'do: {map: "ctx.d.items()", as: pair, each: {call: echo, args: {value: "{{ pair }}"}}}'
    row = await harness.run({"m.yaml": NOT_JSON.replace("DO", do), "m.py": NOT_JSON_PY})
    assert row["status"] == "succeeded", row["error"]
    assert (row["final_state"], row["output"]) == ("done", [["a", 1], ["b", 2]])


# ------------------------------------------------------------------ E9: terminate an interrupted run

def _server(tmp_path) -> StateGraphServer:
    from agent_system.config.models import AgentSystemConfig

    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    (tmp_path / "machines" / "m.yaml").write_text(WAITING, encoding="utf-8")
    return server


async def test_terminate_of_an_interrupted_run_cancels_it(tmp_path):
    """E9: an interrupted run can be abandoned; its run_key then starts a fresh run instead of resuming it."""
    process_one = _server(tmp_path)
    first = await process_one.service.start_run("m", mock_only=True, run_key="req-9")
    run_id = first["run_id"]
    await until(lambda: process_one.run_manager.live[run_id].ctx.status == "waiting", what="the wait")
    await process_one.stop_plugin()
    assert process_one.run_store.get_run(run_id)["status"] == "interrupted", "fixture"

    process_two = _server(tmp_path)
    try:
        await process_two.service.control_run(run_id, "terminate")
        row = process_two.run_store.get_run(run_id)
        assert row["status"] == "cancelled" and row["error"]["type"] == "cancelled", row["status"]
        assert row["finished_at"]

        again = await process_two.service.start_run("m", mock_only=True, run_key="req-9")
        assert again["run_id"] != run_id and "resumed" not in again, again
    finally:
        await process_two.stop_plugin()
