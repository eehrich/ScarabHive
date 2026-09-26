"""The debugger (docs/stategraph_design.md §6, §5.3): breakpoints, stepping, watchpoints, evaluate, set.

Driven through the RunManager API the service and the panel use
(``control``, ``evaluate``, ``assign``, ``set_points``); pauses are observed on
the live run's debugger state, and replay behaviour through a real
shutdown/resume on the same RunStore.

Mutation checks run (each turned the named tests red, then was restored from a copy):
- debugger.at_hook: the ``point_.at != point`` filter removed                  -> test_breakpoint_on_enter_pauses_*
- debugger.at_hook: the breakpoint condition ignored                          -> test_breakpoint_on_exit_sees_out_*
- interpreter._dispatch: the exit/error hook not called                       -> test_breakpoint_on_error_*
- debugger.command: ``step`` behaves like ``continue``                         -> test_step_walks_hook_by_hook
- debugger.at_hook: run_to never matches                                      -> test_run_to_*
- debugger.after_step: the watchpoint condition ignored                       -> test_watchpoint_pauses_on_a_change_that_meets_its_condition
- code.Namespace.evaluate: the read-only hash check removed                     -> test_evaluate_refuses_a_mutating_expression
- runner.RunContext.hook: journaled edits not re-applied                        -> test_set_journals_an_edit_that_replay_reapplies
- runner.RunContext.is_replay_point: always False                              -> test_breakpoints_are_silent_during_replay
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.model.code import CodeError
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, held, runnable, settle,
                                                         until)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

LOOP = """\
stategraph: 1
id: m
context: {n: 0, x: 0, items: [1, 2]}
initial: a
states:
  a:
    max_visits: 10
    do: {agent: w, task: "round {{ ctx.n }}"}
    transitions:
      - target: a
        guard: ctx.n < 3
        effect: ctx.n += 1
      - target: b
        guard: else
  b:
    do: {agent: w, task: "b {{ ctx.x }}"}
    transitions: [{target: done}]
  done: {type: final, output: {n: "{{ ctx.n }}", x: "{{ ctx.x }}"}}
"""


def echo(call):
    return call["task"]


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


async def next_pause(manager, run_id: str, previous=None, timeout: float = 5.0) -> dict:
    """The next pause of a live run (a new pause record, not the one being left)."""
    debugger = manager.live[run_id].ctx.debugger
    await until(lambda: run_id not in manager.live
                or (debugger.paused is not None and debugger.paused is not previous), timeout, "a pause")
    assert run_id in manager.live, f"the run ended instead of pausing: {manager.store.get_run(run_id)['status']}"
    return debugger.paused


def pauses(harness: Harness, run_id: str) -> list[tuple[str, str]]:
    return [(row["state"], row["data"]["hook"]) for row in harness.store.rows(run_id, kinds=("trace",))
            if row["status"] == "paused"]


async def start(harness: Harness, text: str, backend: FakeBackend, **options):
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": text}), backend=backend, **options)
    return manager, run_id


# ------------------------------------------------------------------ breakpoints

async def test_breakpoint_on_enter_pauses_before_the_activity_starts(harness):
    backend = FakeBackend({"a": echo, "b": "B"})
    manager, run_id = await start(harness, LOOP, backend, breakpoints=[{"state": "b", "at": "enter"}])

    paused = await next_pause(manager, run_id)
    assert (paused["state"], paused["hook"]) == ("b", "enter")
    assert backend.count("b") == 0, "enter: the activity must not have started"
    assert harness.store.get_run(run_id)["status"] == "paused"

    manager.control(run_id, "continue")
    row = await settle(manager, run_id)
    assert row["status"] == "succeeded" and backend.count("b") == 1
    assert pauses(harness, run_id) == [("b", "enter")]


async def test_breakpoint_on_exit_sees_out_before_a_transition_is_chosen(harness):
    backend = FakeBackend({"a": echo, "b": "B"})
    manager, run_id = await start(harness, LOOP, backend,
                                  breakpoints=[{"state": "a", "at": "exit", "condition": "out == 'round 1'"}])

    paused = await next_pause(manager, run_id)
    assert (paused["state"], paused["hook"], paused["out"]) == ("a", "exit", "round 1")
    frame = manager.live[run_id].root
    assert frame.ctx["n"] == 1, "exit: the completion's effect must not have run yet"
    assert frame.leaf.name == "a"

    manager.control(run_id, "continue")
    await settle(manager, run_id)
    assert pauses(harness, run_id) == [("a", "exit")], "the condition must hold only for round 1"


ERRORING = """\
stategraph: 1
id: m
initial: a
states:
  a:
    do: {agent: w, task: go}
    transitions:
      - target: done
      - trigger: error
        target: failed
  done: {type: final}
  failed: {type: final, status: failed}
