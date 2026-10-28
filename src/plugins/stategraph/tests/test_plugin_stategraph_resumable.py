"""Resumable failures (docs/stategraph_design.md §5.3): an ActivityError raised with resumable=True.

No error transition takes it and no retry: it stops the run as failed, through submachines and fan-outs, the frames'
finally activities run. Its resume sets aside what the failure decided and runs that activity again, and the siblings
it cancelled; the steps before it and a fan-out's children that ended replay from the journal. Every machine runs
through the real
RunManager on a RunStore under tmp_path; only the boundary behind the engine is FakeBackend, which counts the calls.
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.stategraph.engine.runner import NotRewindable, rewound_rows
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, FakeBackend, Harness, runnable, settle

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

# a call that asks a tool whether the budget is there; refused, it stops the run -- resumable
PY = """\
async def work(sg, n=0):
    got = await sg.tool("budget", {"n": n})
    if got == "full":
        raise sg.Error("call_failed", f"item {n}: the budget is used up", data={"n": n}, resumable=True)
    return f"done {n}"


async def ending(sg, reason):
    return await sg.tool("note", {"reason": reason})


async def slow(sg, n=0):
    return f"slow {n} " + await sg.tool("slowtool", {"n": n})


async def opener(sg):
    return await sg.tool("open", {})


async def closer(sg):
    return await sg.tool("close", {})
"""

FLAT = """\
stategraph: 1
id: m
python: m.py
context: {first: null, second: null, handled: false}
finally:
  call: ending
  args: {reason: "{{ ending.reason }}"}
initial: first
states:
  first:
    do: {call: work, args: {n: 1}}
    transitions:
      - target: second
        effect: ctx.first = out
  second:
    do: {call: work, args: {n: 2}, retry: {attempts: 3, backoff: 0s}}
    transitions:
      - target: done
        effect: ctx.second = out
      - trigger: error
        target: handled
        effect: ctx.handled = True
  done: {type: final, output: "{{ [ctx.first, ctx.second] }}"}
  handled: {type: final, status: failed, output: handled}
"""

FAN = """\
stategraph: 1
id: m
python: m.py
context: {outs: null}
initial: each
states:
  each:
    do:
      map: "[0, 1, 2, 3, 4]"
      as: n
      concurrency: 5
      fail: collect
      each: {call: work, args: {n: "{{ n }}"}}
    transitions:
      - target: done
        effect: ctx.outs = [r["out"] for r in out]
      - trigger: error
        target: handled
  done: {type: final, output: "{{ ctx.outs }}"}
  handled: {type: final, status: failed}
"""

PARENT = """\
stategraph: 1
id: m
python: m.py
imports: {child: ./c.yaml}
context: {got: null}
initial: before
states:
  before:
    do: {call: work, args: {n: 1}}
    transitions:
      - target: sub
  sub:
    do: {machine: child, params: {}}
    transitions:
      - target: done
        effect: ctx.got = out
      - trigger: error
        target: handled
  done: {type: final, output: "{{ ctx.got }}"}
  handled: {type: final, status: failed}
"""
CHILD = """\
stategraph: 1
id: c
python: m.py
context: {got: null}
initial: inner
states:
  inner:
    do: {call: work, args: {n: 7}}
    transitions:
      - target: fine
        effect: ctx.got = out
      - trigger: error
        target: caught
  fine: {type: final, output: "{{ ctx.got }}"}
  caught: {type: final, status: failed}
"""


def budget(full: set[int]):
    """The tool's answer: "full" for the items in `full`, else "ok"."""
    return lambda call: "full" if call["args"]["n"] in full else "ok"


async def stop_and_resume(tmp_path, files: dict[str, str], full: set[int], extra: dict | None = None):
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        before = FakeBackend({**(extra or {}), **{f"{p}/budget": budget(full) for p in PATHS}})
        run_id = await manager.start(runnable(files), backend=before)
        row = await settle(manager, run_id)
        after = FakeBackend({**(extra or {}), **{f"{p}/budget": budget(set()) for p in PATHS}})
        await harness.manager().resume(run_id, backend=after)
        resumed = await settle(harness.managers[-1], run_id)
        journal = harness.store.rows(run_id)
    finally:
        await harness.close()
    return row, resumed, before, after, journal


PATHS = ("first", "second", "before", "sub/inner", *(f"each/{i}" for i in range(5)))
NOTE = {"m.finally/note": "noted"}


def tools(backend, name="budget"):
    return [c["path"] for c in backend.calls if c["kind"] == "call_tool" and c["tool"] == name]


