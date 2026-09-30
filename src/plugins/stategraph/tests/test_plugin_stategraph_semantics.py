"""Run-to-completion semantics (docs/stategraph_design.md §3), driven through the real RunManager.

Every machine is YAML text, validated like service.start_run does, compiled and
run by ``RunManager`` on a ``RunStore`` under tmp_path. Agents and tools answer
from ``FakeBackend``; ``call`` activities run real companion functions.

Mutation checks run (each turned the named tests red, then was restored from a copy):
- interpreter._dispatch: completion candidates from the whole leaf chain         -> test_completion_is_local
- interpreter._dispatch: error candidates outer-first (no ``reversed``)          -> test_errors_are_offered_inner_first[inner]
- interpreter._resolve: choices resolved like junctions (static)                -> test_choice_sees_the_effect_junction_does_not[choice]
- interpreter._resolve: junctions not resolved statically                       -> test_choice_sees_the_effect_junction_does_not[junction]
- machine.lca: proper-ancestor rule dropped (a self-transition stays inside)    -> test_self_transition_exits_and_reenters
- interpreter._next_event: nested final's output not passed as out              -> test_composite_completion_binds_the_nested_final_output
- interpreter._dispatch: no pending no_transition on an unhandled completion     -> test_unhandled_completion_raises_no_transition
- interpreter._abort: ctx not restored                                           -> test_failing_effect_rolls_back_the_transition
- interpreter._abort: visits not restored                                        -> test_failing_entry_rolls_back_configuration_and_visits
- interpreter._abort: error while handling an error becomes a pending error      -> test_error_inside_error_handling_ends_the_frame
- interpreter._enter_node: ``return False`` removed on loop_limit (entry runs)   -> test_max_visits_refuses_the_entry_without_running_it
- interpreter._enter_node: descendant visit reset removed                        -> test_entering_a_composite_resets_nested_visits
- interpreter._fire: internal transition made external (self target)            -> test_internal_event_transition_neither_exits_nor_enters
- runner.send_event: routes to the first accepting frame instead of refusing    -> test_ambiguous_event_is_refused_until_a_frame_is_named
- runner.wait_event: an event for another trigger is taken anyway               -> test_an_event_the_frame_does_not_accept_stays_queued
- runner.wait_event: the deadline never fires                                    -> test_wait_timeout_raises_in_the_waiting_state
- builtin.join: siblings not cancelled on a fast failure                         -> test_parallel_fail_fast_cancels_the_other_branches
- builtin.join: collect raises like fast                                          -> test_parallel_fail_collect_runs_every_branch
- builtin.join: Semaphore(concurrency) -> Semaphore(len(children))               -> test_map_with_concurrency_one_runs_strictly_in_order
- activity.run_submachine: cause not passed on                                   -> test_submachine_failure_carries_the_cause
- activity.execute: the backoff sleep removed                                     -> test_retry_waits_the_backoff_between_attempts
- spec.RetrySpec.retries: ``on`` ignored                                          -> test_retry_on_limits_the_retried_types
- spec.NOT_RETRIED without "timeout"                                              -> test_timeout_is_not_retried_by_default
- interpreter._dispatch: step limit check removed                                 -> test_step_limit_ends_the_run_uncatchably
- interpreter._dispatch: ``raise RunAbort`` -> ``raise MachineFailed`` (catchable) -> test_step_limit_of_a_submachine_ends_the_run
- runner.set_status: waiting time counted as running                             -> test_run_timeout_counts_running_time_only
- interpreter.ACTIVITY_DEFAULTS emptied                                          -> test_activity_meta_names_are_bound[live]
"""

from __future__ import annotations

import asyncio
import textwrap
import time

import pytest

from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, held, runnable,
                                                         sequence, settle, until)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

HELPERS = """\
def one():
    return 1

def boom():
    raise ValueError("boom")
"""


def files(yaml: str, **others: str) -> dict[str, str]:
    """A machine ``m`` with the helper companion module."""
    result = {"m.yaml": "stategraph: 1\nid: m\npython: m.py\n" + textwrap.dedent(yaml), "m.py": HELPERS}
    result.update(others)
    return result


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


# ------------------------------------------------------------------ selection (§3.2)

