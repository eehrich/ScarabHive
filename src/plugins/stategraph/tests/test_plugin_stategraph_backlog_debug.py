"""Regressions for the backlog of the stategraph review of 2026-09-27 (docs/backlog.md), phase 2: what a run tells
about itself -- inputs, guards, tracebacks, failed attempts, waits -- and the debugger's fork and journal access.

Machines run through the real RunManager (FakeBackend at the boundary) or through the real tools of a
StateGraphServer on tmp_path (call activities, no LLM).
"""

from __future__ import annotations

import logging

import pytest

from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, activity_rows, runnable,
                                                         settle, until)
from plugins.stategraph.tests.test_plugin_stategraph_server import run_tool, server  # noqa: F401 -- a fixture

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    made = Harness(tmp_path)
    yield made
    await made.close()


# ------------------------------------------------------------------ G1, G2: what an activity got, which guards held

JUDGED = {"m.yaml": """\
stategraph: 1
id: m
context: {n: 7}
initial: ask
states:
  ask:
    do: {agent: judge, task: "Judge {{ ctx.n }}"}
    transitions:
      - target: done
        guard: out == "yes"
  done: {type: final}
"""}


async def test_an_ended_activity_keeps_what_it_was_given(harness):
    row = await harness.run(JUDGED, backend=FakeBackend({"ask": "no"}))

    [activity] = activity_rows(harness.store, row["id"]).values()
    inputs = activity["data"]["inputs"]
    assert (activity["status"], inputs["agent"], inputs["task"]) == ("done", "judge", "Judge 7"), activity


async def test_no_transition_names_the_guards_it_evaluated(harness):
    row = await harness.run(JUDGED, backend=FakeBackend({"ask": "no"}))

    assert row["error"]["type"] == "no_transition", row["error"]
    assert row["error"]["data"] == {"guards": [{"at": "ask.transitions", "guard": 'out == "yes"', "result": False}]}


async def test_a_transition_trace_names_the_guards_that_let_it_through(harness):
    row = await harness.run(JUDGED, backend=FakeBackend({"ask": "yes"}))

    [taken] = [r["data"] for r in harness.store.rows(row["id"], kinds=("trace",)) if r["status"] == "transition"]
    assert (row["status"], taken["guards"]) == ("succeeded", [
        {"at": "ask.transitions", "guard": 'out == "yes"', "result": True}]), taken


async def test_a_guard_that_raises_is_named_with_the_ones_before_it(harness):
    guards = ('      - target: ask\n        guard: out == "again"\n'
              '      - target: done\n        guard: ctx.missing["x"] > 1')
    files = {"m.yaml": JUDGED["m.yaml"].replace('      - target: done\n        guard: out == "yes"', guards)}
    assert files["m.yaml"] != JUDGED["m.yaml"]
    row = await harness.run(files, backend=FakeBackend({"ask": "no"}))

    assert row["error"]["type"] == "guard_failed", row["error"]
    first, second = row["error"]["data"]["guards"]
    assert (first, second["guard"], "error" in second) == (
        {"at": "ask.transitions", "guard": 'out == "again"', "result": False}, 'ctx.missing["x"] > 1', True), second


# ------------------------------------------------------------------ G3, G4: a call's traceback, failed attempts

CALLS = {"m.yaml": """\
stategraph: 1
id: m
python: m.py
initial: flaky
states:
  flaky:
    do: {call: flaky, args: {}, retry: {attempts: 2, backoff: 0}}
    transitions: [{target: boom}]
  boom:
    do: {call: explode, args: {}}
    transitions: [{target: done}]
  done: {type: final}
""", "m.py": """\
TRIES = []


def flaky():
    TRIES.append(1)
    if len(TRIES) == 1:
        raise ValueError("first try")
    return len(TRIES)


def explode():
    table = {}
    return table["missing"]
"""}


async def test_a_call_that_raises_leaves_its_traceback_and_a_log_line(harness, caplog):
    with caplog.at_level(logging.WARNING, logger="plugins.stategraph.engine.activity"):
        row = await harness.run(CALLS)

    error = activity_rows(harness.store, row["id"])["s1"]["data"]["error"]
    assert (error["type"], error["message"]) == ("call_failed", "KeyError: 'missing'"), error
    assert 'm.py", line 13, in explode' in error["data"]["traceback"], error["data"]["traceback"]
    assert any("boom" in record.getMessage() for record in caplog.records), [r.getMessage() for r in caplog.records]


