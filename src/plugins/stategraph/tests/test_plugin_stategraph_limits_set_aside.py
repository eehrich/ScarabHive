"""What no outcome row carries still counts against limits.max_output_tokens and limits.max_cost.

* an attempt a stop cut -- the branch a join: first lets go, in a loop -- spent its tokens: the cap counts them, live
  and after a resume (its ``started`` row)
* a resume after a failure sets the failed rows aside (REWOUND): what they spent does not come back as budget, or
  each resume refilled the cap
* a composite on its second attempt resumed after a crash: the child its first attempt's timeout cut counts, the
  child in flight at the crash runs again (idempotent) and what it carried is counted once
* what the run shows it has spent (RunManager.output_tokens, budget) is that same sum, live and from its journal
"""
from __future__ import annotations

import asyncio
import json

import pytest

from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.tests.stategraph_testkit import FakeBackend, Harness, activity_rows, runnable, settle, until


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


def _spend(act, tokens, cost=0.0):
    """What an agent call writes into its activity's meta as it goes (the backend's _Spend does so)."""
    meta = act.meta.setdefault("tokens", {"prompt": 0, "completion": 0, "cached": 0})
    meta["completion"] += tokens
    act.meta["cost"] = act.meta.get("cost", 0.0) + cost


RACE = {"m.yaml": """\
stategraph: 1
id: m
title: m
limits: {max_output_tokens: 3000, max_cost: 3.0}
context: {n: 0}
initial: race
states:
  race:
    do:
      parallel:
        quick: {agent: a, task: "x"}
        slow: {agent: b, task: "y"}
      join: first
    transitions:
      - {target: race, guard: "ctx.n < 9", effect: "ctx.n += 1"}
      - {target: done}
  done: {type: final}
"""}


class Racing(FakeBackend):
    """The slow branch writes 1000 tokens and goes on writing; the quick one wins with 100 and cuts it."""

    def __init__(self):
        super().__init__({})
        self.produced = 0

    async def agent_create(self, act, **kwargs):
        self.calls.append({"path": act.path})
        if act.path.endswith("slow"):
            await asyncio.sleep(0.02)
            _spend(act, 1000, 1.0)
            self.produced += 1000
            await asyncio.sleep(30)
        await asyncio.sleep(0.1)
        _spend(act, 100, 0.1)
        self.produced += 100
        return "ok", "inst"


async def test_a_branch_a_join_cut_counts_against_the_cap(harness):
    backend = Racing()
    manager = harness.manager()
    run_id = await manager.start(runnable(RACE), backend=backend)
    row = await settle(manager, run_id, timeout=30)
    assert backend.produced >= 3000, "fixture: the loop never reached the cap"
    # the branches the next iteration starts are refused, so its join fails on them
    assert row["status"] == "failed" and "budget_exceeded" in json.dumps(row["error"]), row["error"]
    # at most what the two branches of the one iteration that crossed it write
    assert backend.produced <= 3000 + 1100, backend.produced
    assert manager.output_tokens(run_id)[1] == backend.produced
    assert manager.budget(run_id)[1] >= 3.0


SPENDS_AND_FAILS = {"m.yaml": """\
stategraph: 1
id: m
title: m
limits: {max_output_tokens: 1000}
initial: a
states:
  a:
    do: {agent: writer, task: "write"}
    transitions: [{target: done}]
  done: {type: final}
"""}


class Spender(FakeBackend):
    async def agent_create(self, act, **kwargs):
        self.calls.append({"path": act.path})
        _spend(act, 800)
        raise ActivityError("agent_failed", "the provider dropped the line after 800 tokens")


async def test_a_resume_does_not_refill_the_cap_with_what_it_set_aside(harness):
    backend = Spender({})
    manager = harness.manager()
    run_id = await manager.start(runnable(SPENDS_AND_FAILS), backend=backend)
    row = await settle(manager, run_id)
    assert row["error"]["type"] == "agent_failed", row["error"]
    for _ in range(3):
        again = harness.manager()
        await again.resume(run_id, backend=backend, transient=True)
        row = await settle(again, run_id)
    # 800, then 800 more on the first resume: 1600 is over 1000, the second resume does not call the agent
    assert len(backend.calls) == 2, backend.calls
    assert row["error"]["type"] == "budget_exceeded", row["error"]
    assert harness.manager().output_tokens(run_id)[1] == 1600


RETRIED = {"m.yaml": """\
stategraph: 1
id: m
title: m
limits: {max_output_tokens: 1000}
initial: both
states:
  both:
    do:
      parallel:
        long: {agent: w, task: "x", idempotent: true}
      timeout: 0.3
      retry: {attempts: 3, backoff: 0, errors: [timeout]}
      idempotent: true
    transitions: [{target: done}]
  done: {type: final}
"""}


class Hanging(FakeBackend):
    """Writes 600 tokens and never answers: the composite's timeout cuts it, a crash cuts the next one."""

    async def agent_create(self, act, **kwargs):
        self.calls.append({"path": act.path})
        _spend(act, 600)
        await asyncio.sleep(30)
        return "late", "inst"