async def test_completion_is_local(harness):
    """A leaf without a completion transition raises no_transition; its parent's completion does not fire."""
    row = await harness.run(files("""\
        context: {err: null}
        initial: outer
        states:
          outer:
            initial: leaf
            states:
              leaf:
                do: {call: one}
            transitions:
              - target: wrong
              - trigger: error
                target: caught
                effect: ctx.err = [error.type, error.state]
          wrong: {type: final, output: wrong}
          caught: {type: final, output: "{{ ctx.err }}"}
        """))
    assert row["final_state"] == "caught", row
    assert row["output"] == ["no_transition", "leaf"]


INNER_FIRST = """\
context: {by: null}
initial: outer
states:
  outer:
    initial: leaf
    states:
      leaf:
        do: {call: boom}
        transitions:
          - trigger: LEAF_TRIGGER
            target: handled
            effect: ctx.by = "inner"
      handled: {type: final}
    transitions:
      - target: done
      - trigger: error
        target: done
        effect: ctx.by = "outer"
  done: {type: final, output: "{{ ctx.by }}"}
"""


@pytest.mark.parametrize("leaf_trigger,expected", [("error", "inner"), ("nothing", "outer")],
                         ids=["inner", "outer"])
async def test_errors_are_offered_inner_first(harness, leaf_trigger, expected):
    """The leaf's own error transition wins; without one the enclosing composite's catches it."""
    machine = INNER_FIRST.replace("LEAF_TRIGGER", leaf_trigger)
    if leaf_trigger == "nothing":
        machine = "events: {nothing: {}}\n" + machine
    row = await harness.run(files(machine))
    assert row["status"] == "succeeded", row
    assert row["output"] == expected


@pytest.mark.parametrize("pseudo,expected", [("choice", "effect"), ("junction", "old")])
async def test_choice_sees_the_effect_junction_does_not(harness, pseudo, expected):
    """A choice's guards run after the incoming effect; a junction's are chosen with the transition."""
    row = await harness.run(files(f"""\
        context: {{x: 0}}
        initial: a
        states:
          a:
            do: {{call: one}}
            transitions:
              - target: branch
                effect: ctx.x = 5
          branch:
            type: {pseudo}
            transitions:
              - target: saw_effect
                guard: ctx.x == 5
              - target: saw_old
                guard: else
          saw_effect: {{type: final, output: effect}}
          saw_old: {{type: final, output: old}}
        """))
    assert row["output"] == expected, row


async def test_self_transition_exits_and_reenters(harness):
    row = await harness.run(files("""\
        context: {entries: 0, exits: 0}
        initial: a
        states:
          a:
            max_visits: 5
            entry: ctx.entries += 1
            exit: ctx.exits += 1
            do: {call: one}
            transitions:
              - target: a
                guard: ctx.entries < 3
              - target: done
                guard: else
          done:
            type: final
            output: {entries: "{{ ctx.entries }}", exits: "{{ ctx.exits }}", visits: "{{ run.visits['a'] }}"}
        """))
    assert row["output"] == {"entries": 3, "exits": 3, "visits": 3}, row


async def test_composite_completion_binds_the_nested_final_output(harness):
    row = await harness.run(files("""\
        context: {got: null}
        initial: c
        states:
          c:
            initial: work
            states:
              work:
                do: {call: one}
                transitions:
                  - target: fin
                    effect: ctx.got = "inner"
              fin:
                type: final
                output: {value: "{{ ctx.got }}", n: 2}
            transitions:
              - target: done
                effect: ctx.got = out
          done: {type: final, output: "{{ ctx.got }}"}
        """))
    assert row["output"] == {"value": "inner", "n": 2}, row


async def test_unhandled_completion_raises_no_transition(harness):
    row = await harness.run(files("""\
        initial: a
        states:
          a:
            do: {call: one}
            transitions:
              - target: done
                guard: out == 2
          done: {type: final}
        """))
    assert row["status"] == "failed"
    assert row["error"]["type"] == "no_transition"
    assert row["error"]["state"] == "a"


# ------------------------------------------------------------------ atomic transitions (§3.2)

async def test_failing_effect_rolls_back_the_transition(harness):
    row = await harness.run(files("""\
        context: {x: 0, seen: null}
        initial: a
        states:
          a:
            do: {call: one}
            transitions:
              - target: b
                effect: |
                  ctx.x = 1
                  ctx.x = 1 / 0
              - trigger: error
                target: caught
                effect: ctx.seen = [error.type, error.state, ctx.x]
          b:
            entry: ctx.x = 99
            transitions: [{target: done}]
          caught: {type: final, output: "{{ ctx.seen }}"}
          done: {type: final}
        """))
    assert row["final_state"] == "caught", row
    assert row["output"] == ["action_failed", "a", 0]