async def test_no_error_transition_and_no_retry_take_it_and_a_resume_runs_just_that_step_again(tmp_path):
    row, resumed, before, after, journal = await stop_and_resume(tmp_path, {"m.yaml": FLAT, "m.py": PY}, {2}, NOTE)
    assert row["status"] == "failed" and row["final_state"] == "second", "it stopped there, no final reached"
    assert row["error"]["resumable"] is True and row["error"]["message"] == "item 2: the budget is used up", row
    assert tools(before) == ["first/budget", "second/budget"], "tried once: no retry, no error transition"
    assert tools(before, "note") == ["m.finally/note"] and before.calls[-1]["args"] == {"reason": "failed"}, \
        "the run's finally ran"
    assert resumed["status"] == "succeeded" and resumed["output"] == ["done 1", "done 2"], resumed
    assert tools(after) == ["second/budget"], "the first step replayed, the stopped one ran again"
    assert tools(after, "note") == ["m.finally/note"], "the finally runs again at the real end"
    assert any(r["key"].startswith("rewound.1/") for r in journal) and any(
        r["kind"] == "trace" and r["status"] == "rewound" for r in journal), "set aside, kept"


async def test_a_fan_out_lets_its_other_items_end_and_a_resume_runs_the_stopped_items_alone(tmp_path):
    """fail: collect -- items 1 and 3 refused: the others run to their end, the join stops the run; resumed, items 1
    and 3 alone run again, the other three replay."""
    row, resumed, before, after, journal = await stop_and_resume(tmp_path, {"m.yaml": FAN, "m.py": PY}, {1, 3})
    assert row["status"] == "failed" and row["error"]["resumable"], row
    assert sorted(tools(before)) == [f"each/{i}/budget" for i in range(5)], "every item ran to its end"
    assert resumed["status"] == "succeeded" and resumed["output"] == [f"done {i}" for i in range(5)], resumed
    assert sorted(tools(after)) == ["each/1/budget", "each/3/budget"], tools(after)


async def test_a_submachine_stopped_inside_stops_its_caller_and_resumes_inside(tmp_path):
    """The child's call refused: neither the child's error transition nor the parent's takes it; the run says the
    child's words. Resumed, the child's call alone runs again."""
    files = {"m.yaml": PARENT, "c.yaml": CHILD, "m.py": PY}
    row, resumed, before, after, journal = await stop_and_resume(tmp_path, files, {7})
    assert row["status"] == "failed" and row["error"]["resumable"], row
    assert row["error"]["message"] == "item 7: the budget is used up", row["error"]
    assert resumed["status"] == "succeeded" and resumed["output"] == "done 7", resumed
    assert tools(after) == ["sub/inner/budget"], "the parent's step before replayed"


async def test_a_resume_too_early_stops_again_at_the_same_step(tmp_path):
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        run_id = await manager.start(runnable({"m.yaml": FLAT, "m.py": PY}),
                                     backend=FakeBackend({**NOTE, **{f"{p}/budget": budget({2}) for p in PATHS}}))
        await settle(manager, run_id)
        again = FakeBackend({**NOTE, **{f"{p}/budget": budget({2}) for p in PATHS}})
        await harness.manager().resume(run_id, backend=again)
        row = await settle(harness.managers[-1], run_id)
        later = FakeBackend({**NOTE, **{f"{p}/budget": budget(set()) for p in PATHS}})
        await harness.manager().resume(run_id, backend=later)
        done = await settle(harness.managers[-1], run_id)
        journal = harness.store.rows(run_id)
    finally:
        await harness.close()
    assert row["status"] == "failed" and tools(again) == ["second/budget"], tools(again)
    assert done["status"] == "succeeded" and tools(later) == ["second/budget"], tools(later)
    assert {r["key"].split("/", 1)[0] for r in journal if r["key"].startswith("rewound.")} == {"rewound.1", "rewound.2"}


