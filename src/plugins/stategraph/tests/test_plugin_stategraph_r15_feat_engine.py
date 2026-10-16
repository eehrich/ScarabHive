"""Engine features and leftovers of the review of 2026-10-15 (docs/review_2026-10-15.md): API-6, DBG-1, DBG-6,
FEAT-6, FEAT-10, FEAT-12.

Every machine runs through the real loader, validator and RunManager on a RunStore under tmp_path; only the
boundary behind the engine is FakeBackend. A "crash" is a real ``RunManager.shutdown()`` and a resume is a NEW
manager on the same store.
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.stategraph.engine.interpreter import bind_params
from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.model.spec import ParamSpec
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, errors, found, held,
                                                         resume, runnable, settle, until, validate)

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


def step_rows(harness: Harness, run_id: str) -> list[dict]:
    return [row["data"] for row in harness.store.rows(run_id, kinds=("trace",)) if row["status"] == "step"
            and "ctx_hash" in row["data"]]


# ------------------------------------------------------------------ API-6: a null param is no value

PARAMS = {
    "name": ParamSpec(type="string", default="world"),
    "who": ParamSpec(type="string", required=True),
    "tone": ParamSpec(type="string", enum=["warm", "dry"]),
    "anything": ParamSpec(type="any", required=True),
}


def test_api6_a_null_param_keeps_the_default_and_counts_as_missing():
    bound = bind_params(PARAMS, {"name": None, "who": "me", "anything": 0}, "m")
    assert bound == {"name": "world", "who": "me", "tone": None, "anything": 0}

    with pytest.raises(ActivityError, match="requires parameter 'who'"):
        bind_params(PARAMS, {"who": None, "anything": 1}, "m")
    with pytest.raises(ActivityError, match="requires parameter 'anything'"):  # as the validator judges it too
        bind_params(PARAMS, {"who": "me", "anything": None}, "m")
    with pytest.raises(ActivityError, match="tone: null is not one of"):
        bind_params(PARAMS, {"who": "me", "tone": None, "anything": 1}, "m")
    with pytest.raises(ActivityError, match="who: 3 is not a string"):
        bind_params(PARAMS, {"who": 3, "anything": 1}, "m")


# ------------------------------------------------------------------ DBG-1: the location reaches the error

LOCATED = """\
stategraph: 1
id: m
python: m.py
context: {a: {}, where: null}
initial: one
states:
  one:
    transitions:
      - {guard: 'False', target: two}
      - target: two
        effect: |
          x = 1
          ctx.b = ctx.a["nope"]
      - trigger: error
        target: caught
        effect: ctx.where = error.where
  two: {type: final}
  caught: {type: final, output: "{{ ctx.where }}"}
"""


async def test_dbg1_machine_code_reads_error_where(harness):
    row = await run(harness, {"m.yaml": LOCATED, "m.py": ""})
    assert (row["status"], row["output"]) == ("succeeded", "one.transitions[1].effect"), row.get("error")


async def test_dbg1_an_unhandled_error_names_file_line_and_code(harness):
    files = {"m.yaml": LOCATED.replace("      - trigger: error\n        target: caught\n        effect: ctx.where = "
                                       "error.where\n", ""), "m.py": ""}
    row = await run(harness, files)
    error = row["error"]
    assert error["type"] == "action_failed", error
    assert (error["where"], error["line"], error["code"]) == (
        "one.transitions[1].effect", 2, 'ctx.b = ctx.a["nope"]'), error
    assert error["message"] == "KeyError: 'nope'"  # unchanged: the journal hashes it


GUARD_RAISES = """\
stategraph: 1
id: m
context: {a: {}, line: null}
initial: one
states:
  one:
    transitions:
      - guard: ctx.a["nope"] > 1
        target: two
      - trigger: error
        target: two
        effect: ctx.line = error["line"]
  two:
    do: {tool: wait, idempotent: true}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.line }}"}