async def test_failing_entry_rolls_back_configuration_and_visits(harness):
    """The entry of the target raises: ctx, the active configuration and visit counts are those of before."""
    row = await harness.run(files("""\
        context: {x: 0, seen: null}
        initial: a
        states:
          a:
            do: {call: one}
            transitions:
              - target: b
                effect: ctx.x = 1
              - trigger: error
                target: caught
                effect: ctx.seen = [error.type, ctx.x, run.visits.get("b")]
          b:
            entry: |
              ctx.x = 2
              ctx.x = [][1]
            transitions: [{target: done}]
          caught: {type: final, output: "{{ ctx.seen }}"}
          done: {type: final}
        """))
    assert row["final_state"] == "caught", "the error must be raised in the source leaf a, whose handler catches it"
    assert row["output"] == ["action_failed", 0, None]


async def test_error_inside_error_handling_ends_the_frame(harness):
    """The error transition's effect fails: the frame ends; the enclosing error transition is not tried."""
    row = await harness.run(files("""\
        context: {x: 0}
        initial: outer
        states:
          outer:
            initial: a
            states:
              a:
                do: {call: boom}
                transitions:
                  - trigger: error
                    target: b
                    effect: ctx.x = 1 / 0
              b: {type: final}
            transitions:
              - target: done
              - trigger: error
                target: rescued
          done: {type: final}
          rescued: {type: final, output: rescued}
        """))
    assert row["status"] == "failed", row
    assert row["final_state"] != "rescued"
    assert row["error"]["type"] == "action_failed"
    assert row["error"]["cause"]["type"] == "call_failed"


# ------------------------------------------------------------------ counters (§3.7)

async def test_max_visits_refuses_the_entry_without_running_it(harness):
    backend = FakeBackend({"a": "ok"})
    row = await harness.run(files("""\
        context: {entries: 0, err: null}
        initial: a
        states:
          a:
            max_visits: 2
            entry: ctx.entries += 1
            do: {agent: w, task: go}
            transitions:
              - target: a
              - trigger: error
                target: stop
                effect: ctx.err = [error.type, error.state, error.visits]
          stop: {type: final, output: {entries: "{{ ctx.entries }}", err: "{{ ctx.err }}"}}
        """), backend=backend)
    assert row["output"] == {"entries": 2, "err": ["loop_limit", "a", 3]}, row
    assert backend.count("a") == 2, "the refused third entry must not start the activity"


async def test_entering_a_composite_resets_nested_visits(harness):
    row = await harness.run(files("""\
        context: {rounds: 0}
        initial: c
        states:
          c:
            max_visits: 3
            initial: x
            states:
              x:
                max_visits: 1
                do: {call: one}
                transitions: [{target: fin}]
              fin: {type: final}
            transitions:
              - target: c
                guard: ctx.rounds < 2
                effect: ctx.rounds += 1
              - target: done
                guard: else
          done: {type: final, output: "{{ ctx.rounds }}"}
        """))
    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == 2


# ------------------------------------------------------------------ events (§3.4)

WAITER = """\
events: {note: {}, go: {}}
context: {entries: 0, notes: []}
initial: w
states:
  w:
    entry: ctx.entries += 1
    transitions:
      - trigger: note
        effect: ctx.notes.append(event.data)
      - trigger: go
        target: done
  done: {type: final, output: {entries: "{{ ctx.entries }}", notes: "{{ ctx.notes }}"}}
"""


def _status(manager, run_id):
    return manager.live[run_id].ctx.status if run_id in manager.live else None


async def test_internal_event_transition_neither_exits_nor_enters(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable(files(WAITER)))
    await until(lambda: _status(manager, run_id) == "waiting", what="the wait state")

    assert manager.send_event(run_id, "note", "first") == {"accepted": True, "frame": "", "queued": False}
    await until(lambda: manager.live[run_id].root.ctx["notes"] == ["first"], what="the internal transition")
    await until(lambda: _status(manager, run_id) == "waiting", what="waiting again")
    manager.send_event(run_id, "note", "second")
    manager.send_event(run_id, "go")
    row = await settle(manager, run_id)

    assert row["output"] == {"entries": 1, "notes": ["first", "second"]}, row


