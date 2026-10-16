"""Regressions for the engine findings of the review of 2026-10-15 (docs/review_2026-10-15.md).

One test (or one parametrised group) per fixed finding, named by its id. Every machine runs through the real
RunManager on a RunStore under tmp_path; only the boundary behind the engine is FakeBackend. A "crash" is a real
``RunManager.shutdown()`` and a resume is a NEW manager on the same store.
"""

from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

from plugins.stategraph.engine.runner import ended_without_outcome, failed_transiently
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, activity_rows, held,
                                                         resume, runnable, sequence, settle, until)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


async def run(harness: Harness, files: dict[str, str], backend: FakeBackend = None, **start) -> dict:
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend, **start)
    return await settle(manager, run_id)


def changed_snapshot(harness: Harness, run_id: str, old: str, new: str) -> None:
    """Edit the run's stored definition (what a resume loads)."""
    snapshot = harness.store.get_run(run_id)["definition"]
    text = snapshot["files"][snapshot["root"]]
    assert text.count(old) == 1, f"fixture: {old!r} not in the stored definition"
    snapshot["files"][snapshot["root"]] = text.replace(old, new)
    harness.store.update_run(run_id, definition=snapshot)


def traces(harness: Harness, run_id: str, status: str) -> list[dict]:
    return [row for row in harness.store.rows(run_id, kinds=("trace",)) if row["status"] == status]


# ------------------------------------------------------------------ PROD-1: replay follows the recorded guards

GATE = """\
stategraph: 1
id: m
python: m.py
events: {comment: {}}
initial: gate
states:
  gate:
    transitions:
      - trigger: comment
        guard: answer_of(event.data) == "go"
        target: push
      - trigger: comment
  push: {type: final, output: pushed}
"""

GATE_PY = """\
from sg_r15_helper import answer_of
"""


@pytest.fixture
def helper():
    """A module the companion imports -- live code a restart may have changed (tracker_steps.answer_of)."""
    module = types.ModuleType("sg_r15_helper")
    module.answer_of = lambda data: "?"
    sys.modules["sg_r15_helper"] = module
    yield module
    sys.modules.pop("sg_r15_helper", None)


async def gate_run_interrupted_after_a_comment(harness: Harness) -> str:
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": GATE, "m.py": GATE_PY}))
    await until(lambda: harness.store.get_run(run_id)["status"] == "waiting", what="the gate waits")
    assert manager.send_event(run_id, "comment", {"body": "{{/tracker go}}"})["accepted"]
    await until(lambda: harness.store.has_row(run_id, "trace", "s1:step"), what="the comment taken")
    await manager.shutdown()
    assert harness.store.get_run(run_id)["status"] == "interrupted"
    return run_id


async def test_prod1_a_changed_helper_does_not_rewrite_a_journaled_guard(harness, helper):
    run_id = await gate_run_interrupted_after_a_comment(harness)
    helper.answer_of = lambda data: "go"  # the fix a restart brought: the recorded comment would hold now

    manager = harness.manager()
    await manager.resume(run_id)
    row = await manager.wait(run_id, timeout=3)

    assert row["status"] == "waiting", row["error"]
    assert row["view"]["frames"][0]["state"] == "gate"


async def test_prod1_a_changed_guard_text_is_a_divergence_that_names_the_guard(harness, helper):
    run_id = await gate_run_interrupted_after_a_comment(harness)
    changed_snapshot(harness, run_id, 'answer_of(event.data) == "go"', 'answer_of(event.data) == "yes"')

    manager = harness.manager()
    await manager.resume(run_id)
    row = await settle(manager, run_id)

    assert row["status"] == "failed" and row["error"]["type"] == "diverged", row
    assert """'answer_of(event.data) == "go"'""" in row["error"]["message"], row["error"]