async def test_the_failed_attempts_of_a_retried_activity_stay_readable(harness):
    row = await harness.run(CALLS)

    flaky = activity_rows(harness.store, row["id"])["s0"]["data"]
    assert (flaky["out"], flaky["meta"]["attempts"], flaky["meta"]["failures"]) == (
        2, 2, [{"attempt": 1, "type": "call_failed", "message": "ValueError: first try"}]), flaky


# ------------------------------------------------------------------ G5: a fork held at its fork point, new mocks

async def test_a_fork_can_hold_at_the_fork_point_and_take_new_mocks(server):
    first, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello"})
    assert first["run_status"] == "succeeded", first

    held, _ = await run_tool(server, "stategraph_control_run", {"run_id": first["run_id"], "action": "fork",
                                                                 "at_step": 0, "pause": True})
    await until(lambda: (server.run_store.get_run(held["run_id"]) or {}).get("status") == "paused", what="held")
    assert server.run_store.get_run(held["run_id"])["debug"]["paused"]["state"] == "greet"
    await run_tool(server, "stategraph_control_run", {"run_id": held["run_id"], "action": "continue"})
    assert (await settle(server.run_manager, held["run_id"]))["status"] == "succeeded"

    mocked, _ = await run_tool(server, "stategraph_control_run", {"run_id": first["run_id"], "action": "fork",
                                                                   "at_step": 0, "mocks": {"greet": "Hi, mock!"}})
    row = await settle(server.run_manager, mocked["run_id"])
    assert row["output"] == {"greeting": "Hi, mock!"}, row


async def test_run_machine_can_hold_the_run_at_its_start(server):
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "pause_at_start": True})

    assert (started["run_status"], started["paused"]["state"]) == ("paused", "greet"), started


# ------------------------------------------------------------------ G6: journal rows by kind, state and seq; bounded

async def test_get_run_filters_the_journal_and_bounds_what_it_answers(server):
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "params": {"name": "x" * 30000}})
    run_id = started["run_id"]

    assert started["output"]["greeting"].endswith("... (30008 characters)"), started["output"]["greeting"][-40:]
    only, _ = await run_tool(server, "stategraph_get_run", {"run_id": run_id, "kinds": ["activity"]})
    assert [row["kind"] for row in only["journal"]] == ["activity"], only["journal"]
    assert len(only["journal"][0]["data"]["out"]) < 2100, "a journal row's text is not bounded"
    of_state, _ = await run_tool(server, "stategraph_get_run", {"run_id": run_id, "state": "done"})
    assert of_state["journal"] and {row["state"] for row in of_state["journal"]} == {"done"}, of_state["journal"]
    everything, _ = await run_tool(server, "stategraph_get_run", {"run_id": run_id, "steps": 200})
    seqs = [row["seq"] for row in everything["journal"]]
    after, _ = await run_tool(server, "stategraph_get_run", {"run_id": run_id, "after": seqs[1], "steps": 2})
    assert [row["seq"] for row in after["journal"]] == seqs[2:4]


@pytest.mark.parametrize("arguments,says", [
    ({"kinds": ["lease"]}, "kinds"), ({"after": -1}, "after"), ({"state": 3}, "state"),
])
async def test_get_run_refuses_filters_it_does_not_know(server, arguments, says):
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello"})

    result, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"], **arguments})

    assert (result["status"], result.get("error_type"), result.get("error", "").startswith(says)) == (
        "error", "http_422", True), result


# ------------------------------------------------------------------ G7: what a waiting run waits for, and since when

WAITING = """\
stategraph: 1
id: waiting
events: {go: {}, later: {}}
initial: hold
states:
  hold:
    timeout: 1h
    transitions:
      - trigger: go
        target: done
      - trigger: error
        target: done
  done: {type: final}
"""


async def test_a_waiting_frame_says_since_when_and_until_when_and_the_inbox_is_shown(server, tmp_path):
    (tmp_path / "machines" / "waiting.yaml").write_text(WAITING, encoding="utf-8")
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "waiting", "wait": "background"})
    run_id = started["run_id"]
    await until(lambda: (server.run_store.get_run(run_id) or {}).get("status") == "waiting", what="the wait")
    queued, _ = await run_tool(server, "stategraph_send_event", {"run_id": run_id, "name": "later"})
    assert queued.get("queued") is True, queued

    seen, _ = await run_tool(server, "stategraph_get_run", {"run_id": run_id})

    frame = seen["frames"][0]
    assert frame["waiting_since"] and frame["deadline"] > frame["waiting_since"], frame
    assert [item["name"] for item in seen["inbox"]] == ["later"], seen.get("inbox")
    await run_tool(server, "stategraph_send_event", {"run_id": run_id, "name": "go"})
    assert (await settle(server.run_manager, run_id))["status"] == "succeeded"