"""


@pytest.mark.parametrize("condition,pauses_expected", [
    ("error.type == 'agent_failed'", [("a", "error")]),
    ("error.type == 'timeout'", []),
])
async def test_breakpoint_on_error_with_a_condition(harness, condition, pauses_expected):
    backend = FakeBackend({"a": ActivityError("agent_failed", "down")})
    manager, run_id = await start(harness, ERRORING, backend,
                                  breakpoints=[{"state": "a", "at": "error", "condition": condition}])
    if pauses_expected:
        paused = await next_pause(manager, run_id)
        assert paused["error"]["type"] == "agent_failed"
        assert manager.live[run_id].root.leaf.name == "a", "error: no transition chosen yet"
        manager.control(run_id, "continue")
    row = await settle(manager, run_id)
    assert row["final_state"] == "failed"
    assert pauses(harness, run_id) == pauses_expected


async def test_breakpoint_on_enter_fires_again_after_the_composite_is_reentered(harness):
    text = """\
stategraph: 1
id: m
context: {rounds: 0}
initial: c
states:
  c:
    max_visits: 3
    initial: x
    states:
      x:
        do: {agent: w, task: go}
        transitions: [{target: fin}]
      fin: {type: final}
    transitions:
      - target: c
        guard: ctx.rounds < 1
        effect: ctx.rounds += 1
      - target: done
        guard: else
  done: {type: final}
"""
    backend = FakeBackend({"x": "X"})
    manager, run_id = await start(harness, text, backend, breakpoints=["x"])
    first = await next_pause(manager, run_id)
    manager.control(run_id, "continue")
    try:
        second = await next_pause(manager, run_id, first, timeout=2)
        assert (second["state"], second["hook"]) == ("x", "enter")
    finally:
        if run_id in manager.live:
            manager.control(run_id, "continue")
        await settle(manager, run_id)
    assert backend.count("x") == 2


# ------------------------------------------------------------------ stepping

CHAIN = """\
stategraph: 1
id: m
initial: a
states:
  a:
    do: {agent: w, task: one}
    transitions: [{target: b}]
  b:
    do: {agent: w, task: two}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_step_walks_hook_by_hook(harness):
    backend = FakeBackend({"a": "A", "b": "B"})
    manager, run_id = await start(harness, CHAIN, backend, pause_at_start=True)

    seen = []
    paused = await next_pause(manager, run_id)
    seen.append((paused["state"], paused["hook"]))
    for _ in range(3):
        manager.control(run_id, "step")
        paused = await next_pause(manager, run_id, paused)
        seen.append((paused["state"], paused["hook"]))
    manager.control(run_id, "continue")
    row = await settle(manager, run_id)

    assert seen == [("a", "enter"), ("a", "exit"), ("b", "enter"), ("b", "exit")]
    assert row["status"] == "succeeded"


async def test_run_to_stops_at_the_state_and_nowhere_before(harness):
    backend = FakeBackend({"a": "A", "b": "B"})
    manager, run_id = await start(harness, CHAIN, backend, pause_at_start=True)
    first = await next_pause(manager, run_id)

    manager.control(run_id, "run_to", state="b")
    paused = await next_pause(manager, run_id, first)
    assert (paused["state"], paused["hook"]) == ("b", "enter")
    assert "reached b" in paused["reason"]
    assert backend.count("a") == 1 and backend.count("b") == 0

    manager.control(run_id, "continue")
    await settle(manager, run_id)
    assert pauses(harness, run_id) == [("a", "enter"), ("b", "enter")]


async def test_pause_command_takes_effect_at_the_next_hook(harness):
    gate = asyncio.Event()
    backend = FakeBackend({"a": held(gate, "A"), "b": "B"})
    manager, run_id = await start(harness, CHAIN, backend)
    await until(lambda: backend.count("a") == 1, what="a running")

    manager.control(run_id, "pause")
    assert manager.live[run_id].ctx.debugger.paused is None, "a running activity is never frozen"
    gate.set()
    paused = await next_pause(manager, run_id)
    assert (paused["state"], paused["hook"]) == ("a", "exit")
    manager.control(run_id, "continue")
    await settle(manager, run_id)


# ------------------------------------------------------------------ watchpoints

async def test_watchpoint_pauses_on_a_change_that_meets_its_condition(harness):
    backend = FakeBackend({"a": echo, "b": "B"})
    manager, run_id = await start(harness, LOOP, backend,
                                  watchpoints=[{"expr": "ctx.n", "condition": "new == 3 and old == 2"}])

    paused = await next_pause(manager, run_id)
    assert paused["hook"] == "watch"
    assert "2 -> 3" in paused["reason"], "the change 1 -> 2 does not meet the condition"
    assert manager.live[run_id].root.ctx["n"] == 3

    manager.control(run_id, "continue")
    row = await settle(manager, run_id)
    assert row["status"] == "succeeded"
    assert [hook for _, hook in pauses(harness, run_id)] == ["watch"], "only the change 2 -> 3 meets the condition"