def test_prod1_a_legacy_choice_row_drops_its_repeated_guard():
    from plugins.stategraph.engine.runner import _deduplicated

    legacy = [{"at": "r.transitions", "guard": "ctx.x > 0", "result": True},
              {"at": "r.transitions", "guard": "ctx.x > 0", "result": True},
              {"at": "j.transitions", "guard": "ctx.y > 0", "result": True}]
    assert [g["at"] for g in _deduplicated(legacy)] == ["r.transitions", "j.transitions"]
    both_false = [{"at": "r.transitions", "guard": "g", "result": False}] * 2
    assert len(_deduplicated(both_false)) == 2


RAISING_GATE = """\
stategraph: 1
id: m
python: m.py
events: {comment: {}}
initial: gate
states:
  gate:
    transitions:
      - trigger: comment
        guard: answer_of(event.data) == "go"
        target: push
      - trigger: error
        target: gate2
  gate2:
    transitions:
      - trigger: comment
        target: push
  push: {type: final, output: pushed}
"""


async def test_prod1_a_guard_that_raised_raises_again_on_replay_with_a_fixed_helper(harness, helper):
    def broken(data):
        raise KeyError("body")
    helper.answer_of = broken
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": RAISING_GATE, "m.py": GATE_PY}))
    await until(lambda: harness.store.get_run(run_id)["status"] == "waiting", what="the gate waits")
    assert manager.send_event(run_id, "comment", {"body": "x"})["accepted"]
    await until(lambda: harness.store.has_row(run_id, "trace", "s2:step"), what="the guard_failed handled")
    await until(lambda: harness.store.get_run(run_id)["status"] == "waiting", what="gate2 waits")
    await manager.shutdown()
    helper.answer_of = lambda data: "go"  # fixed by the restart: live, the recorded comment would hold now

    manager = harness.manager()
    await manager.resume(run_id)
    row = await manager.wait(run_id, timeout=3)

    assert row["status"] == "waiting", row["error"]
    assert row["view"]["frames"][0]["state"] == "gate2"


FORKED_GATE = """\
stategraph: 1
id: m
python: m.py
events: {comment: {}, more: {}}
initial: gate
states:
  gate:
    transitions:
      - trigger: comment
        guard: answer_of(event.data) == "go"
        target: push
      - trigger: comment
        target: second
  second:
    transitions:
      - {trigger: more, target: third}
  third:
    transitions:
      - {trigger: more, target: push}
  push: {type: final, output: pushed}
"""


async def test_prod1_a_fork_s_prefix_answers_its_guards_from_the_source_journal(harness, helper):
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": FORKED_GATE, "m.py": GATE_PY}))
    for event, step in (("comment", "s1"), ("more", "s2")):
        await until(lambda: harness.store.get_run(run_id)["status"] == "waiting", what=f"waits before {event}")
        assert manager.send_event(run_id, event, {"body": "x"})["accepted"]
        await until(lambda: harness.store.has_row(run_id, "trace", f"{step}:step"), what=f"{event} taken")
    await until(lambda: harness.store.get_run(run_id)["status"] == "waiting", what="third waits")
    helper.answer_of = lambda data: "go"

    fork = await manager.fork(run_id, at_step=2)
    row = await manager.wait(fork, timeout=3)

    assert row["status"] == "waiting", row["error"]
    assert row["view"]["frames"][0]["state"] == "third"  # the fork point: the event at step 1 came along


INITIAL_CHOICE = """\
stategraph: 1
id: m
python: m.py
events: {go: {}}
initial: route
states:
  route:
    type: choice
    transitions:
      - guard: answer_of(None) == "go"
        target: first
      - guard: else
        target: other
  first:
    transitions: [{trigger: go, target: done}]
  other:
    transitions: [{trigger: go, target: done}]
  done: {type: final}
"""


async def test_prod1_an_initial_choice_replays_its_recorded_branch(harness, helper):
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": INITIAL_CHOICE, "m.py": GATE_PY}))
    await until(lambda: harness.store.get_run(run_id)["status"] == "waiting", what="other waits")
    await manager.shutdown()
    helper.answer_of = lambda data: "go"  # live, the choice would now pick first

    manager = harness.manager()
    await manager.resume(run_id)
    row = await manager.wait(run_id, timeout=3)

    assert row["status"] == "waiting", row["error"]
    assert row["view"]["frames"][0]["state"] == "other"