def test_what_a_resume_sets_aside():
    """The stopped activity and what ran below it, the composites it was in, and in each frame on its path what came
    after it; a sibling the failure cancelled (started, no outcome) the same way. Not the steps before, nor a join's
    other children, nor edits; nor a refusal that ended nothing: a branch of a join that succeeded, a finally's."""
    error = {"error": {"type": "call_failed", "resumable": True}}
    rows = [{"kind": k, "key": key, "status": s, "data": d} for k, key, s, d in (
        ("activity", "s1", "done", {}),
        ("activity", "s1/b.x", "error", error),                                               # outvoted
        ("activity", "s1.fin.first", "error", error),                                         # a finally's
        ("trace", "s1:step", "step", {}),
        ("activity", "s2", "error", {"error": {"type": "call_failed", "resumable": True}}),   # the map
        ("activity", "s2/i.0", "done", {}),
        ("activity", "s2/i.1", "error", {"error": {"type": "submachine_failed", "resumable": True}}),
        ("activity", "s2/i.1/m/s1", "done", {}),                                              # before, in the child
        ("activity", "s2/i.1/m/s2", "error", error),                                          # the stopped one
        ("activity", "s2/i.1/m/s2/t.0", "done", {}),
        ("trace", "s2/i.1/m/end", "end", {}),
        ("activity", "s2/i.1/m/end.failed.finally", "done", {}),
        ("activity", "s2/i.2/m/s2", "done", {}),                                              # another item's
        ("trace", "s2/i.2/m/end", "end", {}),
        ("activity", "s2/i.3", "started", {}),                                                # cancelled
        ("activity", "s2/i.3/m/s1", "done", {}),
        ("activity", "s2/i.3/m/s2", "started", {}),
        ("trace", "s2/i.3/m/end", "end", {}),
        ("trace", "s2:enter:each", "enter", {}),
        ("edit", "s2:enter:1", None, {"at": "s2:enter"}),
        ("trace", "end", "end", {}),
        ("activity", "end.failed.finally", "done", {}),
        ("trace", "request_seq", "request_seq", {}),
    )]
    assert [r["key"] for r in rewound_rows(rows)] == [
        "s2", "s2/i.1", "s2/i.1/m/s2", "s2/i.1/m/s2/t.0", "s2/i.1/m/end", "s2/i.1/m/end.failed.finally",
        "s2/i.3", "s2/i.3/m/s2", "s2/i.3/m/end", "s2:enter:each", "end", "end.failed.finally"]
    with pytest.raises(NotRewindable, match="no activity"):  # only refusals that ended nothing: nothing to run again
        rewound_rows([r for r in rows if not r["key"].startswith("s2")])


QUORUM = """\
stategraph: 1
id: m
python: m.py
initial: two
states:
  two:
    do:
      parallel:
        a: {call: work, args: {n: 1}}
        b: {call: work, args: {n: 2}}
        c: {call: work, args: {n: 3}}
      join: {count: 2}
    transitions:
      - target: done
      - trigger: error
        target: handled
  done: {type: final}
  handled: {type: final, status: failed}
"""


async def test_a_quorum_that_cannot_be_reached_for_a_resumable_failure_stops_on_it(tmp_path):
    """join: count -- two of three refused: not join_failed (which a transition would take), the refusal itself."""
    harness = Harness(tmp_path)
    try:
        backend = FakeBackend({f"two/{b}/budget": budget({2, 3}) for b in "abc"})
        row = await harness.run({"m.yaml": QUORUM, "m.py": PY}, backend=backend)
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["final_state"] == "two", row
    assert row["error"]["resumable"] and row["error"]["message"].endswith("the budget is used up"), row["error"]


def test_a_run_stopped_so_says_how_it_goes_on():
    """get_run's next: a failed run whose error is resumable is resumed; any other failed run has no next."""
    from plugins.stategraph.server import _summary
    row = {"status": "failed", "error": {"type": "call_failed", "message": "budget", "resumable": True}}
    assert "action='resume'" in _summary(row)["next"], _summary(row)
    assert "next" not in _summary({"status": "failed", "error": {"type": "call_failed", "message": "x"}})


async def test_a_mocked_run_resumed_gets_the_mock_s_next_answer(tmp_path):
    """A test of a machine with mocks: the refused visit counts ($visits), so the resume gets the next answer -- the
    window has reset -- and goes on, instead of the first answer again."""
    mocks = {"first/budget": "ok", "second/budget": {"$visits": ["full", "ok"]}, "m.finally/note": "noted"}
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        run_id = await manager.start(runnable({"m.yaml": FLAT, "m.py": PY}), mocks=mocks, mock_only=True)
        row = await settle(manager, run_id)
        await harness.manager().resume(run_id)
        resumed = await settle(harness.managers[-1], run_id)
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["resumable"], row
    assert resumed["status"] == "succeeded" and resumed["output"] == ["done 1", "done 2"], resumed


FAST = """\
stategraph: 1
id: m
python: m.py
context: {out: null}
initial: two
states:
  two:
    do:
      parallel:
        a: {call: work, args: {n: 1}}
        b: {call: slow, args: {n: 2}}
    transitions:
      - target: done
        effect: ctx.out = out
      - trigger: error
        target: handled
  done: {type: final, output: "{{ ctx.out }}"}
  handled: {type: final, status: failed}
"""