# ------------------------------------------------------------------ the review of phase 2

CHAINED = {"m.yaml": """\
stategraph: 1
id: m
python: m.py
initial: parse
states:
  parse:
    do: {call: parse, args: {}}
    transitions: [{target: done}]
  done: {type: final}
""", "m.py": """\
import json


def parse():
    try:
        return json.loads("{" + "x" * 3000)
    except ValueError as exc:
        raise RuntimeError("could not parse the answer") from exc
"""}


async def test_a_long_traceback_keeps_its_end_where_the_error_line_is(harness):
    row = await harness.run(CHAINED)

    trace = row["error"]["data"]["traceback"]
    assert len(trace) <= 2000 and trace.rstrip().endswith("RuntimeError: could not parse the answer"), trace[-300:]


FORKED = """\
stategraph: 1
id: forked
python: forked.py
initial: a
states:
  a:
    do: {call: greet, args: {name: a}}
    transitions: [{target: b}]
  b:
    transitions: [{target: c}]
  c:
    do: {call: greet, args: {name: c}}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.last }}"}
"""


async def test_a_held_fork_stops_at_its_fork_point_not_at_a_state_without_do_before_it(server, tmp_path):
    (tmp_path / "machines" / "forked.yaml").write_text(FORKED.replace('output: "{{ ctx.last }}"', "output: done"),
                                                       encoding="utf-8")
    (tmp_path / "machines" / "forked.py").write_text("def greet(name):\n    return name\n", encoding="utf-8")
    first, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "forked"})
    assert first["run_status"] == "succeeded", first

    held, _ = await run_tool(server, "stategraph_control_run", {"run_id": first["run_id"], "action": "fork",
                                                                 "at_step": 2, "pause": True})
    await until(lambda: (server.run_store.get_run(held["run_id"]) or {}).get("status") == "paused", what="held")

    paused = server.run_store.get_run(held["run_id"])["debug"]["paused"]
    assert (paused["state"], paused["hook"], paused["step"]) == ("c", "enter", 2), paused
    await run_tool(server, "stategraph_control_run", {"run_id": held["run_id"], "action": "continue"})
    assert (await settle(server.run_manager, held["run_id"]))["status"] == "succeeded"


async def test_a_fork_s_mocks_win_over_the_source_s(server):
    first, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "mocks": {"greet": "Hi, A"}})
    assert first["output"] == {"greeting": "Hi, A"}, first

    forked, _ = await run_tool(server, "stategraph_control_run", {"run_id": first["run_id"], "action": "fork",
                                                                   "at_step": 0, "mocks": {"greet": "Hi, B"}})

    assert (await settle(server.run_manager, forked["run_id"]))["output"] == {"greeting": "Hi, B"}


SUB = """\
stategraph: 1
id: sub
params: {label: {type: string, required: true}}
events: {approve: {}, poke: {}}
initial: w
states:
  w:
    timeout: 1h
    transitions:
      - trigger: approve
        target: ok
      - trigger: poke
      - trigger: error
        target: ok
  ok: {type: final, output: "{{ params.label }}"}
"""
BOTH = """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
initial: work
states:
  work:
    do:
      parallel:
        left: {machine: sub, params: {label: left}}
        slow: {agent: w, task: go}
        right: {machine: sub, params: {label: right}}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_every_waiting_frame_is_in_the_stored_view_and_keeps_its_since(harness):
    """The second frame starts waiting while the run is waiting already: its wait must reach the stored view (the
    panel and REST read it). An internal transition does not restart since when it waits; a resume keeps it too."""
    import asyncio

    async def slow(call):
        await asyncio.sleep(0.2)
        return "x"

    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": BOTH, "sub.yaml": SUB}), backend=FakeBackend({"work/slow": slow}))
    ctx = manager.live[run_id].ctx

    def stored():
        view = harness.store.get_run(run_id)["view"] or {}
        return {f["prefix"]: f.get("waiting_since") for f in view.get("frames", []) if f["prefix"]}

    await until(lambda: len([s for s in stored().values() if s]) == 2, what="both waits in the stored view")
    before = stored()
    frame = next(prefix for prefix in before)
    assert manager.send_event(run_id, "poke", frame=frame)["accepted"] is True
    await asyncio.sleep(0.2)
    assert stored() == before, "an internal transition restarted the wait"
    await manager.shutdown()

    second = harness.manager()
    await second.resume(run_id, backend=FakeBackend({"work/slow": slow}))
    await until(lambda: run_id in second.live and len([f for f in second.live[run_id].ctx.frames
                                                          if f.waiting_since]) == 2, what="the waits again")
    await asyncio.sleep(0.1)
    assert (stored(), bool(ctx)) == (before, True), "a resume restarted since when the frames wait"


NEVER = {"m.yaml": """\
stategraph: 1
id: m
python: m.py
initial: flaky
states:
  flaky:
    do: {call: never, args: {}, retry: {attempts: 3, backoff: 1s}}
    transitions: [{target: done}]
  done: {type: final}