# ------------------------------------------------------------------ RES-1: interrupted is not transient

def test_res1_a_run_that_failed_interrupted_is_not_tried_anew():
    assert not failed_transiently({"type": "interrupted", "message": "was running"})
    assert not failed_transiently({"type": "submachine_failed", "cause": {"type": "interrupted"}})
    assert failed_transiently({"type": "timeout"})


# ------------------------------------------------------------------ RES-4: a cancel from outside is a halt

FINALLY = """\
stategraph: 1
id: m
initial: work
finally: {agent: c, task: clean}
states:
  work:
    do: {agent: w, task: go}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_res4_a_cancel_of_the_run_task_nobody_asked_for_interrupts_the_run(harness):
    backend = FakeBackend({"work": held(asyncio.Event()), "m.finally": "cleaned"})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": FINALLY}), backend=backend)
    await until(lambda: backend.count("work") == 1, what="work in flight")

    task = manager.live[run_id].task
    task.cancel()  # what asyncio.run's teardown does when the process stops without shutdown
    await asyncio.wait({task})

    row = harness.store.get_run(run_id)
    assert row["status"] == "interrupted", (row["status"], row["error"])
    assert backend.count("m.finally") == 0  # a halt: the resume, not the teardown, runs on
    after = await resume(harness, run_id, FakeBackend({"work": "W", "m.finally": "cleaned"}))
    assert after["status"] == "succeeded", after["error"]


TIMED_SUB = """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
context: {err: null}
initial: p
states:
  p:
    do: {machine: sub, timeout: 0.3}
    transitions:
      - target: done
      - trigger: error
        target: timed
        effect: ctx.err = error.type
  done: {type: final}
  timed: {type: final, output: "{{ ctx.err }}"}
"""

SUB_WITH_FINALLY = """\
stategraph: 1
id: sub
initial: w
finally: {agent: c, task: clean}
states:
  w:
    do: {agent: w, task: go}
    transitions: [{target: fin}]
  fin: {type: final}
"""


async def test_res4_an_activity_timeout_in_the_run_task_still_ends_its_submachine_with_its_finally(harness):
    backend = FakeBackend({"p/w": held(asyncio.Event()), "p/sub.finally": "cleaned"})
    row = await run(harness, {"m.yaml": TIMED_SUB, "sub.yaml": SUB_WITH_FINALLY}, backend)

    assert (row["status"], row["output"]) == ("succeeded", "timeout"), row
    assert backend.count("p/sub.finally") == 1


# ------------------------------------------------------------------ RES-5: a failed resume with live objects

FLAKY = """\
stategraph: 1
id: m
python: m.py
initial: a
states:
  a:
    do: {call: flaky}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.out }}"}
context: {out: null}
"""

FLAKY_PY = """\
def flaky(sg):
    if not sg.live.get("fixed"):
        raise sg.Error("call_failed", "not yet", resumable=True)
    return "ok"