SUB_WAITER = """\
stategraph: 1
id: sub
params: {label: {type: string, required: true}}
events: {approve: {}}
initial: w
states:
  w:
    transitions:
      - trigger: approve
        target: ok
  ok: {type: final, output: "{{ params.label }} approved"}
"""


async def test_event_reaches_a_waiting_submachine_inside_parallel(harness):
    """The submachine waits in one branch while the root frame runs the parallel activity (§3.4 Frames)."""
    backend = FakeBackend({"work/other": "other done"})
    manager = harness.manager()
    run_id = await manager.start(runnable(files("""\
        imports: {sub: ./sub.yaml}
        context: {out: null}
        initial: work
        states:
          work:
            do:
              parallel:
                left: {machine: sub, params: {label: left}}
                other: {agent: w, task: go}
            transitions:
              - target: done
                effect: ctx.out = out
          done: {type: final, output: "{{ ctx.out }}"}
        """, **{"sub.yaml": SUB_WAITER})), backend=backend)
    await until(lambda: _status(manager, run_id) == "waiting" and backend.count("work/other") == 1,
                what="the waiting branch")

    routed = manager.send_event(run_id, "approve")
    row = await settle(manager, run_id)

    assert routed == {"accepted": True, "frame": "s0/b.left/m/", "queued": False}
    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == {"left": "left approved", "other": "other done"}


async def test_ambiguous_event_is_refused_until_a_frame_is_named(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable(files("""\
        imports: {sub: ./sub.yaml}
        context: {out: null}
        initial: work
        states:
          work:
            do:
              parallel:
                left: {machine: sub, params: {label: left}}
                right: {machine: sub, params: {label: right}}
            transitions:
              - target: done
                effect: ctx.out = out
          done: {type: final, output: "{{ ctx.out }}"}
        """, **{"sub.yaml": SUB_WAITER})))
    ctx = manager.live[run_id].ctx
    await until(lambda: sum(1 for f in ctx.frames if "approve" in f.accepts()) == 2, what="both waiting frames")

    refused = manager.send_event(run_id, "approve")
    assert refused["accepted"] is False and "several frames" in refused["reason"], refused
    assert manager.send_event(run_id, "approve", frame="s0/b.right/m/")["accepted"] is True
    await until(lambda: sum(1 for f in ctx.frames if "approve" in f.accepts()) == 1, what="right to finish")
    assert manager.send_event(run_id, "approve")["frame"] == "s0/b.left/m/"
    row = await settle(manager, run_id)

    assert row["output"] == {"left": "left approved", "right": "right approved"}, row


async def test_an_event_nobody_accepts_yet_is_deferred(harness):
    gate = asyncio.Event()
    backend = FakeBackend({"a": held(gate, "drafted")})
    manager = harness.manager()
    run_id = await manager.start(runnable(files("""\
        events: {go: {}}
        initial: a
        states:
          a:
            do: {agent: w, task: draft}
            transitions: [{target: w}]
          w:
            transitions:
              - trigger: go
                target: done
          done: {type: final, output: went}
        """)), backend=backend)
    await until(lambda: backend.count("a") == 1, what="the running activity")

    early = manager.send_event(run_id, "go", {"n": 1})
    gate.set()
    row = await settle(manager, run_id)

    assert early == {"accepted": True, "frame": None, "queued": True}
    assert row["status"] == "succeeded", row
    consumed = [r for r in harness.store.rows(run_id, kinds=("event",))]
    assert [(r["key"], r["status"]) for r in consumed] == [("s1:event", "consumed")]


async def test_an_event_the_frame_does_not_accept_stays_queued(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable(files("""\
        events: {first: {}, second: {}}
        context: {order: []}
        initial: w1
        states:
          w1:
            transitions:
              - trigger: first
                target: w2
                effect: ctx.order.append("first")
          w2:
            transitions:
              - trigger: second
                target: done
                effect: ctx.order.append("second")
          done: {type: final, output: "{{ ctx.order }}"}
        """)))
    await until(lambda: _status(manager, run_id) == "waiting", what="w1")

    manager.send_event(run_id, "second")
    await asyncio.sleep(0.05)
    assert manager.live[run_id].root.leaf.name == "w1", "w1 must not take an event it has no transition for"
    manager.send_event(run_id, "first")
    row = await settle(manager, run_id)

    assert row["output"] == ["first", "second"], row