""", "m.py": """\
def never():
    raise ValueError("always")
"""}


async def test_the_failed_attempts_survive_a_crash_in_the_backoff(harness):
    first = harness.manager()
    run_id = await first.start(runnable(NEVER))
    await until(lambda: (activity_rows(harness.store, run_id).get("s0", {}).get("data") or {}).get("backoff_until"),
                what="the first backoff")
    await first.shutdown()
    second = harness.manager()
    await second.resume(run_id)
    await settle(second, run_id, timeout=10)

    meta = activity_rows(harness.store, run_id)["s0"]["data"]["meta"]
    assert (meta["attempts"], [f["attempt"] for f in meta["failures"]]) == (3, [1, 2]), meta


INITIAL_CHOICE = {"m.yaml": """\
stategraph: 1
id: m
context: {n: 1}
initial: pick
states:
  pick:
    type: choice
    transitions:
      - target: done
        guard: ctx.missing["x"] > 5
      - target: done
        guard: else
  done: {type: final}
"""}

DISCARD = {"m.yaml": """\
stategraph: 1
id: m
context: {n: 1}
events: {go: {}}
initial: w
states:
  w:
    transitions:
      - trigger: go
        target: done
        guard: ctx.n > 5
  done: {type: final}
"""}


async def test_an_initial_choice_and_a_discarded_event_name_their_guards(harness):
    row = await harness.run(INITIAL_CHOICE)
    assert [g["guard"] for g in row["error"]["data"]["guards"]] == ['ctx.missing["x"] > 5'], row["error"]

    manager = harness.manager()
    run_id = await manager.start(runnable(DISCARD))
    await until(lambda: harness.store.get_run(run_id)["status"] == "waiting", what="the wait")
    manager.send_event(run_id, "go")
    await until(lambda: any(r["status"] == "event_discarded" for r in harness.store.rows(run_id, kinds=("trace",))),
                what="the event discarded")
    [discarded] = [r["data"] for r in harness.store.rows(run_id, kinds=("trace",)) if r["status"] == "event_discarded"]
    assert discarded["guards"] == [{"at": "w.transitions", "guard": "ctx.n > 5", "result": False}], discarded
    manager.control(run_id, "terminate")
    await settle(manager, run_id)


async def test_the_output_in_full_on_request_and_a_bounded_answer(server):
    import json

    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "params": {"name": "x" * 30000},
                                                                    "full_output": True})
    assert len(started["output"]["greeting"]) == 30008, "full_output cut the output"
    from plugins.stategraph import server as server_module

    frames = [{"prefix": "", "ctx": {f"k{i}": "y" * 1999 for i in range(150)}}]
    journal = [{"seq": i, "data": {"out": "z" * 1999}} for i in range(100)]
    answer = server_module._bounded({"run_id": "r", "frames": frames, "journal": journal})
    assert len(json.dumps(answer)) <= server_module.ANSWER_CHARS, len(json.dumps(answer))
    assert answer["journal"] == [] and "journal_cut" in answer and "frames_cut" in answer, sorted(answer)
    assert answer["frames"][0]["ctx"]["$too_large"]["k0"] == 2001
    small = server_module._bounded({"run_id": "r", "frames": [], "journal": journal[:3]})
    assert (len(small["journal"]), "journal_cut" in small) == (3, False)