async def _held(call):
    await asyncio.sleep(3600)


async def test_a_sibling_the_refusal_cancelled_runs_again_too(tmp_path):
    """fail: fast -- branch a refused, branch b cancelled in flight (its started row, no outcome): resumed, both run
    again; b is not taken for one that crashed in flight (interrupted, not idempotent)."""
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        before = FakeBackend({"two/a/budget": budget({1}), "two/b/slowtool": _held})
        run_id = await manager.start(runnable({"m.yaml": FAST, "m.py": PY}), backend=before)
        row = await settle(manager, run_id)
        after = FakeBackend({"two/a/budget": budget(set()), "two/b/slowtool": "fine"})
        await harness.manager().resume(run_id, backend=after)
        resumed = await settle(harness.managers[-1], run_id)
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["resumable"], row
    assert resumed["status"] == "succeeded" and resumed["output"] == {"a": "done 1", "b": "slow 2 fine"}, resumed
    assert sorted(c["path"] for c in after.calls) == ["two/a/budget", "two/b/slowtool"], after.calls


OUTVOTED = """\
stategraph: 1
id: m
python: m.py
initial: two
states:
  two:
    do:
      parallel:
        a: {call: work, args: {n: 1}}
        b: {call: work, args: {n: 2}}
      join: first
    transitions:
      - target: mid
  mid:
    do: {call: work, args: {n: 4}}
    transitions:
      - target: last
    finally: {call: work, args: {n: 5}}
  last:
    do: {call: work, args: {n: 3}}
    transitions:
      - target: done
  done: {type: final}
"""


async def _late(call):
    await asyncio.sleep(0.05)
    return "ok"


async def test_a_refusal_that_ended_nothing_is_not_run_again(tmp_path):
    """Branch a refused, but b won the join; mid's finally refused (swallowed): neither ended the run, so the stop
    at last runs last alone again -- not mid, not the join's branches, not the finally."""
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        before = FakeBackend({"two/a/budget": budget({1}), "two/b/budget": _late, "mid/budget": "ok",
                              "mid/finally/budget": "full", "last/budget": "full"})
        run_id = await manager.start(runnable({"m.yaml": OUTVOTED, "m.py": PY}), backend=before)
        row = await settle(manager, run_id)
        after = FakeBackend({p: "ok" for p in ("two/a/budget", "two/b/budget", "mid/budget", "mid/finally/budget",
                                                "last/budget")})
        await harness.manager().resume(run_id, backend=after)
        resumed = await settle(harness.managers[-1], run_id)
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["final_state"] == "last", row
    assert "mid/finally/budget" in tools(before), "the finally was refused"
    assert resumed["status"] == "succeeded", resumed
    assert tools(after) == ["last/budget"], tools(after)


FIRST = """\
stategraph: 1
id: m
python: m.py
initial: two
states:
  two:
    do:
      parallel:
        a: {call: work, args: {n: 1}}
        b: {call: work, args: {n: 2}}
      join: first
    transitions:
      - target: done
        effect: ctx.won = out
  done: {type: final, output: "{{ ctx.won }}"}
context: {won: null}
"""


async def test_a_join_counts_the_branches_that_ran_again_where_they_ended_now(tmp_path):
    """join: first -- both branches refused: the join could not be won. Resumed, both run again and the join sees
    each one end anew (a fresh joined row): a set-aside one is no place a branch ended at."""
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        run_id = await manager.start(runnable({"m.yaml": FIRST, "m.py": PY}),
                                     backend=FakeBackend({f"two/{b}/budget": "full" for b in "ab"}))
        row = await settle(manager, run_id)
        await harness.manager().resume(run_id, backend=FakeBackend({f"two/{b}/budget": "ok" for b in "ab"}))
        resumed = await settle(harness.managers[-1], run_id)
        joined = [r["key"] for r in harness.store.rows(run_id) if r["kind"] == "trace" and r["status"] == "joined"]
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["resumable"], row
    assert resumed["status"] == "succeeded", resumed
    assert sorted(k for k in joined if not k.startswith("rewound.")) == ["s0/b.a:joined", "s0/b.b:joined"], joined