async def test_wait_timeout_raises_in_the_waiting_state(harness):
    started = time.monotonic()
    row = await harness.run(files("""\
        events: {go: {}}
        context: {err: null}
        initial: w
        states:
          w:
            timeout: 200ms
            transitions:
              - trigger: go
                target: done
              - trigger: error
                target: expired
                effect: ctx.err = [error.type, error.state]
          done: {type: final}
          expired: {type: final, output: "{{ ctx.err }}"}
        """), timeout=3)
    assert row["final_state"] == "expired", row
    assert row["output"] == ["wait_timeout", "w"]
    assert time.monotonic() - started >= 0.2


TIMED = """\
    events: {go: {}}
    context: {err: null, rounds: 0}
    initial: w
    states:
      w:
        timeout: 1h
        transitions:
          - trigger: go
            target: pause
          - trigger: error
            target: expired
            effect: ctx.err = [error.type, error.state]
      pause:
        after: 1h
        transitions:
          - target: done
      done: {type: final, output: "{{ ctx.err }}"}
      expired: {type: final, output: "{{ ctx.err }}"}
    """


async def test_a_mock_times_a_wait_out_at_once_with_the_timeout_path_a_real_one_takes(harness):
    started = time.monotonic()

    row = await harness.run(files(TIMED), mocks={"w": {"$timeout": True}}, mock_only=True, timeout=3)

    assert (row["final_state"], row["output"]) == ("expired", ["wait_timeout", "w"]), row
    assert time.monotonic() - started < 2, "the mock waited the hour out"
    assert not (row.get("view") or {}).get("mocks_unused"), "the wait's mock counts as used"
    timers = harness.store.rows(row["id"], kinds=("timer",))
    assert [(r["state"], r["status"]) for r in timers] == [("w", "fired")], "journaled as a real time up: replay takes it"


VISITED = """\
    events: {go: {}, note: {}}
    context: {err: null, notes: 0}
    initial: w
    states:
      w:
        timeout: 1h
        transitions:
          - trigger: note
            effect: ctx.notes += 1
          - trigger: go
            target: again
          - trigger: error
            target: expired
            effect: ctx.err = [error.type, ctx.notes]
      again:
        transitions: [{target: w}]
      expired: {type: final, output: "{{ ctx.err }}"}
    """


async def test_a_time_up_mocks_visit_is_one_entry_of_the_state_and_a_resume_counts_on(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable(files(VISITED)), mock_only=True,
                                 mocks={"w": {"$visits": ["waits", {"$timeout": True}]}})
    await until(lambda: _status(manager, run_id) == "waiting", what="the first entry waits")
    manager.send_event(run_id, "note")  # an internal transition: the same entry, not the second visit
    await asyncio.sleep(0.05)
    assert _status(manager, run_id) == "waiting", "an internal transition took the next visit's mock"
    await manager.shutdown()  # the process stops: a resume in another one goes on from the journal
    other = harness.manager()
    await other.resume(run_id)
    await until(lambda: _status(other, run_id) == "waiting", what="the first entry waits again, as it did")

    other.send_event(run_id, "go")  # the second entry: its visit's mock is the time up
    row = await settle(other, run_id, 3)

    assert row["output"] == ["wait_timeout", 1], row


async def test_a_time_up_mock_on_a_wait_without_timeout_is_reported_unused(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable(files(WAITER)), mock_only=True, mocks={"w": {"$timeout": True}})

    await until(lambda: _status(manager, run_id) == "waiting", what="it waits: nothing to time out")

    assert manager.live[run_id].ctx.view()["mocks_unused"] == ["w"], "a mock that does nothing must say so"


async def test_a_timer_states_mock_goes_on_at_once_and_visits_pick_the_wait_that_times_out(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable(files(TIMED)), mock_only=True,
                                 mocks={"w": {"$visits": ["waits for its event"]}, "pause": {"$timeout": True}})
    await until(lambda: _status(manager, run_id) == "waiting", what="w waits: its visit's mock is no time up")
    manager.send_event(run_id, "go")

    row = await settle(manager, run_id, 3)

    assert (row["final_state"], row["output"]) == ("done", None), row