async def test_a_composite_resumed_on_its_second_attempt_counts_what_its_first_cut(harness):
    backend = Hanging({})
    manager = harness.manager()
    run_id = await manager.start(runnable(RETRIED), backend=backend)
    await until(lambda: len(backend.calls) == 2, timeout=10)  # attempt 1 cut by its timeout, attempt 2 hangs
    await asyncio.sleep(0.05)
    await manager.shutdown()  # a crash while attempt 2 is in flight
    keys = [key for key, row in activity_rows(harness.store, run_id).items() if row["status"] == "started"]
    assert {"s0/b.long", "s0/a2/b.long"} <= set(keys), f"fixture: attempt 1 cut, attempt 2 in flight: {keys}"
    again = harness.manager()
    await again.resume(run_id, backend=backend)
    row = await settle(again, run_id, timeout=10)
    # 600 cut in attempt 1, 600 carried by attempt 2's child: over 1000, it is not called again
    assert len(backend.calls) == 2, backend.calls
    assert row["error"]["type"] == "budget_exceeded", row["error"]


RACE_THEN_WAIT = {"m.yaml": """\
stategraph: 1
id: m
title: m
limits: {max_output_tokens: 100000, max_cost: 100.0}
events:
  go: {description: on}
initial: race
states:
  race:
    do:
      parallel:
        quick: {agent: a, task: "x"}
        slow: {agent: b, task: "y"}
      join: first
    transitions: [{target: hold}]
  hold:
    transitions: [{trigger: go, target: done}]
  done: {type: final}
"""}


async def test_a_resume_counts_what_a_join_cut_before_the_crash(harness):
    backend = Racing()
    manager = harness.manager()
    run_id = await manager.start(runnable(RACE_THEN_WAIT), backend=backend)
    await until(lambda: (harness.store.get_run(run_id) or {}).get("status") == "waiting", timeout=10)
    assert manager.output_tokens(run_id)[1] == 1100, "fixture: the cut branch's 1000 and the winner's 100, live"
    await manager.shutdown()
    again = harness.manager()
    await again.resume(run_id, backend=backend)
    await until(lambda: run_id in again.live and again.live[run_id].ctx.live, timeout=10)  # replayed to the wait
    assert again.output_tokens(run_id)[1] == 1100  # the cut branch's 1000 read back from its started row
    assert again.budget(run_id)[1] == 1.1


BACKOFF_CUT = {"m.yaml": """\
stategraph: 1
id: m
title: m
limits: {max_output_tokens: 100000}
events: {go: {}}
initial: race
states:
  race:
    do:
      parallel:
        boom: {agent: a, task: "x"}
        retrier: {agent: b, task: "y", retry: {attempts: 3, backoff: 30}}
    transitions:
      - {trigger: error, target: hold}
      - {target: hold}
  hold:
    transitions: [{trigger: go, target: done}]
  done: {type: final}
"""}


class FailsThenWaits(FakeBackend):
    """The retrier spends 1000 and fails: its retry waits 30 s; the other branch fails meanwhile and cuts it."""

    async def agent_create(self, act, **kwargs):
        if act.path.endswith("retrier"):
            _spend(act, 1000, 1.0)
            raise ActivityError("agent_failed", "dropped")
        await asyncio.sleep(0.3)
        raise ActivityError("agent_failed", "boom")


async def test_a_branch_cut_in_its_retrys_backoff_counts_live(harness):
    """Cut in the backoff, outside an attempt: its row carried the 1000, the live limit did not."""
    manager = harness.manager()
    run_id = await manager.start(runnable(BACKOFF_CUT), backend=FailsThenWaits({}))
    await until(lambda: (harness.store.get_run(run_id) or {}).get("status") == "waiting", timeout=10)
    assert harness.store.run_output_tokens(run_id) == 1000, "fixture: the journal holds the cut 1000"
    assert manager.live[run_id].ctx.spent == (1.0, 1000)


async def test_a_finally_cut_by_the_cancel_bound_is_counted_once(harness, monkeypatch):
    """Cut, its spend set aside -- then its outcome written after all: the outcome takes it back over."""
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 0.3)

    class CleansUp(FakeBackend):
        async def agent_create(self, act, **kwargs):
            if act.path.endswith("finally"):
                _spend(act, 500, 0.5)
            await asyncio.sleep(30)
            return "ok", "inst"

    files = {"m.yaml": """\
stategraph: 1
id: m
title: m
limits: {max_output_tokens: 100000}
initial: a
states:
  a:
    do: {agent: w, task: "x"}
    finally: {agent: f, task: "clean up"}
    transitions: [{target: done}]
  done: {type: final}
"""}
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=CleansUp({}))
    await asyncio.sleep(0.3)
    ctx = manager.live[run_id].ctx
    manager.control(run_id, "terminate")
    await until(lambda: (harness.store.get_run(run_id) or {}).get("status") == "cancelled", timeout=10)
    assert harness.store.run_output_tokens(run_id) == 500, "fixture: the finally spent 500, its outcome holds it"
    assert ctx.spent == (0.5, 500) and (ctx.cost_aside, ctx.output_tokens_aside) == (0.0, 0)