WAITING = """\
stategraph: 1
id: w
python: m.py
initial: a
states:
  a:
    do: {call: work, args: {n: 5}}
    transitions:
      - target: pause
  pause:
    after: 1s
    transitions:
      - target: z
  z: {type: final}
"""
FAST_WAIT = """\
stategraph: 1
id: m
python: m.py
imports: {w: ./w.yaml}
initial: two
states:
  two:
    do:
      parallel:
        a: {machine: w, params: {}}
        b: {call: work, args: {n: 6}}
    transitions:
      - target: done
  done: {type: final}
"""


async def _late_full(call):
    await asyncio.sleep(0.1)  # branch a stands in its wait by then
    return "full"


async def test_a_cancelled_sibling_keeps_what_it_had_finished(tmp_path):
    """fail: fast -- branch a (a submachine) had finished its step and stood in a timer state when b was refused:
    resumed, a's step replays and its wait goes on; only b runs again."""
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        files = {"m.yaml": FAST_WAIT, "w.yaml": WAITING, "m.py": PY}
        before = FakeBackend({"two/a/a/budget": "ok", "two/b/budget": _late_full})
        run_id = await manager.start(runnable(files), backend=before)
        row = await settle(manager, run_id)
        after = FakeBackend({"two/a/a/budget": "ok", "two/b/budget": "ok"})
        await harness.manager().resume(run_id, backend=after)
        resumed = await settle(harness.managers[-1], run_id)
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["resumable"] and tools(before) == [
        "two/a/a/budget", "two/b/budget"], (row, tools(before))
    assert resumed["status"] == "succeeded", resumed
    assert tools(after) == ["two/b/budget"], tools(after)


RES_CHILD = """\
stategraph: 1
id: r
python: m.py
resources:
  res:
    open: {call: opener}
    close: {call: closer}
initial: a
states:
  a:
    do: {call: slow, args: {n: 3}}
    transitions:
      - target: z
  z: {type: final}
"""
FAST_RES = """\
stategraph: 1
id: m
python: m.py
imports: {r: ./r.yaml}
initial: two
states:
  two:
    do:
      parallel:
        a: {machine: r, params: {}}
        b: {call: work, args: {n: 6}}
    transitions:
      - target: done
  done: {type: final}
"""


@pytest.mark.parametrize("shape", ["in_flight", "waiting"])
async def test_a_cancelled_sibling_with_resources_is_not_resumed_either(tmp_path, shape):
    """Branch a, a frame with resources, cancelled when b was refused -- in flight, or standing in a wait (nothing
    started below it): its end closed them, the resume refuses as for the stopped one's frames."""
    child = RES_CHILD if shape == "in_flight" else RES_CHILD.replace(
        "    do: {call: slow, args: {n: 3}}\n    transitions:\n      - target: z\n",
        "    after: 1h\n    transitions:\n      - target: z\n")
    assert child.count("after: 1h") == (shape == "waiting")
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        files = {"m.yaml": FAST_RES, "r.yaml": child, "m.py": PY}
        run_id = await manager.start(runnable(files), backend=FakeBackend(
            {"two/a/resources/res/open/open": "R", "two/a/resources/res/close/close": "closed",
             "two/a/a/slowtool": _held, "two/b/budget": _late_full}))
        row = await settle(manager, run_id)
        with pytest.raises(NotRewindable, match=r"resources \(res\)"):
            await harness.manager().resume(run_id, backend=FakeBackend({}))
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["resumable"], row


@pytest.mark.parametrize("where", ["row", "launch"])
async def test_a_resume_that_failed_to_start_can_be_resumed_again(tmp_path, monkeypatch, where):
    """The rewind is done, then the resume fails before the run runs -- its row cannot be written, or its launch
    fails after the row said running: the run stays failed and resumable, and the next resume -- the journal already
    rewound -- runs the stopped step once."""
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        run_id = await manager.start(runnable({"m.yaml": FLAT, "m.py": PY}),
                                     backend=FakeBackend({**NOTE, **{f"{p}/budget": budget({2}) for p in PATHS}}))
        row = await settle(manager, run_id)
        real, failures = harness.store.update_run, []

        def locked_once(run, **fields):
            if fields.get("status") == "running" and not failures:
                failures.append(run)
                raise RuntimeError("database is locked")
            return real(run, **fields)
        failing = harness.manager()
        if where == "row":
            monkeypatch.setattr(harness.store, "update_run", locked_once)
        else:
            def no_launch(*args, **kwargs):
                failures.append(args[0])
                raise RuntimeError("no backend")
            monkeypatch.setattr(failing, "_launch", no_launch)
        with pytest.raises(RuntimeError):
            await failing.resume(run_id, backend=FakeBackend({}))
        monkeypatch.undo()
        between = harness.store.get_run(run_id)
        later = FakeBackend({**NOTE, **{f"{p}/budget": budget(set()) for p in PATHS}})
        await harness.manager().resume(run_id, backend=later)
        done = await settle(harness.managers[-1], run_id)
        rewinds = [r for r in harness.store.rows(run_id) if r["kind"] == "trace" and r["status"] == "rewound"]
    finally:
        await harness.close()
    assert row["status"] == "failed" and failures, failures
    assert between["status"] == "failed" and between["error"] == row["error"], between["error"]
    assert done["status"] == "succeeded" and tools(later) == ["second/budget"], (done.get("error"), tools(later))
    assert len(rewinds) == 1, "rewound once"