"""


async def test_res5_a_resume_with_live_objects_that_failed_to_launch_can_be_resumed_again(harness):
    tree = runnable({"m.yaml": FLAKY, "m.py": FLAKY_PY})
    first = harness.manager()
    run_id = await first.start(tree, live={"fixed": False})
    row = await settle(first, run_id)
    assert row["status"] == "failed" and row["error"]["resumable"], row

    broken = harness.manager()

    def locked(*_, **__):
        raise RuntimeError("database is locked")

    broken._launch = locked
    with pytest.raises(RuntimeError):
        await broken.resume(run_id, live={"fixed": True})
    assert harness.store.get_run(run_id)["status"] == "failed"

    again = harness.manager()
    await again.resume(run_id, live={"fixed": True})
    assert (await settle(again, run_id))["status"] == "succeeded"


# ------------------------------------------------------------------ RES-7: a cut attempt's cost counts

class SpendingBackend(FakeBackend):
    """An agent run that spends 0.5 before it answers -- like backend._run_agent, into meta in its finally."""

    def __init__(self, gate: asyncio.Event = None):
        super().__init__()
        self.gate = gate

    async def agent_create(self, act, **kw):
        try:
            if self.gate is not None:
                await self.gate.wait()
            return "done", "inst"
        finally:
            act.meta["cost"] = round((act.meta.get("cost") or 0.0) + 0.5, 6)
            tokens = act.meta.setdefault("tokens", {"prompt": 0, "completion": 0, "cached": 0})
            tokens["prompt"] += 100


ONE_AGENT = """\
stategraph: 1
id: m
initial: work
states:
  work:
    do: {agent: w, task: go}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_res7_the_cost_of_an_attempt_a_stop_cut_off_counts_in_the_spend(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": ONE_AGENT}), backend=SpendingBackend(asyncio.Event()))
    await until(lambda: "s0" in activity_rows(harness.store, run_id), what="work started")
    await manager.shutdown()
    assert (harness.store.spend(run_id) or {}).get("cost") == 0.5

    row = await resume(harness, run_id, SpendingBackend())

    assert row["status"] == "succeeded", row
    spend = harness.store.spend(run_id)
    assert (spend["cost"], spend["tokens"]["prompt"]) == (1.0, 200), spend


# ------------------------------------------------------------------ ENG-1: map over a string or a dict

MAP = """\
stategraph: 1
id: m
context: {items: ITEMS, got: null}
initial: each
states:
  each:
    do: {map: ctx.items, each: {agent: w, task: "x {{ item }}"}}
    transitions:
      - target: done
        effect: ctx.got = out
      - trigger: error
        target: bad
        effect: ctx.got = [error.type, error.message]
  done: {type: final, output: "{{ ctx.got }}"}
  bad: {type: final, output: "{{ ctx.got }}"}
"""


@pytest.mark.parametrize("items,final,shown", [
    ('"ab"', "bad", "got str"),
    ("{a: 1, b: 2}", "bad", "got dict"),
    ("null", "done", None),
])
async def test_eng1_map_refuses_a_string_or_a_dict(harness, items, final, shown):
    backend = FakeBackend()
    row = await run(harness, {"m.yaml": MAP.replace("ITEMS", items)}, backend)

    assert row["final_state"] == final, row
    if shown:
        assert row["output"][0] == "template_failed" and shown in row["output"][1], row["output"]
    else:
        assert row["output"] == []
    assert backend.calls == []


# ------------------------------------------------------------------ ENG-2: a choice branch into a failing junction

CHOICE = """\
stategraph: 1
id: m
context: {x: 1, y: 0}
initial: INITIAL
states:
  start:
    transitions: [{target: route}]
  route:
    type: choice
    transitions:
      - guard: ctx.x > 0
        target: j
      - guard: else
        target: other
  j:
    type: junction
    transitions:
      - guard: ctx.y > 0
        target: a
      - guard: ctx.y < 0
        target: a
  a: {type: final, output: a}
  other: {type: final, output: other}
"""


@pytest.mark.parametrize("initial", ["start", "route"])
async def test_eng2_a_junction_that_holds_no_branch_falls_through_to_the_choice_s_else(harness, initial):
    row = await run(harness, {"m.yaml": CHOICE.replace("INITIAL", initial)})

    assert (row["status"], row["output"]) == ("succeeded", "other"), row
    if initial == "start":
        guards = traces(harness, row["id"], "transition")[-1]["data"]["guards"]
        assert [g["guard"] for g in guards].count("ctx.x > 0") == 1, guards


# ------------------------------------------------------------------ ENG-3, ENG-4: events go to frames that take them now

ACTIVITY_EVENT = """\
stategraph: 1
id: m
events: {cancel: {}, ok: {}}
initial: write
states:
  write:
    do: {agent: w, task: go}
    transitions:
      - target: confirm
      - trigger: cancel
        target: aborted
  confirm:
    transitions:
      - {trigger: cancel, target: aborted}
      - {trigger: ok, target: done}
  aborted: {type: final}
  done: {type: final}
"""


async def test_eng4_an_event_while_an_activity_runs_is_queued_not_routed_to_its_frame(harness):
    gate = asyncio.Event()
    backend = FakeBackend({"write": held(gate, "W")})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": ACTIVITY_EVENT}), backend=backend)
    await until(lambda: backend.count("write") == 1, what="write in flight")

    answer = manager.send_event(run_id, "cancel")

    assert answer == {"accepted": True, "frame": None, "queued": True}, answer
    gate.set()
    assert (await settle(manager, run_id))["final_state"] == "aborted"  # the first wait that accepts it took it


SUB_STOP = """\
stategraph: 1
id: sub
events: {stop: {}}
initial: work
states:
  work:
    do: {agent: w, task: go}
    transitions:
      - target: fin
      - trigger: stop
        target: fin
  fin: {type: final}
"""

ROOT_STOP = """\
stategraph: 1
id: m
events: {stop: {}, go: {}}
imports: {sub: ./sub.yaml}
initial: call
states:
  call:
    do: {machine: sub}
    transitions: [{target: wait_stop}]
  wait_stop:
    transitions:
      - {trigger: stop, target: done}
      - {trigger: go, target: done}
  done: {type: final}
"""

STOP_FILES = {"m.yaml": ROOT_STOP, "sub.yaml": SUB_STOP}


async def start_in_sub(harness: Harness, gate: asyncio.Event):
    backend = FakeBackend({"call/work": held(gate, "W")})
    manager = harness.manager()
    run_id = await manager.start(runnable(STOP_FILES), backend=backend)
    await until(lambda: backend.count("call/work") == 1, what="the submachine's work in flight")
    return manager, run_id


async def test_eng3_an_explicit_frame_that_does_not_declare_the_event_is_refused(harness):
    manager, run_id = await start_in_sub(harness, asyncio.Event())

    answer = manager.send_event(run_id, "go", frame="s0/m/")

    assert answer["accepted"] is False and "does not declare event 'go'" in answer["reason"], answer
    assert answer.get("data_refused") is True, answer  # callback and wait questions answer 422, not "try later"


async def test_eng3_an_event_sent_to_a_frame_that_ends_is_dropped_and_traced(harness):
    gate = asyncio.Event()
    manager, run_id = await start_in_sub(harness, gate)
    assert manager.send_event(run_id, "stop", frame="s0/m/")["accepted"]

    gate.set()  # the work ends normally: the submachine ends without taking it
    row = await manager.wait(run_id, timeout=3)

    assert row["status"] == "waiting" and row["view"]["inbox"] == [], row["view"]
    assert [t["data"]["event"] for t in traces(harness, run_id, "event_dropped")] == ["stop"]
    assert manager.send_event(run_id, "stop")["accepted"]
    assert (await settle(manager, run_id))["status"] == "succeeded"


async def test_eng3_an_event_an_older_engine_pinned_to_an_ending_frame_goes_back_to_the_run(harness):
    gate = asyncio.Event()
    first, run_id = await start_in_sub(harness, gate)
    await first.shutdown()
    harness.store.record(run_id, "event", "pending:old", status="pending",
                         data={"name": "stop", "data": None, "frame": "s0/m/"})  # as routing by structure pinned it

    manager = harness.manager()
    await manager.resume(run_id, backend=FakeBackend({"call/work": "W"}))
    row = await settle(manager, run_id, timeout=3)

    assert (row["status"], row["final_state"]) == ("succeeded", "done"), row


# ------------------------------------------------------------------ ENG-5: the view names the initial state at once

async def test_eng5_the_stored_view_names_the_initial_state_while_its_activity_runs(harness):
    backend = FakeBackend({"work": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": ONE_AGENT}), backend=backend)
    await until(lambda: backend.count("work") == 1, what="work in flight")

    frame = harness.store.get_run(run_id)["view"]["frames"][0]
    assert (frame["state"], frame["config"]) == ("work", ["work"]), frame


# ------------------------------------------------------------------ ENG-6: the same answer in a later round

CHECKED = """\
stategraph: 1
id: m
python: m.py
initial: fix
states:
  fix:
    do: {agent: w, task: fix it, check: verify, parse_retries: 2}
    transitions:
      - target: done
      - trigger: error
        target: bad
  done: {type: final}
  bad: {type: final}
"""

CHECKED_PY = """\
async def verify(sg, value):
    state = await sg.tool("read_state", {})
    if state != "good":
        raise ValueError(f"state is {state}")
"""


async def test_eng6_a_check_s_tools_run_again_when_a_later_round_gives_the_same_answer(harness):
    backend = FakeBackend({"fix": "done", "fix/read_state": sequence("bad", "good")})
    row = await run(harness, {"m.yaml": CHECKED, "m.py": CHECKED_PY}, backend)

    assert row["final_state"] == "done", row
    assert backend.count("fix/read_state") == 2


# ------------------------------------------------------------------ ENG-7: a mocked $error goes through retry

RETRIED = """\
stategraph: 1
id: m
context: {out: null}
initial: work
states:
  work:
    do: {agent: worker, task: go, retry: {attempts: 3, errors: [agent_failed]}}
    transitions:
      - target: ok
        effect: ctx.out = out
      - trigger: error
        target: bad
  ok: {type: final, output: "{{ ctx.out }}"}
  bad: {type: final}
"""


async def test_eng7_a_mocked_error_is_retried_like_a_live_failure(harness):
    mocks = {"work": {"$visits": [{"$error": {"type": "agent_failed", "message": "flaky"}}, "second try works"]}}
    row = await run(harness, {"m.yaml": RETRIED}, mocks=mocks, mock_only=True)

    assert (row["final_state"], row["output"]) == ("ok", "second try works"), row
    meta = activity_rows(harness.store, row["id"])["s0"]["data"]["meta"]
    assert (meta["attempts"], meta["mocked"], [f["type"] for f in meta["failures"]]) == (2, True, ["agent_failed"])


# ------------------------------------------------------------------ ENG-8: sg.tool() in a sync function

SYNC_TOOLS = """\
stategraph: 1
id: m
python: m.py
context: {got: null}
initial: a
states:
  a:
    do: {call: FN}
    transitions:
      - target: done
        effect: ctx.got = out
      - trigger: error
        target: done
        effect: ctx.got = [error.type, error.message]
  done: {type: final, output: "{{ ctx.got }}"}
"""

SYNC_TOOLS_PY = """\
def uses_value(sg):
    state = sg.tool("read_state", {})
    return "good" if state == "good" else "not good"


def discards(sg):
    sg.tool("write", {})
    return "written"


def returns(sg):
    return sg.tool("read_state", {})
"""


@pytest.mark.parametrize("fn,shown,calls", [
    ("uses_value", "no answer yet", 0),
    ("discards", "runs only when the function returns it", 0),
    ("returns", None, 1),
])
async def test_eng8_a_sync_function_cannot_use_sg_tool_s_value(harness, fn, shown, calls):
    backend = FakeBackend({"a/read_state": "good", "a/write": "ok"})
    row = await run(harness, {"m.yaml": SYNC_TOOLS.replace("FN", fn), "m.py": SYNC_TOOLS_PY}, backend)

    if shown:
        assert row["output"][0] == "call_failed" and shown in row["output"][1], row["output"]
    else:
        assert row["output"] == "good", row["output"]
    assert len(backend.calls) == calls


# ------------------------------------------------------------------ ENG-10: two pauses in one step

async def test_eng10_an_enter_and_an_exit_pause_keep_a_row_each(harness):
    backend = FakeBackend({"work": "W"})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": ONE_AGENT}), backend=backend,
                                 breakpoints=[{"state": "work", "at": "enter"}, {"state": "work", "at": "exit"}])
    for _ in range(2):
        await until(lambda: manager.live[run_id].ctx.status == "paused", what="a pause")
        manager.control(run_id, "continue")
        await asyncio.sleep(0.05)
    await settle(manager, run_id)

    paused = traces(harness, run_id, "paused")
    assert [row["data"]["hook"] for row in paused] == ["enter", "exit"], paused
    work = activity_rows(harness.store, run_id)["s0"]["seq"]
    assert paused[0]["seq"] < work < paused[1]["seq"]


# ------------------------------------------------------------------ ENG-11: a resumed wait waits only the rest

WAITS = """\
stategraph: 1
id: m
events: {go: {}}
initial: w
states:
  w:
    WAIT
  done: {type: final, output: early}
  late: {type: final, output: late}
"""


@pytest.mark.parametrize("wait", [
    "timeout: 2\n    transitions:\n      - {trigger: go, target: done}\n      - {trigger: error, target: late}",
    "after: 2\n    transitions:\n      - {trigger: go, target: done}\n      - {target: late}",
], ids=["wait_timeout", "timer_after"])
async def test_eng11_a_resumed_wait_waits_only_the_rest_of_its_time(harness, wait):
    first = harness.manager()
    run_id = await first.start(runnable({"m.yaml": WAITS.replace("WAIT", wait)}))
    await until(lambda: harness.store.get_run(run_id)["status"] == "waiting", what="the wait")
    await asyncio.sleep(1.2)
    await first.shutdown()

    began = time.monotonic()
    row = await resume(harness, run_id, FakeBackend(), timeout=4)

    assert row["output"] == "late", row
    assert time.monotonic() - began < 1.6  # the 0.8 s left, not 2 s anew


# ------------------------------------------------------------------ DBG-1: errors name where the code failed

LOCATED = """\
stategraph: 1
id: m
python: m.py
context: {a: {}}
initial: one
states:
  one:
    BODY
  two: {type: final}
"""

LOCATED_PY = """\
def boom(x=None):
    raise RuntimeError("boom")
"""


@pytest.mark.parametrize("body,type_,where", [
    ("transitions:\n      - {guard: 'False', target: two}\n      - {target: two, effect: 'ctx.b = ctx.a[\"nope\"]'}",
     "action_failed", "one.transitions[1].effect"),
    ("exit: ctx.b = ctx.a['nope']\n    transitions: [{target: two}]", "action_failed", "one.exit"),
    ("do: {call: boom, args: {x: \"{{ ctx.a['nope'] }}\"}}\n    transitions: [{target: two}]",
     "template_failed", "one.do.args"),
])
async def test_dbg1_an_error_from_machine_code_names_its_field(harness, body, type_, where):
    row = await run(harness, {"m.yaml": LOCATED.replace("BODY", body), "m.py": LOCATED_PY})

    assert (row["error"]["type"], row["error"]["state"]) == (type_, "one"), row["error"]
    assert row["error"]["where"].startswith(where), row["error"]


async def test_dbg1_machine_vars_and_a_call_s_own_failure_name_where(harness):
    vars_row = await run(harness, {"m.yaml": LOCATED.replace("BODY", "transitions: [{target: two}]").replace(
        "initial: one", "vars: {p: \"{{ ctx.a['missing'] }}\"}\ninitial: one"), "m.py": LOCATED_PY})
    assert vars_row["error"]["type"] == "template_failed" and vars_row["error"]["where"].startswith("m.vars"),         vars_row["error"]

    call_row = await run(harness, {"m.yaml": LOCATED.replace("BODY", "do: {call: boom}\n    transitions: [{target: two}]"),
                                   "m.py": LOCATED_PY})
    assert (call_row["error"]["message"], call_row["error"]["where"]) == ("RuntimeError: boom", "one"), call_row["error"]


# ------------------------------------------------------------------ DBG-3: call and decide under their own request id

REQUESTS = """\
stategraph: 1
id: m
python: m.py
context: {got: null}
initial: a
states:
  a:
    do: {call: seen, retry: {attempts: 2}}
    transitions:
      - target: b
        effect: ctx.got = out
  b:
    do: {decide: noul, question: fine?, input: "{{ ctx.got }}"}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.got }}"}
"""

REQUESTS_PY = """\
from agent_system.tools.status import current_request_id

tries = []


def seen():
    tries.append(current_request_id.get())
    if len(tries) == 1:
        raise RuntimeError("first try fails")
    return tries[-1]
"""


class RequestBackend(FakeBackend):
    async def decide(self, act, **kw):
        from agent_system.tools.status import current_request_id

        self.decided_under = current_request_id.get()
        return {"decision": {"value": 0.9, "confidence": None, "probabilities": None}}


async def test_dbg3_call_and_decide_activities_run_under_their_own_request_ids(harness):
    backend = RequestBackend()
    row = await run(harness, {"m.yaml": REQUESTS, "m.py": REQUESTS_PY}, backend)
    run_id = row["id"]

    assert row["output"] == f"{run_id}_002", row  # the second attempt's own id, seen inside the function
    meta = activity_rows(harness.store, run_id)["s0"]["data"]["meta"]
    assert meta["request_ids"] == [f"{run_id}_001", f"{run_id}_002"] and meta["request_id"] == f"{run_id}_002"
    assert meta["failures"][0]["request_id"] == f"{run_id}_001", meta["failures"]
    decide = activity_rows(harness.store, run_id)["s1"]["data"]["meta"]
    assert backend.decided_under == decide["request_id"] == f"{run_id}_003"


async def test_dbg3_a_call_s_request_id_names_its_run(harness):
    """What finds the run a call activity's code belongs to (service.run_to_end's enclosing run)."""
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": ONE_AGENT}), backend=FakeBackend({"work": held(asyncio.Event())}))

    assert manager.run_of_request(f"{run_id}_007") is manager.live[run_id]
    assert manager.run_of_request(run_id) is manager.live[run_id]
    assert manager.run_of_request(f"{run_id}_sg1") is None and manager.run_of_request(None) is None


# ------------------------------------------------------------------ DBG-5: a divergence names what differs

CHAIN = """\
stategraph: 1
id: m
context: {a: null, b: null}
initial: a
states:
  a:
    do: {agent: w, task: first}
    transitions:
      - target: b
        effect: ctx.a = out
  b:
    do: {agent: w, task: "use {{ ctx.a }}"}
    transitions:
      - target: done
        effect: ctx.b = out
  done: {type: final}
"""


async def crashed_in_b(harness: Harness) -> str:
    backend = FakeBackend({"a": "A", "b": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": CHAIN}), backend=backend)
    await until(lambda: backend.count("b") == 1, what="b in flight")
    await manager.shutdown()
    return run_id


async def test_dbg5_an_input_divergence_names_the_input_that_changed(harness):
    run_id = await crashed_in_b(harness)
    changed_snapshot(harness, run_id, '"use {{ ctx.a }}"', '"using {{ ctx.a }}"')

    row = await resume(harness, run_id, FakeBackend({"b": "B"}))

    assert row["error"]["type"] == "diverged", row
    assert 'inputs.task was "use A", now "using A"' in row["error"]["message"], row["error"]


async def test_dbg5_a_step_divergence_names_the_ctx_key_that_changed(harness):
    run_id = await crashed_in_b(harness)
    changed_snapshot(harness, run_id, "effect: ctx.a = out", "effect: ctx.a = out + '!'")

    row = await resume(harness, run_id, FakeBackend({"b": "B"}))

    assert row["error"]["type"] == "diverged", row
    assert "after s1:step ctx.a differ from the recorded run" in row["error"]["message"], row["error"]


# ------------------------------------------------------------------ PROD-6: started rows that ended without outcome

def test_prod6_started_rows_below_an_ended_composite_or_of_an_ended_run_are_marked():
    rows = [{"kind": "activity", "key": "s0", "status": "done"},
            {"kind": "activity", "key": "s0/b.x", "status": "started"},          # cancelled by a fail-fast join
            {"kind": "activity", "key": "s0/b.y", "status": "done"},
            {"kind": "activity", "key": "s1", "status": "started"},             # in flight
            {"kind": "activity", "key": "s1/m/s2", "status": "started"},        # in flight below it
            {"kind": "activity", "key": "rewound.1/s3", "status": "started"},
            {"kind": "trace", "key": "s0/b.x:joined", "status": "started"}]

    assert ended_without_outcome(rows, terminal=False) == {"s0/b.x"}
    assert ended_without_outcome(rows, terminal=True) == {"s0/b.x", "s1", "s1/m/s2"}