async def test_run_stays_waiting_while_another_frame_still_waits(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable(files("""\
        imports: {sub: ./sub.yaml}
        context: {out: null}
        initial: work
        states:
          work:
            do:
              parallel:
                left: {machine: sub, params: {label: left}}
                right: {machine: sub, params: {label: right}}
            transitions:
              - target: done
                effect: ctx.out = out
          done: {type: final, output: "{{ ctx.out }}"}
        """, **{"sub.yaml": SUB_WAITER})))
    ctx = manager.live[run_id].ctx
    await until(lambda: sum(1 for f in ctx.frames if "approve" in f.accepts()) == 2, what="both waiting frames")

    manager.send_event(run_id, "approve", frame="s0/b.right/m/")
    await until(lambda: sum(1 for f in ctx.frames if "approve" in f.accepts()) == 1, what="right to finish")
    await asyncio.sleep(0.05)

    assert ctx.status == "waiting", "only the left frame is left, and it waits for an event"


# ------------------------------------------------------------------ parallel, map, submachines (§2.5)

async def test_parallel_fail_fast_cancels_the_other_branches(harness):
    backend = FakeBackend({"fan/bad": ActivityError("agent_failed", "down"), "fan/slow": held(asyncio.Event())})
    row = await harness.run(files("""\
        context: {err: null}
        initial: fan
        states:
          fan:
            do:
              parallel:
                slow: {agent: w, task: s}
                bad: {agent: w, task: b}
            transitions:
              - target: done
              - trigger: error
                target: failed
                effect: ctx.err = [error.type, error.branch]
          done: {type: final}
          failed: {type: final, output: "{{ ctx.err }}"}
        """), backend=backend, timeout=3)
    assert row["output"] == ["agent_failed", "bad"], row
    assert backend.cancelled == ["fan/slow"]


async def test_parallel_fail_collect_runs_every_branch(harness):
    backend = FakeBackend({"fan/bad": ActivityError("agent_failed", "down"), "fan/good": "fine"})
    row = await harness.run(files("""\
        context: {out: null}
        initial: fan
        states:
          fan:
            do:
              parallel:
                bad: {agent: w, task: b}
                good: {agent: w, task: g}
              fail: collect
            transitions:
              - target: done
                effect: ctx.out = out
          done: {type: final, output: "{{ ctx.out }}"}
        """), backend=backend)
    out = row["output"]
    assert out["good"] == {"status": "succeeded", "out": "fine"}, row
    assert out["bad"]["status"] == "failed"
    assert out["bad"]["error"]["type"] == "agent_failed"


MAP_MACHINE = """\
context: {{out: null}}
initial: each
states:
  each:
    do:
      map: "[3, 1, 2]"
      as: n
      concurrency: {concurrency}
      each: {{agent: w, task: "item {{{{ index }}}}: {{{{ n }}}}"}}
    transitions:
      - target: done
        effect: ctx.out = out
  done: {{type: final, output: "{{{{ ctx.out }}}}"}}
"""


def _map_backend(log: list[str]) -> FakeBackend:
    async def answer(call):
        index = call["path"].rsplit("/", 1)[1]
        log.append(f"start {index}")
        await asyncio.sleep(0.06 if index == "0" else 0.01)  # the first item is the slowest
        log.append(f"end {index}")
        return call["task"]

    return FakeBackend({f"each/{i}": answer for i in range(3)})


async def test_map_with_concurrency_one_runs_strictly_in_order(harness):
    log: list[str] = []
    row = await harness.run(files(MAP_MACHINE.format(concurrency=1)), backend=_map_backend(log))
    assert log == ["start 0", "end 0", "start 1", "end 1", "start 2", "end 2"]
    assert row["output"] == ["item 0: 3", "item 1: 1", "item 2: 2"], row


async def test_map_results_keep_item_order_whatever_finishes_first(harness):
    log: list[str] = []
    row = await harness.run(files(MAP_MACHINE.format(concurrency=3)), backend=_map_backend(log))
    assert log.index("end 0") > log.index("end 2"), "fixture: item 0 must finish last to prove anything"
    assert row["output"] == ["item 0: 3", "item 1: 1", "item 2: 2"], row