RES = """\
stategraph: 1
id: m
python: m.py
resources:
  res:
    open: {call: opener}
    close: {call: closer}
initial: first
states:
  first:
    do: {call: work, args: {n: 1}}
    transitions:
      - target: done
  done: {type: final}
"""


async def test_a_run_stopped_in_a_frame_with_resources_is_not_resumed(tmp_path):
    """Its failed end closed the resources and the steps before would replay with them closed: the resume refuses,
    says so, and leaves the run and its journal as they were."""
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        run_id = await manager.start(runnable({"m.yaml": RES, "m.py": PY}),
                                     backend=FakeBackend({"first/budget": "full", "resources/res/open/open": "R",
                                                          "resources/res/close/close": "closed"}))
        row = await settle(manager, run_id)
        keys = [r["key"] for r in harness.store.rows(run_id)]
        with pytest.raises(NotRewindable, match=r"resources \(res\).*fork it"):
            await harness.manager().resume(run_id, backend=FakeBackend({"first/budget": "ok"}))
        again = harness.store.get_run(run_id)
        kept = [r["key"] for r in harness.store.rows(run_id)]
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["resumable"], row
    assert again["status"] == "failed" and kept == keys, (again["status"], kept)


async def test_resources_without_a_close_do_not_refuse_the_resume(tmp_path):
    """Nothing closed them when the run failed: the resume goes on with the values it opened -- not opened again."""
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        first = FakeBackend({"first/budget": "full", "resources/res/open/open": "R"})
        unclosed = RES.replace("    close: {call: closer}\n", "")
        assert "close" not in unclosed, "fixture: the resource has no close"
        run_id = await manager.start(runnable({"m.yaml": unclosed, "m.py": PY}), backend=first)
        row = await settle(manager, run_id)
        later = FakeBackend({"first/budget": "ok", "resources/res/open/open": "R2"})
        resumer = harness.manager()
        await resumer.resume(run_id, backend=later)
        done = await settle(resumer, run_id)
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["resumable"], row
    assert done["status"] == "succeeded", done.get("error")
    assert [c["path"] for c in later.calls] == ["first/budget"], later.calls


def test_setting_aside_is_all_or_nothing(tmp_path):
    """One row it cannot move (gone) and none moves, no trace row either; a store that does not hold the run moves
    nothing."""
    from plugins.stategraph.engine.journal import RunStore
    store = RunStore(tmp_path / "runs.db")
    try:
        run_id = "r1"
        store.create_run(run_id, "m", {}, owner="me")
        store.record(run_id, "activity", "s1", status="done", data={})
        store.record(run_id, "activity", "s2", status="error", data={})
        with pytest.raises(ValueError, match="no journal row"):
            store.set_aside(run_id, [("activity", "s2"), ("activity", "s9")], "rewound.1", status="rewound",
                            data={}, fence="me")
        assert [r["key"] for r in store.rows(run_id)] == ["s1", "s2"]
        with pytest.raises(ValueError, match="not held"):
            store.set_aside(run_id, [("activity", "s2")], "rewound.1", status="rewound", data={}, fence="another")
        store.set_aside(run_id, [("activity", "s2")], "rewound.1", status="rewound", data={}, fence="me")
        assert [r["key"] for r in store.rows(run_id)] == ["s1", "rewound.1/s2", "rewound.1"]
    finally:
        store.close()


class _Everywhere(dict):
    """FakeBackend handlers: one function for every path."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def get(self, key, default=None):
        return self.fn


def everywhere(fn) -> FakeBackend:
    backend = FakeBackend()
    backend.handlers = _Everywhere(fn)
    return backend


PY_MORE = PY + """

async def note(sg, n=0):
    return await sg.tool("note", {"n": n})


async def flaky(sg, n=0):
    if await sg.tool("budget", {"n": n}) == "boom":
        raise sg.Error("call_failed", f"item {n}: boom")
    return f"done {n}"


