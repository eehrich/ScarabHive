"""Regressions for the engine findings of the review of 2026-09-26 (docs/stategraph_design.md §2.8, §3, §5).

One test (or one parametrised group) per fixed finding. Every machine runs through the real RunManager on a
RunStore under tmp_path; only the boundary behind the engine is FakeBackend. A "crash" is a real
``RunManager.shutdown()`` and a resume is a NEW manager on the same store.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, activity_rows, held,
                                                         resume, runnable, settle, until)

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


# ------------------------------------------------------------------ an error while an error transition runs (§3.5)

RETRY_BY_ERROR = """\
stategraph: 1
id: ID
initial: a
states:
  a:
    max_visits: 2
    do: {agent: w, task: go}
    transitions:
      - target: done
      - trigger: error
        target: a
  done: {type: final}
"""


async def test_a_loop_limit_raised_by_an_error_transition_ends_the_frame(harness):
    """The error transition re-enters a past its max_visits: that loop_limit is not handled again by the same
    error transition (which spun until step_limit)."""
    backend = FakeBackend({"a": ActivityError("agent_failed", "boom")})
    row = await run(harness, {"m.yaml": RETRY_BY_ERROR.replace("ID", "m")}, backend)

    assert (row["status"], row["error"]["type"], row["error"]["cause"]["type"]) == (
        "failed", "loop_limit", "agent_failed"), row
    assert backend.count("a") == 2


async def test_a_loop_limit_in_a_submachine_s_error_handling_reaches_the_caller(harness):
    files = {"sub.yaml": RETRY_BY_ERROR.replace("ID", "sub"), "m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
context: {cause: null}
initial: s
states:
  s:
    do: {machine: sub}
    transitions:
      - target: ok
      - trigger: error
        target: handled
        effect: ctx.cause = error.cause["type"]
  ok: {type: final}
  handled: {type: final, output: "{{ ctx.cause }}"}
"""}
    row = await run(harness, files, FakeBackend({"s/a": ActivityError("agent_failed", "boom")}))

    assert (row["status"], row["output"]) == ("succeeded", "loop_limit"), row


# ------------------------------------------------------------------ call: sync functions run off the loop

CALLS = """\
stategraph: 1
id: m
python: m.py
context: {got: null, v: 2}
initial: a
states:
  a:
    do: DO
    transitions:
      - target: done
        effect: ctx.got = out
      - trigger: error
        target: failed
        effect: ctx.got = [error.type, error.message]
  done: {type: final, output: "{{ ctx.got }}"}
  failed: {type: final, output: "{{ ctx.got }}"}
"""

CALLS_PY = """\
import asyncio
import threading
import time


def slow():
    time.sleep(0.6)
    return "late"


def reads(sg, x):
    return {"sum": sg.ctx.v + x, "loop_thread": threading.current_thread() is threading.main_thread()}


def returns_tool(sg):
    return sg.tool("t", {"v": sg.ctx.v})


def runs_tool_in_its_thread(sg):
    return asyncio.run(sg.tool("t", {}))


def empty():
    return next(iter([]))
"""


def calls(do: str) -> dict[str, str]:
    return {"m.yaml": CALLS.replace("DO", do), "m.py": CALLS_PY}


async def test_a_sync_call_does_not_block_the_loop_and_honours_its_timeout(harness):
    begun = time.monotonic()
    row = await run(harness, calls("{call: slow, timeout: 0.1s}"))

    assert row["output"][0] == "timeout", row
    assert time.monotonic() - begun < 0.45, "the sync function held the event loop until it returned"


async def test_a_sync_call_that_raises_stop_iteration_fails_instead_of_hanging(harness):
    """asyncio cannot set a StopIteration on the future the loop awaits: the run waited forever, its lease
    renewed, so no sweep ever ended it."""
    row = await run(harness, calls("{call: empty, timeout: 5s}"))

    assert row["status"] == "succeeded", row
    assert row["output"][0] == "call_failed" and "StopIteration" in row["output"][1], row["output"]


async def test_a_sync_call_reads_sg_in_its_thread_and_may_return_sg_tool(harness):
    row = await run(harness, calls("{call: reads, args: {x: 3}}"))
    assert row["output"] == {"sum": 5, "loop_thread": False}, row

    backend = FakeBackend({"a/t": "T"})
    row = await run(harness, calls("{call: returns_tool}"), backend)
    assert (row["output"], backend.calls[0]["args"]) == ("T", {"v": 2}), row