# ------------------------------------------------------------------ evaluate and set

async def _paused_at_b(harness, backend):
    manager, run_id = await start(harness, LOOP, backend, breakpoints=["b"])
    await next_pause(manager, run_id)
    return manager, run_id


async def test_evaluate_reads_the_paused_scope(harness):
    manager, run_id = await _paused_at_b(harness, FakeBackend({"a": echo, "b": "B"}))
    assert manager.evaluate(run_id, "ctx.n + 10") == 13
    assert manager.evaluate(run_id, "run.state") == "b"
    manager.control(run_id, "continue")
    await settle(manager, run_id)


async def test_evaluate_refuses_a_mutating_expression(harness):
    manager, run_id = await _paused_at_b(harness, FakeBackend({"a": echo, "b": "B"}))
    with pytest.raises(CodeError, match="ctx mutated"):
        manager.evaluate(run_id, "ctx.items.append(3)")
    manager.control(run_id, "continue")
    await settle(manager, run_id)


async def test_a_refused_evaluate_leaves_the_context_unchanged(harness):
    manager, run_id = await _paused_at_b(harness, FakeBackend({"a": echo, "b": "B"}))
    with pytest.raises(CodeError):
        manager.evaluate(run_id, "ctx.items.append(3)")
    try:
        assert manager.live[run_id].root.ctx["items"] == [1, 2]
    finally:
        manager.control(run_id, "continue")
        await settle(manager, run_id)


async def test_set_journals_an_edit_that_replay_reapplies(harness):
    """set at b's enter changes b's rendered input; the resumed run must re-apply it before b renders again."""
    gate = asyncio.Event()
    manager, run_id = await _paused_at_b(harness, FakeBackend({"a": echo, "b": held(gate, "B")}))

    assert manager.assign(run_id, "ctx.x", "40 + 2") == 42
    [edit] = harness.store.rows(run_id, kinds=("edit",))
    assert (edit["key"].split(":")[:2], edit["data"]["path"], edit["data"]["value"]) == (["s4", "enter"], "ctx.x", 42)
    manager.control(run_id, "continue")
    await until(lambda: len([c for c in manager.live[run_id].ctx.backend.calls if c["path"] == "b"]) == 1,
                what="b in flight")
    await manager.shutdown()

    second = FakeBackend({"b": echo})
    resumed = harness.manager()
    await resumed.resume(run_id, backend=second)
    # b's enter is the last recorded step (its edit), so its breakpoint is live again after the replay (§5.3)
    paused = await next_pause(resumed, run_id)
    assert (paused["state"], paused["hook"]) == ("b", "enter")
    assert resumed.live[run_id].root.ctx["x"] == 42, "the journaled edit was not re-applied at its hook"
    resumed.control(run_id, "continue")
    row = await settle(resumed, run_id)

    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == {"n": 3, "x": 42}
    assert second.calls[0]["task"] == "b 42"


async def test_set_at_a_watchpoint_pause_is_replayed(harness):
    gate = asyncio.Event()
    backend = FakeBackend({"a": echo, "b": held(gate, "B")})
    manager, run_id = await start(harness, LOOP, backend, watchpoints=[{"expr": "ctx.n", "condition": "new == 2"}])
    paused = await next_pause(manager, run_id)
    assert paused["hook"] == "watch"
    manager.assign(run_id, "ctx.x", "7")
    manager.control(run_id, "continue")
    await until(lambda: backend.count("b") == 1, what="b in flight")
    await manager.shutdown()

    resumed = harness.manager()
    await resumed.resume(run_id, backend=FakeBackend({"b": echo}))
    row = await settle(resumed, run_id)
    assert row["status"] == "succeeded", row["error"]
    assert row["output"]["x"] == 7


async def test_breakpoints_are_silent_during_replay(harness):
    gate = asyncio.Event()
    backend = FakeBackend({"a": "A", "b": held(gate, "B")})
    manager, run_id = await start(harness, CHAIN, backend)
    await until(lambda: backend.count("b") == 1, what="b in flight")
    await manager.shutdown()
    manager.set_points(run_id, breakpoints=["a", "a@exit", "b"])

    resumed = harness.manager()
    await resumed.resume(run_id, backend=FakeBackend({"b": "B2"}))
    paused = await next_pause(resumed, run_id)
    assert (paused["state"], paused["hook"]) == ("b", "enter"), "a replayed state paused the run"
    resumed.control(run_id, "continue")
    row = await settle(resumed, run_id)
    assert row["status"] == "succeeded"