async def pair(sg, n=0):
    import asyncio
    held = asyncio.ensure_future(sg.tool("slowtool", {"n": 7}))
    await asyncio.sleep(0.05)
    got = await sg.tool("budget", {"n": 8})
    if got == "full":
        raise sg.Error("call_failed", "item 8: the budget is used up", resumable=True)
    return [got, await held]
"""

RETRIED_CHILD = """\
stategraph: 1
id: c
python: m.py
finally: {call: note, args: {n: 99}}
initial: x
states:
  x:
    do: {call: flaky, args: {n: 10}}
    transitions:
      - target: hold
  hold:
    after: 1h
    transitions:
      - target: z
  z: {type: final}
"""
RETRIED = """\
stategraph: 1
id: m
python: m.py
imports: {c: ./c.yaml}
initial: two
states:
  two:
    do:
      parallel:
        a: {machine: c, params: {}, retry: {attempts: 2, backoff: 0s}}
        b: {call: work, args: {n: 6}}
    transitions:
      - target: done
  done: {type: final}
"""


async def test_a_cancelled_sibling_in_its_second_attempt_keeps_its_first(tmp_path):
    """Branch a (a submachine with retry) failed its first attempt -- whose frame ended, its finally ran -- and stood
    in a wait in its second when b was refused: the resume sets aside the second attempt's end, not the first's, so
    the first's finally does not run again."""
    tries = []

    async def first(call):
        n = call["args"].get("n")
        if n == 10:
            tries.append(n)
            return "boom" if len(tries) == 1 else "ok"
        if n == 6:
            await asyncio.sleep(0.3)
            return "full"
        return "noted" if call["tool"] == "note" else "ok"
    later = everywhere(lambda call: "noted" if call["tool"] == "note" else "ok")
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        before = everywhere(first)
        run_id = await manager.start(runnable({"m.yaml": RETRIED, "c.yaml": RETRIED_CHILD, "m.py": PY_MORE}),
                                     backend=before)
        row = await settle(manager, run_id)
        await harness.manager().resume(run_id, backend=later)
        await asyncio.sleep(0.5)
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["resumable"] and len(tries) == 2, (row, tries)
    assert [c for c in later.calls if c.get("tool") == "note"] == [], "the first attempt's finally ran again"
    assert [c["args"]["n"] for c in later.calls if c.get("tool") == "budget"] == [6], later.calls


GRAND = """\
stategraph: 1
id: g
python: m.py
resources:
  res:
    open: {call: opener}
    close: {call: closer}
initial: a
states:
  a:
    do: {call: work, args: {n: 1}}
    transitions:
      - target: z
  z: {type: final}
"""
MID = """\
stategraph: 1
id: c
python: m.py
imports: {g: ./g.yaml}
initial: x
states:
  x:
    do: {machine: g, params: {}}
    transitions:
      - target: hold
  hold:
    after: 1h
    transitions:
      - target: z
  z: {type: final}
"""
TOP = """\
stategraph: 1
id: m
python: m.py
imports: {c: ./c.yaml}
initial: two
states:
  two:
    do:
      parallel:
        a: {machine: c, params: {}}
        b: {call: work, args: {n: 6}}
    transitions:
      - target: done
  done: {type: final}
"""


async def test_resources_of_a_frame_that_finished_below_a_cancelled_sibling_do_not_refuse(tmp_path):
    """Branch a's finished step ran a submachine with resources (opened and closed, done) and then stood in a wait:
    the frame a had open has none -- the resume goes on."""
    async def first(call):
        if call["args"].get("n") == 6:
            await asyncio.sleep(0.3)
            return "full"
        return "R" if call["tool"] == "open" else "ok"
    later = everywhere(lambda call: "ok")
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        run_id = await manager.start(runnable({"m.yaml": TOP, "c.yaml": MID, "g.yaml": GRAND, "m.py": PY_MORE}),
                                     backend=everywhere(first))
        row = await settle(manager, run_id)
        await harness.manager().resume(run_id, backend=later)
        await asyncio.sleep(0.3)
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["resumable"], row
    assert [c["args"]["n"] for c in later.calls if c.get("tool") == "budget"] == [6], later.calls


TOOL_IN_FLIGHT = """\
stategraph: 1
id: m
python: m.py
context: {out: null}
initial: one
states:
  one:
    do:
      map: "[0, 1]"
      as: n
      fail: collect
      each: {call: pair}
    transitions:
      - target: done
        effect: ctx.out = out
  done: {type: final, output: "{{ ctx.out }}"}
"""