async def test_sg_tool_refuses_to_run_outside_the_run_s_loop(harness):
    backend = FakeBackend({"a/t": "T"})
    row = await run(harness, calls("{call: runs_tool_in_its_thread}"), backend)

    assert row["output"][0] == "call_failed" and "event loop" in row["output"][1], row
    assert backend.calls == []


# ------------------------------------------------------------------ a CancelledError that is not the run's (§5.8)

CANCELS = """\
stategraph: 1
id: m
python: m.py
initial: a
states:
  a:
    do: {call: FN}
    transitions:
      - target: done
      - trigger: error
        target: handled
        effect: ctx.err = error.type
  done: {type: final, output: done}
  handled: {type: final, output: "{{ ctx.err }}"}
context: {err: null}
"""

CANCELS_PY = """\
import asyncio


async def cancelled_inside():
    future = asyncio.get_running_loop().create_future()
    future.cancel()  # a library's future, cancelled by someone other than the run
    return await future


async def waits():
    await asyncio.sleep(30)
"""


class TokenBackend(FakeBackend):
    """The run's token lives on the backend (service.backend_factory); here it is cancelled already."""

    token = SimpleNamespace(is_cancelled=True)


@pytest.mark.parametrize("backend,status,output", [
    (FakeBackend(), "succeeded", "call_failed"),
    (TokenBackend(), "cancelled", None),
], ids=["stray", "run_token_cancelled"])
async def test_a_cancel_from_inside_an_activity_is_its_failure_unless_the_run_is_cancelled(harness, backend, status,
                                                                                           output):
    """A future some library cancelled fails the activity (the error transition runs); the same CancelledError
    while the run's token is cancelled from outside is the run's cancel."""
    row = await run(harness, {"m.yaml": CANCELS.replace("FN", "cancelled_inside"), "m.py": CANCELS_PY}, backend)

    assert (row["status"], row["output"]) == (status, output), row