SUB_PARAMS = """\
stategraph: 1
id: sub
python: sub.py
params:
  text: {type: string, required: true}
  times: {type: integer, default: 2}
context: {said: null}
initial: say
states:
  say:
    do: {call: repeat, args: {text: "{{ params.text }}", times: "{{ params.times }}"}}
    transitions:
      - target: done
        effect: ctx.said = out
        guard: out != "fail!fail!"
      - target: gave_up
        guard: else
  done: {type: final, output: {said: "{{ ctx.said }}", times: "{{ params.times }}"}}
  gave_up: {type: final, status: failed, output: {reason: "{{ params.text }} was refused"}}
"""
SUB_PY = "def repeat(text, times):\n    if text == 'crash':\n        raise RuntimeError('crashed')\n    return text * times\n"


async def test_submachine_binds_params_and_returns_its_final_output(harness):
    row = await harness.run(files("""\
        imports: {sub: ./sub.yaml}
        context: {got: null, word: "ab"}
        initial: a
        states:
          a:
            do: {machine: sub, params: {text: "{{ ctx.word }}"}}
            transitions:
              - target: done
                effect: ctx.got = out
          done: {type: final, output: "{{ ctx.got }}"}
        """, **{"sub.yaml": SUB_PARAMS, "sub.py": SUB_PY}))
    assert row["output"] == {"said": "abab", "times": 2}, row


@pytest.mark.parametrize("text,cause,data", [
    ("fail!", "final", {"reason": "fail! was refused"}),
    ("crash", "call_failed", None),
], ids=["failed_final", "unhandled_error"])
async def test_submachine_failure_carries_the_cause(harness, text, cause, data):
    row = await harness.run(files(f"""\
        imports: {{sub: ./sub.yaml}}
        context: {{err: null}}
        initial: a
        states:
          a:
            do: {{machine: sub, params: {{text: "{text}"}}}}
            transitions:
              - target: done
              - trigger: error
                target: failed
                effect: |
                  ctx.err = {{"type": error.type, "cause": error.cause["type"], "data": error.data}}
          done: {{type: final}}
          failed: {{type: final, output: "{{{{ ctx.err }}}}"}}
        """, **{"sub.yaml": SUB_PARAMS, "sub.py": SUB_PY}))
    assert row["output"] == {"type": "submachine_failed", "cause": cause, "data": data}, row


@pytest.mark.parametrize("mocked", [False, True], ids=["live", "mocked"])
async def test_activity_meta_names_are_bound(harness, mocked):
    """§2.6: activity.attempts/mocked are readable live and mocked alike (a mocked test run must not pass where
    the live run fails); `replayed` stays journal meta -- it differs between a run and its replay, so code that
    could write it into ctx would make every resume diverge."""
    options = {"mocks": {"a": "M"}} if mocked else {"backend": FakeBackend({"a": "L"})}
    row = await harness.run(files("""\
        context: {meta: null}
        initial: a
        states:
          a:
            do: {agent: w, task: go}
            transitions:
              - target: done
                effect: ctx.meta = [activity.attempts, activity.mocked, "replayed" in activity]
          done: {type: final, output: "{{ ctx.meta }}"}
        """), **options)
    assert row["status"] == "succeeded", row["error"]
    assert row["output"][1:] == [mocked, False]


# ------------------------------------------------------------------ retries and timeouts (§2.5)

async def test_retry_waits_the_backoff_between_attempts(harness):
    stamps: list[float] = []

    def flaky(call):
        stamps.append(time.monotonic())
        return ActivityError("agent_failed", "flaky") if len(stamps) < 3 else "ok"

    backend = FakeBackend({"a": flaky})
    row = await harness.run(files("""\
        context: {got: null}
        initial: a
        states:
          a:
            do:
              agent: w
              task: go
              retry: {attempts: 3, backoff: {initial: 0.05, factor: 2, max: 0.08}}
            transitions:
              - target: done
                effect: ctx.got = [out, activity.attempts]
          done: {type: final, output: "{{ ctx.got }}"}
        """), backend=backend)
    assert row["output"] == ["ok", 3], row
    gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
    assert gaps[0] >= 0.045 and gaps[1] >= 0.075, gaps


async def test_retry_on_limits_the_retried_types(harness):
    backend = FakeBackend({"a": sequence(ActivityError("agent_failed", "no"), "ok")})
    row = await harness.run(files("""\
        initial: a
        states:
          a:
            do:
              agent: w
              task: go
              retry: {attempts: 3, errors: [tool_failed]}
            transitions:
              - target: done
          done: {type: final}
        """), backend=backend)
    assert row["status"] == "failed"
    assert row["error"]["type"] == "agent_failed"
    assert backend.count("a") == 1