"""


async def test_dbg1_a_replayed_guard_error_keeps_its_location(harness):
    """The guard's error replays from its recorded guards: with the line it had, or ctx.line diverges on resume."""
    backend = FakeBackend({"two": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": GUARD_RAISES}), backend=backend)
    await until(lambda: backend.count("two") == 1, what="two in flight")
    await manager.shutdown()

    row = await resume(harness, run_id, FakeBackend({"two": "ok"}))
    assert (row["status"], row["output"]) == ("succeeded", 1), row.get("error")


# ------------------------------------------------------------------ DBG-6: the step row says what changed

CHANGES = """\
stategraph: 1
id: m
context: {round: 0, big: "", tmp: 1}
initial: one
states:
  one:
    transitions:
      - target: two
        effect: |
          ctx.round = 1
          ctx.big = "x" * 3000
  two:
    transitions:
      - target: done
        effect: |
          ctx.round = 2
          del ctx["tmp"]
  done: {type: final}
"""


async def test_dbg6_each_step_row_records_the_ctx_keys_it_changed(harness):
    row = await run(harness, {"m.yaml": CHANGES})
    assert row["status"] == "succeeded", row.get("error")
    first, second = step_rows(harness, row["id"])

    assert first["changed"]["round"] == 1
    big = first["changed"]["big"]
    assert big["$size"] == 3002 and big["$hash"] == first["keys"]["big"], big  # 3000 x and the JSON quotes
    assert "tmp" not in first["changed"]
    assert second["changed"] == {"round": 2, "tmp": {"$gone": True}}, second


# ------------------------------------------------------------------ FEAT-6: run.cost and limits.max_cost

BUDGET = """\
stategraph: 1
id: m
python: m.py
limits: {max_cost: 1.0}
context: {seen: null}
initial: a
states:
  a:
    do: {call: spend}
    transitions: [{target: b}]
  b:
    do: {call: spend}
    transitions:
      - target: c
        effect: ctx.seen = run.cost
  c:
    do: {call: spend}
    transitions:
      - target: over
      - trigger: error
        guard: error.type == "budget_exceeded"
        target: stopped
  over: {type: final}
  stopped: {type: final, output: {seen: "{{ ctx.seen }}", cost: "{{ run.cost }}"}}
"""

BUDGET_PY = """\
from plugins.stategraph.engine.backend import CALL_SPEND

calls = []


def spend():
    calls.append(1)
    CALL_SPEND.get().cost += 0.6
    return len(calls)
"""


async def test_feat6_a_spending_activity_past_max_cost_fails_budget_exceeded(harness):
    row = await run(harness, {"m.yaml": BUDGET, "m.py": BUDGET_PY})

    assert (row["status"], row["final_state"]) == ("succeeded", "stopped"), row.get("error")
    assert row["output"] == {"seen": 1.2, "cost": 1.2}, row["output"]
    c = next(r for r in harness.store.rows(row["id"], kinds=("activity",)) if r["key"] == "s2")
    assert c["data"]["error"]["type"] == "budget_exceeded", c["data"]  # journaled: a replay reads the same


BUDGET_HELD = BUDGET.replace("limits: {max_cost: 1.0}", "limits: {max_cost: 5.0}").replace(
    "  c:\n    do: {call: spend}", "  c:\n    do: {tool: wait, idempotent: true}")


async def test_feat6_run_cost_replays_to_the_same_value_after_a_crash(harness):
    """ctx.seen = run.cost ran before the crash; its step hash only matches on resume when the replayed outcomes
    count their cost again."""
    files = {"m.yaml": BUDGET_HELD, "m.py": BUDGET_PY}
    backend = FakeBackend({"c": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend)
    await until(lambda: backend.count("c") == 1, what="c in flight")
    await manager.shutdown()

    resumed = FakeBackend({"c": "ok"})
    row = await resume(harness, run_id, resumed)
    assert (row["status"], row["final_state"]) == ("succeeded", "over"), row.get("error")


# ------------------------------------------------------------------ FEAT-10: bounds from params

LOOP = """\
stategraph: 1
id: m
params: {rounds: {type: integer, default: 2}}
context: {n: 0}
initial: work
states:
  work:
    max_visits: "{{ params.rounds }}"
    transitions:
      - target: work
        effect: ctx.n += 1
      - trigger: error
        target: done
  done: {type: final, output: "{{ ctx.n }}"}
"""


async def test_feat10_max_visits_from_a_param(harness):
    row = await run(harness, {"m.yaml": LOOP}, params={"rounds": 3})
    assert (row["status"], row["output"]) == ("succeeded", 3), row.get("error")


def test_feat10_a_param_driven_max_visits_bounds_the_loop_and_reads_params_only():
    assert not found(validate({"m.yaml": LOOP}), "SG103")
    assert found(validate({"m.yaml": LOOP.replace('max_visits: "{{ params.rounds }}"', "")}), "SG103")

    bad = validate({"m.yaml": LOOP.replace("params.rounds }}", "ctx.n }}")})
    assert [p.code for p in errors(bad)] == ["SG004"], [p.as_dict() for p in bad.problems]


TIMED = """\
stategraph: 1
id: m
params: {wait: {type: number, default: 30}, slow: {type: string, default: 30s}}
events: {go: {}}
initial: call
states:
  call:
    do: {tool: slow, timeout: "{{ params.slow }}"}
    transitions:
      - target: hold
      - trigger: error
        target: hold
        effect: ctx.first = error.type
  hold:
    timeout: "{{ params.wait }}"
    transitions:
      - {trigger: go, target: done}
      - {trigger: error, target: done, effect: ctx.second = error.type}
  done: {type: final, output: {first: "{{ ctx.first }}", second: "{{ ctx.second }}"}}
"""


async def test_feat10_wait_and_activity_timeouts_from_params(harness):
    backend = FakeBackend({"call": held(asyncio.Event())})
    row = await run(harness, {"m.yaml": TIMED.replace("initial: call", "context: {first: null, second: null}\n"
                                                                      "initial: call")},
                    backend, params={"wait": 0.05, "slow": "50ms"})
    assert row["output"] == {"first": "timeout", "second": "wait_timeout"}, (row["output"], row.get("error"))


# ------------------------------------------------------------------ FEAT-12: the original exception, in process

RAISES = """\
stategraph: 1
id: m
python: m.py
imports: {sub: ./sub.yaml}
initial: first
states:
  first:
    do: {call: boom}
    transitions:
      - target: inner
      - trigger: error
        target: inner
  inner:
    do: {machine: sub, params: {}}
    transitions: [{target: done}]
  done: {type: final}
"""

SUB = """\
stategraph: 1
id: sub
python: m.py
initial: go
states:
  go:
    do: {call: rejected}
    transitions: [{target: done}]
  done: {type: final}
"""

RAISES_PY = """\
class Rejected(Exception):
    pass


def boom():
    raise RuntimeError("handled by the machine")


def rejected():
    raise Rejected("the design gate said no")
"""


async def test_feat12_the_exception_that_ended_the_run_is_kept_in_process(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": RAISES, "sub.yaml": SUB, "m.py": RAISES_PY}))
    live = manager.live[run_id]
    row = await settle(manager, run_id)

    assert row["status"] == "failed" and row["error"]["type"] == "submachine_failed", row["error"]
    exc = live.ctx.exception
    # the one that ended the run -- through the submachine -- not the first one, which an error transition handled
    assert type(exc).__name__ == "Rejected" and str(exc) == "the design gate said no", repr(exc)


# ------------------------------------------------------------------ fix round: R1, R2, R4, R5, R6, R7

RETRY = """\
stategraph: 1
id: m
python: m.py
limits: {max_cost: 1.0}
initial: a
states:
  a:
    do: {call: spend, retry: {attempts: 4, backoff: 0, errors: [call_failed]}}
    transitions: [{target: done}, {trigger: error, target: done}]
  done: {type: final, output: "{{ run.cost }}"}
"""

RETRY_PY = """\
from plugins.stategraph.engine.backend import CALL_SPEND


def spend():
    CALL_SPEND.get().cost += 0.6
    raise RuntimeError("again")
"""


async def test_r1_no_retry_attempt_starts_past_max_cost(harness):
    row = await run(harness, {"m.yaml": RETRY, "m.py": RETRY_PY})

    assert (row["status"], row["output"]) == ("succeeded", 1.2), row.get("error")
    a = next(r["data"] for r in harness.store.rows(row["id"], kinds=("activity",)) if r["status"] == "error")
    assert a["error"]["type"] == "budget_exceeded" and a["meta"]["attempts"] == 2, a


OPTIONAL_LOOP = LOOP.replace("{rounds: {type: integer, default: 2}}", "{rounds: {type: integer}}").replace(
    "states:\n", "limits: {max_steps: 40}\nstates:\n", 1)


async def test_r2_a_max_visits_expression_that_gives_nothing_is_refused_not_unbounded(harness):
    row = await run(harness, {"m.yaml": OPTIONAL_LOOP})

    assert row["status"] == "failed" and row["error"]["type"] == "template_failed", row.get("error")
    assert "gave None" in row["error"]["message"], row["error"]


SPEND_FAIL = """\
stategraph: 1
id: m
python: m.py
imports: {sub: ./sub.yaml}
context: {c: null}
initial: j
states:
  j:
    do:
      parallel:
        a: {call: spend_and_fail}
        b: {machine: sub}
      fail: fast
    transitions:
      - target: done
      - trigger: error
        target: done
        effect: ctx.c = run.cost
  done: {type: final, output: "{{ ctx.c }}"}
"""

SPEND_SUB = """\
stategraph: 1
id: sub
initial: w
states:
  w:
    do: {tool: wait, idempotent: true}
    finally: {tool: fin, idempotent: true}
    transitions: [{target: end}]
  end: {type: final}
"""

SPEND_FAIL_PY = """\
from plugins.stategraph.engine.backend import CALL_SPEND


def spend_and_fail():
    CALL_SPEND.get().cost += 0.5
    raise RuntimeError("no")
"""


async def test_r4_a_join_that_ends_on_a_recorded_child_error_counts_its_cost(harness):
    """Crash while the cancelled sibling runs its finally: the fail-fast child's error row is journaled, the
    parallel's is not. The resume ends the join from that row without replaying the child; its 0.5 still counts."""
    files = {"m.yaml": SPEND_FAIL, "sub.yaml": SPEND_SUB, "m.py": SPEND_FAIL_PY}
    backend = FakeBackend({"j/b/w": held(asyncio.Event()), "j/b/w/finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend)
    await until(lambda: backend.count("j/b/w/finally") == 1, what="b's finally in flight")
    await manager.shutdown()

    row = await resume(harness, run_id, FakeBackend({"j/b/w": "ok", "j/b/w/finally": "ok"}))
    assert (row["status"], row["output"]) == ("succeeded", 0.5), row.get("error")


def test_r5_the_validator_refuses_a_fixed_null_for_an_enum_param_as_bind_params_does():
    from plugins.stategraph.model.validate import agent_params_problems

    declared = {"task": ParamSpec(type="string"), "tone": ParamSpec(type="string", enum=["warm", "dry"])}
    assert agent_params_problems("m", declared, {"params": {"tone": None}}) == [
        "params.tone: null is not one of ['warm', 'dry']"]
    declared["tone"] = ParamSpec(type="string", enum=["warm", "dry"], default="warm")  # the default applies
    assert agent_params_problems("m", declared, {"params": {"tone": None}}) == []


def test_r6_a_submachine_max_cost_is_ignored_and_says_so():
    sub = "stategraph: 1\nid: sub\nlimits: {max_cost: 0.5}\ninitial: end\nstates:\n  end: {type: final}\n"
    main = ("stategraph: 1\nid: m\nimports: {sub: ./sub.yaml}\ninitial: a\nstates:\n"
            "  a:\n    do: {machine: sub}\n    transitions: [{target: end}]\n  end: {type: final}\n")
    tree = validate({"m.yaml": main, "sub.yaml": sub})
    assert [(p.code, p.path) for p in tree.problems if p.code == "SG108"] == [("SG108", "limits.max_cost")], [
        p.as_dict() for p in tree.problems]


def test_r7_the_schema_keeps_max_visits_constraints():
    from plugins.stategraph.model.schema_gen import machine_schema

    shapes = machine_schema()["$defs"]["StateSpec"]["properties"]["max_visits"]["anyOf"]
    assert {"type": "integer", "minimum": 1} in shapes and {"type": "string", "pattern": r"\{\{"} in shapes, shapes