async def test_a_terminate_while_a_call_awaits_still_cancels_the_run(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": CANCELS.replace("FN", "waits"), "m.py": CANCELS_PY}))
    await until(lambda: "s0" in activity_rows(harness.store, run_id), what="the call started")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert (row["status"], row["output"]) == ("cancelled", None), row


# ------------------------------------------------------------------ a crash in a retry's backoff (§5.5)

BACKOFF = """\
stategraph: 1
id: m
context: {err: null}
initial: a
states:
  a:
    do: {DO, retry: {attempts: 2, backoff: 1s}}
    transitions:
      - target: done
      - trigger: error
        target: failed
        effect: ctx.err = error.type
  done: {type: final}
  failed: {type: final, status: failed, output: "{{ ctx.err }}"}
"""


@pytest.mark.parametrize("do,error", [
    ("tool: t", "tool_failed"),
    ("agent: w, task: go", "agent_failed"),
], ids=["not_idempotent_tool", "idempotent_agent"])
async def test_a_resume_in_the_backoff_starts_the_next_attempt_when_it_is_due(harness, do, error):
    """Not 'in flight': the tool is not interrupted, the agent does not run its failed attempt again (the
    budget holds), and the rest of the backoff still passes before the next attempt."""
    stamps: list[float] = []
    backend = FakeBackend({"a": lambda call: stamps.append(time.monotonic()) or ActivityError(error, "boom")})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": BACKOFF.replace("DO", do)}), backend=backend)
    await until(lambda: ((activity_rows(harness.store, run_id).get("s0") or {}).get("data") or {}).get(
        "backoff_until"), what="the backoff journaled")
    await manager.shutdown()
    assert harness.store.get_run(run_id)["status"] == "interrupted"

    row = await resume(harness, run_id, backend)

    assert (row["status"], row["output"], len(stamps)) == ("failed", error, 2), row
    assert stamps[1] - stamps[0] >= 0.85, "the resumed attempt did not wait out the rest of the backoff"


# ------------------------------------------------------------------ a finally left by the initial entry (§3.10)

async def test_a_finally_left_by_the_initial_entry_runs_before_the_target_s_do(harness):
    """c's initial choice leads out of c: c's finally runs before x's do -- and x's failing transition, which
    rolls back only what it did itself, does not lose it."""
    order: list[str] = []
    backend = FakeBackend({"x": lambda call: order.append("x") or "X",
                           "c/finally": lambda call: order.append("fin") or "F"})
    row = await run(harness, {"m.yaml": """\
stategraph: 1
id: m
context: {v: 0}
initial: c
states:
  c:
    finally: {agent: cleanup, task: bye}
    initial: pick
    states:
      pick:
        type: choice
        transitions:
          - target: x
            guard: "ctx.v == 0"
          - target: inner
            guard: else
      inner: {type: final}
    transitions: [{target: done}]
  x:
    do: {agent: w, task: x}
    transitions:
      - target: done
        effect: ctx.v = 1 / 0
      - trigger: error
        target: done
  done: {type: final}
"""}, backend)

    assert (row["status"], order) == ("succeeded", ["fin", "x"]), (row, order)


# ------------------------------------------------------------------ final states: output and ending (§3.6, §2.8)

ENDING = """\
stategraph: 1
id: m
python: m.py
finally: {tool: log_end, args: {reason: "{{ ending.reason }}", error: "{{ ending.error and ending.error['type'] }}"}}
initial: a
states:
  a:
    transitions: [{target: done}]
  done: {type: final, FINAL}
"""

ENDING_PY = """\
def pairs():
    return {(1, 2): "x"}
"""


@pytest.mark.parametrize("final,error", [
    ('output: "{{ pairs() }}"', "not_serialisable"),
    ("status: failed", "final"),
], ids=["output_not_json", "failed_final"])
async def test_a_final_that_fails_the_frame_ends_it_failed(harness, final, error):
    """A final output that is not JSON data fails the frame (not the engine); a failed final state ends the
    frame 'failed' too, not 'finished'. Either way the machine's finally sees why."""
    backend = FakeBackend({"m.finally": "ok"})
    row = await run(harness, {"m.yaml": ENDING.replace("FINAL", final), "m.py": ENDING_PY}, backend)

    assert (row["status"], row["error"]["type"], row["error"]["state"]) == ("failed", error, "done"), row
    assert [call["args"] for call in backend.calls] == [{"reason": "failed", "error": error}]


# ------------------------------------------------------------------ joins: the failed child of THIS join

NESTED_JOIN = """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
context: {b: null}
initial: p
states:
  p:
    do:
      parallel:
        x: {parallel: {y: {agent: w, task: y}}}
        z: {machine: sub}
    transitions:
      - target: done
      - trigger: error
        target: failed
        effect: ctx.b = error.branch
  done: {type: final}
  failed: {type: final, output: "{{ ctx.b }}"}
"""

NESTED_JOIN_SUB = """\
stategraph: 1
id: sub
finally: {agent: tidy, task: t}
initial: s
states:
  s:
    do: {agent: w, task: s}
    transitions: [{target: fin}]
  fin: {type: final}
"""


async def test_a_nested_join_s_branch_does_not_shadow_the_outer_one(harness):
    backend = FakeBackend({"p/z/s": held(asyncio.Event()), "p/z/sub.finally": "ok"})

    async def y_fails(call: dict) -> ActivityError:
        await until(lambda: backend.count("p/z/s") == 1, what="z started")
        return ActivityError("agent_failed", "boom")

    backend.handlers["p/x/y"] = y_fails
    row = await run(harness, {"m.yaml": NESTED_JOIN, "sub.yaml": NESTED_JOIN_SUB}, backend)

    assert (row["status"], row["output"]) == ("succeeded", "x"), row


async def test_a_replayed_nested_join_failure_names_the_outer_branch(harness):
    """The crash comes while the join ends z (its finally held): the resume replays x's recorded failure --
    journaled with the inner join's branch -- and must still name the outer one."""
    backend = FakeBackend({"p/z/s": held(asyncio.Event()), "p/z/sub.finally": held(asyncio.Event())})

    async def y_fails(call: dict) -> ActivityError:
        await until(lambda: backend.count("p/z/s") == 1, what="z started")
        return ActivityError("agent_failed", "boom")

    backend.handlers["p/x/y"] = y_fails
    files = {"m.yaml": NESTED_JOIN, "sub.yaml": NESTED_JOIN_SUB}
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend)
    await until(lambda: backend.count("p/z/sub.finally") == 1, what="z ending")
    await manager.shutdown()

    second = FakeBackend({"p/z/sub.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["output"], second.count("p/x/y")) == ("succeeded", "x", 0), row


# ------------------------------------------------------------------ submachine_failed names its cause

async def test_submachine_failed_names_the_cause_in_its_message_bounded(harness):
    files = {"sub.yaml": """\
stategraph: 1
id: sub
initial: a
states:
  a:
    do: {tool: store_merge}
    transitions: [{target: fin}]
  fin: {type: final}
""", "m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
context: {message: null}
initial: s
states:
  s:
    do: {machine: sub}
    transitions:
      - target: ok
      - trigger: error
        target: failed
        effect: ctx.message = error.message
  ok: {type: final}
  failed: {type: final, output: "{{ ctx.message }}"}
"""}
    backend = FakeBackend({"s/a": ActivityError("tool_failed", "store says no " + "x" * 5000)})
    row = await run(harness, files, backend)

    assert row["output"].startswith("sub ended in a (failed): tool_failed: store says no"), row
    assert len(row["output"]) < 600, len(row["output"])


# ------------------------------------------------------------------ resources: never opened reads as None (§2.8)

async def test_a_finally_reads_a_resource_whose_open_failed_as_none(harness):
    backend = FakeBackend({"resources/forum/open": ActivityError("interrupted", "was running"), "m.finally": "ok"})
    row = await run(harness, {"m.yaml": """\
stategraph: 1
id: m
resources:
  forum:
    open: {tool: forum_open}
    close: {tool: forum_close, args: {group: "{{ resources.forum }}"}}
finally: {tool: log_end, args: {forum: "{{ resources.forum }}", reason: "{{ ending.reason }}"}}
initial: a
states:
  a:
    transitions: [{target: done}]
  done: {type: final}
"""}, backend)

    assert (row["status"], row["error"]["type"]) == ("failed", "interrupted"), row
    assert [(call["path"], call["args"]) for call in backend.calls[1:]] == [
        ("m.finally", {"forum": None, "reason": "failed"})], "the finally failed, or a never-opened resource closed"


# ================================================================== review of this fix round (E1-E6)

# ------------------------------------------------------------------ E1: a stopped composite in its retry's backoff

BACKOFF_SUB = """\
stategraph: 1
id: sub
finally: {agent: tidy, task: t}
initial: a
states:
  a:
    do: {agent: w, task: a}
    transitions: [{target: fin}]
  fin: {type: final}
"""

BACKOFF_ROOT = """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
finally: {agent: rootfin, task: f}
initial: s
states:
  s:
    do: {machine: sub, retry: {attempts: 2, backoff: 30s}}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_a_composite_stopped_in_its_backoff_never_begins_the_next_attempt(harness):
    """Terminated in the backoff, crashed while the root's finally ran: the resume ends the run without a frame of
    attempt 2 -- whose finally would otherwise run live for an attempt that never began."""
    files = {"m.yaml": BACKOFF_ROOT, "sub.yaml": BACKOFF_SUB}
    backend = FakeBackend({"s/a": ActivityError("agent_failed", "boom"), "s/sub.finally": "tidied",
                           "m.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend)
    await until(lambda: ((activity_rows(harness.store, run_id).get("s0") or {}).get("data") or {}).get(
        "backoff_until"), what="the backoff journaled")
    manager.control(run_id, "terminate")
    await until(lambda: backend.count("m.finally") == 1, what="the root's finally in flight")
    await manager.shutdown()

    second = FakeBackend({"s/a": "ok", "s/sub.finally": "tidied again", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert (row["status"], [call["path"] for call in second.calls]) == ("cancelled", ["m.finally"]), row
    assert not [key for key in activity_rows(harness.store, run_id) if key.startswith("s0/a2/")]


# ------------------------------------------------------------------ E2: a journal from before failed finals failed

async def test_a_failed_final_journaled_as_finished_resumes_without_diverging(harness):
    files = {"m.yaml": """\
stategraph: 1
id: m
initial: a
finally: {agent: w, task: fin}
states:
  a:
    do: {agent: w, task: go}
    transitions:
      - target: bad
  bad: {type: final, status: failed}
"""}
    backend = FakeBackend({"a": "ok", "m.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend)
    await until(lambda: backend.count("m.finally") == 1, what="the finally in flight")
    await manager.shutdown()
    db = harness.store._db()  # as the engine wrote it before: reason finished, no error, keys end.finished.*
    for row in harness.store.rows(run_id):
        if row["kind"] == "trace" and row["status"] == "end":
            db.execute("UPDATE journal SET data = ? WHERE run_id = ? AND seq = ?",
                       (json.dumps({**row["data"], "reason": "finished", "error": None}), run_id, row["seq"]))
        if row["kind"] == "activity" and row["key"] == "end.failed.finally":
            db.execute("UPDATE journal SET key = 'end.finished.finally' WHERE run_id = ? AND seq = ?",
                       (run_id, row["seq"]))
    db.commit()

    second = FakeBackend({"m.finally": "done"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["error"]["type"], second.count("m.finally")) == ("failed", "final", 1), row
    assert "end.failed.finally" not in activity_rows(harness.store, run_id)


# ------------------------------------------------------------------ E3: the repair pattern spends its budget

async def test_the_documented_repair_loop_ends_in_its_failed_final_after_the_third_store_error(harness):
    """patterns.md §3 as the author reads it: two repairs, then the third refusal takes the failed final."""
    import re
    from pathlib import Path

    import plugins.stategraph

    patterns = (Path(plugins.stategraph.__file__).parent / "skills" / "stategraph-authoring" / "references"
                / "patterns.md").read_text(encoding="utf-8")
    [text] = [block for block in re.findall(r"```yaml\n(.*?)```", patterns, re.S) if "id: store_record" in block]
    refused = {"$error": {"type": "tool_failed", "message": "record refused"}}
    manager = harness.manager()
    run_id = await manager.start(runnable({"store_record.yaml": text}, "store_record.yaml"),
                                 params={"record_text": "{"}, mock_only=True,
                                 mocks={"save": {"$visits": [refused] * 3}, "repair": "{}"})
    row = await settle(manager, run_id)

    assert (row["final_state"], row["output"]) == ("failed", {"problem": "record refused"}), row
    assert sum(1 for r in activity_rows(harness.store, run_id).values() if r["data"]["path"] == "repair") == 2


# ------------------------------------------------------------------ E4: a join nested in the other kind of join

@pytest.mark.parametrize("do,second,seen", [
    ('{parallel: {x: {map: "[1, 2]", each: {agent: w, task: go}}}}', "p/x/1", ["x", None]),
    ('{map: "[1, 2]", each: {parallel: {x: {agent: w, task: go}}}}', "p/1/x", [None, 1]),
], ids=["map_in_parallel", "parallel_in_map"])
async def test_a_nested_join_of_the_other_kind_leaves_no_name_of_its_own(harness, do, second, seen):
    """The second item (or its branch) fails: the outer join names its own child and drops the inner one's."""
    first = second.replace("/1", "/0")
    backend = FakeBackend({first: "ok", second: ActivityError("agent_failed", "boom")})
    row = await run(harness, {"m.yaml": f"""\
stategraph: 1
id: m
context: {{seen: null}}
initial: p
states:
  p:
    do: {do}
    transitions:
      - target: done
      - trigger: error
        target: failed
        effect: ctx.seen = [error.branch, error.index]
  done: {{type: final}}
  failed: {{type: final, output: "{{{{ ctx.seen }}}}"}}
"""}, backend)

    assert (row["status"], row["output"]) == ("succeeded", seen), row


# ------------------------------------------------------------------ E5: decide by an agent, options as JSON

async def test_an_agent_decides_among_options_as_json_strings_like_a_decision_model(harness):
    """Options a template made (a YAML key is a string already): 2 and True are "2" and "true", as in the JSON a
    decision model gets -- the same guard holds on both paths."""
    backend = FakeBackend({"judge": '{"decision": "2"}'})
    row = await run(harness, {"m.yaml": """\
stategraph: 1
id: m
initial: judge
states:
  judge:
    do:
      decide: choice
      by: critic
      question: How many scenes?
      criteria: "{{ {2: 'two scenes', 3: 'three scenes', True: 'all of them'} }}"
      input: The text.
    transitions:
      - target: done
        guard: out["value"] == "2"
      - target: other
        guard: else
  done: {type: final}
  other: {type: final, status: failed}
"""}, backend)

    assert (row["status"], row["final_state"]) == ("succeeded", "done"), row
    assert '"2": two scenes' in backend.calls[0]["task"] and '"true": all of them' in backend.calls[0]["task"]


# ------------------------------------------------------------------ E6: sync calls have a pool of their own

async def test_a_sync_call_runs_in_the_call_pool_with_the_run_s_context(harness):
    row = await run(harness, {"m.yaml": CALLS.replace("DO", "{call: where}"), "m.py": CALLS_PY + """

def where():
    from agent_system.tools.status import current_request_id

    return {"thread": threading.current_thread().name.split("_")[0], "request": current_request_id.get()}
"""})

    assert row["output"] == {"thread": "stategraph-call", "request": row["id"]}, row