async def test_a_stopped_call_with_a_tool_still_in_flight_asks_again(tmp_path):
    """A map item's call refused while one of its own tools was still in flight (started, no outcome): that row is
    the stopped call's, set aside with it -- not a cancelled sibling's -- so the resume asks the budget again."""
    async def first(call):
        if call["tool"] == "slowtool":
            await asyncio.sleep(3600)
        return "full"
    later = everywhere(lambda call: "ok")
    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        run_id = await manager.start(runnable({"m.yaml": TOOL_IN_FLIGHT, "m.py": PY_MORE}), backend=everywhere(first))
        row = await settle(manager, run_id)
        await harness.manager().resume(run_id, backend=later)
        resumed = await settle(harness.managers[-1], run_id)
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["resumable"], row
    assert resumed["status"] == "succeeded" and [r["out"] for r in resumed["output"]] == [["ok", "ok"]] * 2, resumed
    assert sorted(c["tool"] for c in later.calls) == ["budget", "budget", "slowtool", "slowtool"], later.calls


CONTINUED = """\
stategraph: 1
id: m
context: {inst: null}
initial: a
states:
  a:
    do: {agent: worker, task: begin}
    transitions: [{target: b, effect: ctx.inst = activity.instance_id}]
  b:
    do: {agent: worker, continue: "{{ ctx.inst }}", task: go on}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_a_transient_failure_resumed_goes_on_in_the_agent_instance_an_earlier_step_made(tmp_path):
    """A transient failure (the same request again, service._by_key) resumes the run itself: its continue reaches
    the instance step a made -- a new run, or a fork with a session of its own, could not continue it."""
    from agent_system.config.models import AgentSystemConfig
    from plugins.stategraph.engine.backend import ScarabHiveBackend
    from plugins.stategraph.tests.stategraph_testkit import AgentHost, FakeAgent

    tries: list[str] = []

    def answer(call):
        tries.append(call["task"])
        return RuntimeError("provider hiccup") if tries.count("go on") == 1 else f"{call['task']}: ok"

    worker = FakeAgent("worker", answer)
    host = AgentHost(tmp_path / "s", worker)
    harness = Harness(tmp_path)
    backend = lambda rid: ScarabHiveBackend(runner=host, system_config=AgentSystemConfig(),  # noqa: E731
                                            session_id=f"sg_{rid}", user_id="ann")
    try:
        manager = harness.manager()
        run_id = await manager.start(runnable({"m.yaml": CONTINUED}), backend_factory=backend)
        row = await settle(manager, run_id)
        resumer = harness.manager()
        await resumer.resume(run_id, backend_factory=backend, transient=True)
        done = await settle(resumer, run_id)
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["type"] == "agent_failed", row["error"]
    assert done["status"] == "succeeded", done.get("error")
    assert tries == ["begin", "go on", "go on"], tries
    assert len({call["session"] for call in worker.calls}) == 1, "one instance, continued twice"


CAUGHT = """\
stategraph: 1
id: m
python: m.py
initial: a
states:
  a:
    do: {call: work, args: {n: 1}}
    transitions:
      - target: b
      - trigger: error
        target: b
  b:
    do: {call: work, args: {n: 2}}
    transitions: [{target: c}]
  c:
    do: {call: work, args: {n: 3}}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_a_transient_error_the_machine_caught_is_not_where_the_resume_starts(tmp_path):
    """Step a failed transiently and its error transition went on; step c failed the run. The resume runs c again --
    not a and b, which the run had finished."""
    from plugins.stategraph.kinds import ActivityError

    harness = Harness(tmp_path)
    try:
        manager = harness.manager()
        run_id = await manager.start(runnable({"m.yaml": CAUGHT, "m.py": PY}), backend=FakeBackend({
            "a/budget": ActivityError("agent_failed", "hiccup"), "b/budget": "ok",
            "c/budget": ActivityError("agent_failed", "hiccup")}))
        row = await settle(manager, run_id)
        later = FakeBackend({"a/budget": "ok", "b/budget": "ok", "c/budget": "ok"})
        resumer = harness.manager()
        await resumer.resume(run_id, backend=later, transient=True)
        done = await settle(resumer, run_id)
    finally:
        await harness.close()
    assert row["status"] == "failed" and row["error"]["type"] == "agent_failed", row["error"]
    assert done["status"] == "succeeded", done.get("error")
    assert [c["path"] for c in later.calls] == ["c/budget"], later.calls