PATIENT = '''
def patient(sg, path):
    """Works 30 s in its worker thread -- unless nobody waits for its answer any more."""
    import pathlib
    stopped = sg.cancelled.wait(30)
    pathlib.Path(path).write_text("stopped" if stopped else "ran out", encoding="utf-8")
'''


async def test_a_sync_call_learns_that_its_activity_timed_out_and_returns_early(harness, tmp_path):
    seen = tmp_path / "seen.txt"
    row = await harness.run(files(f"""\
        context: {{err: null}}
        initial: a
        states:
          a:
            do: {{call: patient, args: {{path: "{seen.as_posix()}"}}, timeout: 100ms}}
            transitions:
              - target: done
              - trigger: error
                target: failed
                effect: ctx.err = error.type
          done: {{type: final}}
          failed: {{type: final, output: "{{{{ ctx.err }}}}"}}
        """, **{"m.py": HELPERS + PATIENT}), timeout=5)

    assert row["output"] == "timeout", row
    await until(seen.exists, timeout=5, what="the thread to return: it was told, it did not wait out its 30 s")
    assert seen.read_text(encoding="utf-8") == "stopped"


@pytest.mark.parametrize("on,calls,error", [("", 1, "timeout"), (", errors: [timeout]", 3, "timeout")],
                         ids=["default", "on_timeout"])
async def test_timeout_is_not_retried_by_default(harness, on, calls, error):
    backend = FakeBackend({"a": held(asyncio.Event())})
    row = await harness.run(files(f"""\
        initial: a
        states:
          a:
            do:
              agent: w
              task: go
              timeout: 50ms
              retry: {{attempts: 3{on}}}
            transitions:
              - target: done
          done: {{type: final}}
        """), backend=backend)
    assert row["error"]["type"] == error, row
    assert backend.count("a") == calls


# ------------------------------------------------------------------ limits (§3.5, §3.7, §3.8)

async def test_step_limit_ends_the_run_uncatchably(harness):
    row = await harness.run(files("""\
        limits: {max_steps: 5}
        initial: a
        states:
          a:
            max_visits: 100
            do: {call: one}
            transitions:
              - target: a
              - trigger: error
                target: caught
          caught: {type: final}
        """))
    assert row["status"] == "failed", row
    assert row["error"]["type"] == "step_limit"
    assert row["final_state"] != "caught"


async def test_step_limit_of_a_submachine_ends_the_run(harness):
    """§3.5: not catchable -- the caller's error transition must not turn it into submachine_failed."""
    looping = """\
        stategraph: 1
        id: loop
        python: loop.py
        limits: {max_steps: 3}
        initial: a
        states:
          a:
            max_visits: 100
            do: {call: one}
            transitions: [{target: a}]
          done: {type: final}
        """
    row = await harness.run(files("""\
        imports: {loop: ./loop.yaml}
        initial: a
        states:
          a:
            do: {machine: loop}
            transitions:
              - target: done
              - trigger: error
                target: caught
          done: {type: final}
          caught: {type: final}
        """, **{"loop.yaml": looping, "loop.py": HELPERS}))
    assert row["status"] == "failed" and row["error"]["type"] == "step_limit", row
    assert row["final_state"] != "caught"


async def test_run_timeout_counts_running_time_only(harness):
    """1.2 s in a wait state against a 0.6 s run timeout: waiting does not count (§3.8)."""
    manager = harness.manager()
    run_id = await manager.start(runnable(files("""\
        limits: {timeout: 0.6}
        events: {go: {}}
        initial: w
        states:
          w:
            transitions:
              - trigger: go
                target: done
          done: {type: final}
        """)))
    await until(lambda: _status(manager, run_id) == "waiting", what="the wait state")
    await asyncio.sleep(1.2)
    manager.send_event(run_id, "go")
    row = await settle(manager, run_id)
    assert row["status"] == "succeeded", row["error"]


async def test_run_timeout_ends_a_run_that_keeps_running(harness):
    backend = FakeBackend({"a": held(asyncio.Event())})
    row = await harness.run(files("""\
        limits: {timeout: 0.6}
        initial: a
        states:
          a:
            do: {agent: w, task: go}
            transitions:
              - target: done
              - trigger: error
                target: done
          done: {type: final}
        """), backend=backend, timeout=4)
    assert row["status"] == "failed"
    assert row["error"]["type"] == "timed_out"
