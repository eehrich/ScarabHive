"""Regression tests for the findings of the fix-round re-review (2026-09-26).

Mutation checks run (each red, then restored byte-exactly; runner: scratchpad mut_rereview.py):
- SG109 parallel threshold len(union) > 99: the vars-vs-no-vars test goes red; >= 1: the identical-vars test
  goes red; map rule reduced to "any signature": the item-independent map test goes red.
- _entered_chain returning [target] (the direct edge): both composite-loop tests go red.
- _DATA_METHODS check removed for out: the bound-method test goes red.
- _can_fail back to "last guard is not else": the junction SG104 test goes red.
- NAMESPACE_FIELDS check disabled: the activity.replayed test goes red.
- title check back to ord(ch) < 32: the DEL title test goes red.
- Debugger run_to ignoring the machine: the run_to test goes red.

Engine lens and the review of its fix round (runner: scratchpad mut_engine_lens.py, 23 mutations, all red):
- the retry decision back to ``retry.retries(failure.type)``: the composite, parallel, cause-chain and
  journal-loaded tests go red; without the ``run.interrupted`` check below the key: the composite, handled,
  parallel-race and journal-loaded tests; ``note_outcome`` skipped when journaling live: the handled and race
  tests; skipped when loading the journal: the journal-loaded test; without ``t not in named``: the
  named-cause test; the cause walk stopped after one link: the depth-2 cause test.
- journal.mark_expired's UPDATE without the lease condition: the sweep race test goes red.
- update_debug_unowned without its condition: the refusal test; with the condition on the status instead of the
  lease: its interrupted case; set_points always refused: the stored test; resume not re-reading the row after
  take_lease: the resume race test; not re-checking the status there, or not releasing the lease it took: the
  ended-run test.
- Debugger.assign applying before the journal took the edit, the CancelledError not turned into ValueError,
  RunManager._live without the lost check (seen through evaluate): the assign test goes red; a control whose
  persist finds the run lost reported as done: the control test; server._wait terminating a lost copy: the
  cancelled-caller test.
- ``fence=None`` in RunContext.write / send_event's record / wait_event's rekey, and send_event not calling
  lose(): the matching fenced-write test goes red.
- SG110 without the ``interrupted`` branch: the SG110 test goes red.
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, found, validate

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

HEAD = "stategraph: 1\nid: m\n"


def machine(body: str, head: str = "") -> dict[str, str]:
    return {"m.yaml": HEAD + head + body}


# ------------------------------------------------------------------ SG109 on effective-var signatures

PARALLEL = """\
initial: a
states:
  a:
    do:
      parallel:
        x: {agent: w, task: t%s}
        y: {agent: w, task: t%s}
    transitions: [{target: done}]
  done: {type: final}
"""


def test_sg109_warns_when_a_branch_without_vars_runs_next_to_one_with_vars():
    tree = validate(machine(PARALLEL % (", vars: {phase: one}", "")))
    assert found(tree, "SG109"), "a call without vars clears what its sibling set"


def test_sg109_is_silent_for_branches_with_identical_machine_level_vars():
    tree = validate(machine(PARALLEL % ("", ""), head="vars: {genre: thriller}\n"))
    assert not found(tree, "SG109")


def test_sg109_is_silent_for_a_concurrent_map_with_item_independent_vars():
    tree = validate(machine("""\
context: {items: [1, 2, 3]}
initial: a
states:
  a:
    do:
      map: ctx.items
      concurrency: 3
      each: {agent: w, task: "{{ item }}", vars: {phase: fixed}}
    transitions: [{target: done}]
  done: {type: final}
"""))
    assert not found(tree, "SG109")


def test_sg109_warns_for_a_concurrent_map_with_per_item_vars():
    tree = validate(machine("""\
context: {items: [1, 2, 3]}
initial: a
states:
  a:
    do:
      map: ctx.items
      concurrency: 3
      each: {agent: w, task: "{{ item }}", vars: {chapter: "{{ item }}"}}
    transitions: [{target: done}]
  done: {type: final}
"""))
    assert found(tree, "SG109")


# ------------------------------------------------------------------ loops that enter a composite from outside

def test_sg103_sees_a_loop_that_re_enters_a_composite_around_its_bound():
    tree = validate(machine("""\
initial: x
states:
  x:
    transitions: [{target: a}]
  comp:
    initial: a
    states:
      a:
        max_visits: 3
        transitions: [{target: x}]
"""))
    assert found(tree, "SG103"), "a's count resets on every entry of comp: its max_visits bounds nothing"
    assert not found(tree, "SG101"), "comp is entered by x -> a"


def test_a_loop_bounded_on_the_re_entered_composite_is_bounded():
    tree = validate(machine("""\
initial: x
states:
  x:
    transitions: [{target: a}]
  comp:
    max_visits: 3
    initial: a
    states:
      a:
        transitions: [{target: x}]
    transitions:
      - trigger: error
        target: done
  done: {type: final}
"""))
    assert not found(tree, "SG103")
    assert not found(tree, "SG101")


# ------------------------------------------------------------------ misuse lint and namespaces

def test_a_bound_method_of_plain_data_passed_as_a_value_is_valid():
    tree = validate(machine("""\
initial: a
states:
  a:
    do: {agent: w, task: t}
    transitions:
      - {target: done, guard: "max(out, key=out.get) == 'b'"}
      - {target: done, guard: else}
  done: {type: final}
"""))
    assert not found(tree, "SG004"), [p.message for p in tree.problems]


def test_an_unknown_field_of_activity_is_sg004():
    tree = validate(machine("""\
initial: a
states:
  a:
    do: {agent: w, task: t}
    transitions:
      - {target: done, guard: not activity.replayed}
      - {target: done, guard: else}
  done: {type: final}
"""))
    assert any("replayed" in p.message for p in found(tree, "SG004"))


# ------------------------------------------------------------------ SG104 and junctions

def test_sg104_reports_a_fallback_after_a_junction_that_cannot_fail():
    tree = validate(machine("""\
context: {n: 0}
initial: a
states:
  a:
    do: {agent: w, task: t}
    transitions:
      - target: j
      - target: other
  j:
    type: junction
    transitions:
      - {target: done, guard: ctx.n > 1}
      - {target: done}
  other: {type: final}
  done: {type: final}
"""))
    assert found(tree, "SG104"), "j's last branch always holds, so 'other' never fires"


# ------------------------------------------------------------------ create_machine title

def test_a_title_with_a_control_character_is_refused(tmp_path):
    import types

    from plugins.stategraph.engine.journal import RunStore
    from plugins.stategraph.engine.runner import RunManager
    from plugins.stategraph.service import ServiceError, StateGraphService
    from plugins.stategraph.store import MachineStore

    root = tmp_path / "machines"
    root.mkdir()
    store = RunStore(tmp_path / "runs.db")
    server = types.SimpleNamespace(name="stategraph", system_config=None, runner_agent="r", default_sam="s",
                                   machines=MachineStore([str(root)], [str(root)]), run_store=store,
                                   run_manager=RunManager(store), inject_params={})
    service = StateGraphService(server)
    with pytest.raises(ServiceError) as refused:
        service.create_machine("fresh", title="Review\x7fdraft")
    assert refused.value.status == 422
    assert not (root / "fresh.yaml").exists()


# ------------------------------------------------------------------ run_to is scoped to a machine

async def test_run_to_stops_only_in_the_named_machine(tmp_path):
    from plugins.stategraph.engine.journal import RunStore
    from plugins.stategraph.engine.runner import RunManager
    from plugins.stategraph.tests.stategraph_testkit import load

    files = {
        "m.yaml": HEAD + """\
imports: {sub: ./sub.yaml}
initial: first
states:
  first:
    do: {machine: sub}
    transitions: [{target: work}]
  work:
    transitions: [{target: done}]
  done: {type: final}
""",
        "sub.yaml": """\
stategraph: 1
id: sub
initial: work
states:
  work:
    transitions: [{target: end}]
  end: {type: final}
""",
    }
    manager = RunManager(RunStore(tmp_path / "runs.db"))
    run_id = await manager.start(load(files, execute=True), pause_at_start=True)
    row = await manager.wait(run_id, timeout=5)
    assert row["status"] == "paused"
    manager.control(run_id, "run_to", state="work", machine="m")
    row = await manager.wait(run_id, timeout=5)
    paused = (row.get("debug") or {}).get("paused") or {}
    assert (row["status"], paused.get("machine"), paused.get("state")) == ("paused", "m", "work"), row
    manager.control(run_id, "continue")
    row = await manager.wait(run_id, timeout=5)
    assert row["status"] == "succeeded"


# ================================================================== engine lens of the re-review

@pytest.fixture
async def harness(tmp_path):
    from plugins.stategraph.tests.stategraph_testkit import Harness

    h = Harness(tmp_path)
    yield h
    await h.close()


TOOL_SUB = """\
stategraph: 1
id: sub
initial: t
states:
  t:
    do: {tool: story_create, args: {key: "{{ run.id }}"}}
    transitions: [{target: end}]
  end: {type: final}
"""


def around_sub(retry: str) -> dict[str, str]:
    """m runs the tool submachine under ``retry`` and records the error chain it ends with."""
    return {"sub.yaml": TOOL_SUB, "m.yaml": HEAD + f"""\
imports: {{sub: ./sub.yaml}}
context: {{chain: null}}
initial: s
states:
  s:
    do: {{machine: sub, retry: {retry}}}
    transitions:
      - target: done
      - trigger: error
        target: failed
        effect: ctx.chain = [error.type, error.cause["type"] if error.cause else None]
  done: {{type: final, output: done}}
  failed: {{type: final, output: "{{{{ ctx.chain }}}}"}}
"""}


# ------------------------------------------------------------------ a composite retry and its children's causes

async def test_a_composite_retry_never_restarts_an_interrupted_child(harness):
    """The sub's tool was in flight at the crash: interrupted. The machine's retry must not start it again."""
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, crash_in, resume

    run_id, _ = await crash_in(harness, around_sub("{attempts: 3, backoff: 0}"), "s/t", {})
    second = FakeBackend({"s/t": {"id": 7}})
    row = await resume(harness, run_id, second)

    assert second.count("s/t") == 0, "a retry of the composite ran the interrupted, non-idempotent tool again"
    assert (row["final_state"], row["output"]) == ("failed", ["submachine_failed", "interrupted"]), row


async def test_a_parallel_retry_naming_interrupted_does_not_restart_the_branch(harness):
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, crash_in, resume

    files = machine("""\
context: {err: null}
initial: s
states:
  s:
    do:
      parallel:
        x: {tool: story_create, args: {key: "{{ run.id }}"}}
      retry: {attempts: 3, backoff: 0, errors: [interrupted]}
    transitions:
      - target: done
      - trigger: error
        target: failed
        effect: ctx.err = error.type
  done: {type: final, output: done}
  failed: {type: final, output: "{{ ctx.err }}"}
""")
    run_id, _ = await crash_in(harness, files, "s/x", {})
    second = FakeBackend({"s/x": {"id": 7}})
    row = await resume(harness, run_id, second)

    assert second.count("s/x") == 0, "errors: [interrupted] started the interrupted tool a second time"
    assert (row["final_state"], row["output"]) == ("failed", "interrupted"), row


MID = """\
stategraph: 1
id: mid
imports: {sub: ./sub.yaml}
initial: x
states:
  x:
    do: {machine: sub}
    transitions: [{target: end}]
  end: {type: final}
"""


@pytest.mark.parametrize("depth", [1, 2])
async def test_a_not_retried_cause_behind_submachine_failed_stops_the_retry(harness, depth):
    """depth 2: the tool runs in a submachine of the submachine -- the cause sits two links down the chain."""
    from plugins.stategraph.kinds import ActivityError
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, runnable, settle

    files = around_sub("{attempts: 3, backoff: 0}")
    tool = "s/t"
    if depth == 2:
        files = {**files, "mid.yaml": MID, "m.yaml": files["m.yaml"].replace("./sub.yaml", "./mid.yaml")}
        tool = "s/x/t"
    backend = FakeBackend({tool: ActivityError("tool_denied", "not allowed for this agent")})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend)
    row = await settle(manager, run_id)

    assert backend.count(tool) == 1, "tool_denied is never retried -- not even behind submachine_failed"
    assert row["output"] == ["submachine_failed", "tool_denied" if depth == 1 else "submachine_failed"], row


async def test_a_cause_named_in_errors_lets_the_composite_retry(harness):
    from plugins.stategraph.kinds import ActivityError
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, runnable, settle

    backend = FakeBackend({"s/t": ActivityError("tool_denied", "not allowed for this agent")})
    manager = harness.manager()
    retry = "{attempts: 3, backoff: 0, errors: [submachine_failed, tool_denied]}"
    run_id = await manager.start(runnable(around_sub(retry)), backend=backend)
    row = await settle(manager, run_id)

    assert backend.count("s/t") == 3, "a cause the retry names explicitly is retried"
    assert row["output"] == ["submachine_failed", "tool_denied"], row


def test_sg110_warns_that_interrupted_is_never_retried():
    tree = validate(machine("""\
initial: a
states:
  a:
    do: {tool: story_create, retry: {attempts: 2, errors: [tool_failed, interrupted]}}
    transitions: [{target: done}]
  done: {type: final}
"""))
    assert [p.message for p in found(tree, "SG110") if "never retried" in p.message], tree.problems
    assert not [p for p in found(tree, "SG110") if "tool_failed" in p.message]


# ------------------------------------------------------------------ leases: the sweep, set_points, assign

WAITING = {"m.yaml": HEAD + """\
context: {n: 1}
events: {go: {}}
initial: w
states:
  w:
    transitions: [{trigger: go, target: done}]
  done: {type: final, output: finished}
"""}


class _RenewedAfterRead:
    """The sweeping process's connection; the owner's heartbeat lands between the sweep's read and its write."""

    def __init__(self, conn, renew):
        self.conn, self.renew, self.pending = conn, renew, True

    def execute(self, sql, params=()):
        cursor = self.conn.execute(sql, params)
        if self.pending and sql.startswith("SELECT id FROM runs"):
            self.pending = False
            rows = cursor.fetchall()
            self.renew()
            return type("Rows", (), {"fetchall": lambda _self: rows})()
        return cursor


async def test_the_sweep_leaves_a_run_its_owner_renewed_after_the_read(harness, tmp_path, monkeypatch):
    from plugins.stategraph.engine.journal import RunStore
    from plugins.stategraph.tests.stategraph_testkit import seed_run, utc_at

    seed_run(harness.store, "r1", WAITING, owner="host-a:1", lease=-60)
    owner = RunStore(tmp_path / "runs.db")  # host-a's own connection to the same database
    conn = harness.store._db()
    renewed = _RenewedAfterRead(conn, lambda: owner.renew("r1", "host-a:1", utc_at(60), "running"))
    monkeypatch.setattr(harness.store, "_db", lambda: renewed)
    try:
        swept = harness.store.mark_expired(now=utc_at(0))
    finally:
        owner.close()

    assert not renewed.pending, "fixture: the renewal did not run between the read and the write"
    assert swept == []
    assert harness.store.get_run("r1")["status"] == "running", "the sweep marked a run whose owner is alive"


@pytest.mark.parametrize("status", ["waiting", "interrupted"])  # a swept run's owner may still be alive
async def test_set_points_refuses_a_run_another_process_runs(harness, status):
    from plugins.stategraph.tests.stategraph_testkit import seed_run

    seed_run(harness.store, "busy", WAITING, owner="host-a:1", lease=60, status=status)
    with pytest.raises(ValueError, match="host-a:1"):
        harness.manager().set_points("busy", breakpoints=["w"])
    assert not (harness.store.get_run("busy").get("debug") or {}).get("breakpoints")


async def test_set_points_on_a_run_nobody_runs_is_stored(harness):
    from plugins.stategraph.tests.stategraph_testkit import seed_run

    seed_run(harness.store, "stopped", WAITING, owner="host-a:1", lease=-60, status="interrupted")
    harness.manager().set_points("stopped", breakpoints=["w"])
    stored = harness.store.get_run("stopped")["debug"]["breakpoints"]
    assert [b["state"] for b in stored] == ["w"]


async def test_assign_on_a_run_another_process_took_changes_nothing(harness):
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, runnable, until, utc_at

    manager = harness.manager()
    run_id = await manager.start(runnable(WAITING), backend=FakeBackend(), pause_at_start=True)
    await until(lambda: manager.live[run_id].ctx.status == "paused", what="the pause")
    live = manager.live[run_id]
    harness.store.update_run(run_id, owner="host-b:2", lease_until=utc_at(60))  # B took the run meanwhile

    with pytest.raises(ValueError, match="another process owns"):
        manager.assign(run_id, "ctx.n", "2")
    with pytest.raises(ValueError, match="another process owns"):  # nor does it answer for the run any more
        manager.evaluate(run_id, "ctx.n")

    assert live.root.ctx["n"] == 1, "the local copy took an edit the journal refused"
    assert harness.store.rows(run_id, kinds=("edit",)) == []
    await until(lambda: run_id not in manager.live, what="A's copy to stop")


# ------------------------------------------------------------------ fenced writes of a process that lost its run

async def test_a_process_that_lost_its_run_writes_no_journal_row(harness):
    """A's call is in flight when its lease runs out; B sweeps, resumes and finishes; then A's call answers."""
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, held, runnable, settle, until, utc_at

    files = machine("""\
context: {got: null}
initial: a
states:
  a:
    do: {agent: w, task: t}
    transitions:
      - target: done
        effect: ctx.got = out
  done: {type: final, output: "{{ ctx.got }}"}
""")
    gate = asyncio.Event()
    backend_a = FakeBackend({"a": held(gate, "from A")})
    manager_a = harness.manager()
    run_id = await manager_a.start(runnable(files), backend=backend_a)
    await until(lambda: backend_a.count("a") == 1, what="A's call in flight")
    harness.store.update_run(run_id, lease_until=utc_at(-120))  # A's loop stalled past its lease

    manager_b = harness.manager()
    assert manager_b.sweep_expired() == [run_id]
    await manager_b.resume(run_id, backend=FakeBackend({"a": "from B"}))
    final = await settle(manager_b, run_id)
    assert (final["status"], final["output"]) == ("succeeded", "from B"), final
    journal = harness.store.rows(run_id)

    gate.set()  # A's call answers after B finished the run
    await until(lambda: run_id not in manager_a.live, what="A's copy to stop")

    assert harness.store.rows(run_id) == journal, "A wrote into the journal of a run B owns"
    row = harness.store.get_run(run_id)
    assert (row["status"], row["output"], row["owner"]) == ("succeeded", "from B", manager_b.owner)


async def test_an_event_sent_to_a_lost_run_is_refused_and_stops_the_copy(harness):
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, runnable, until, utc_at

    manager = harness.manager()
    run_id = await manager.start(runnable(WAITING), backend=FakeBackend())
    await until(lambda: manager.live[run_id].ctx.status == "waiting", what="the wait")
    harness.store.update_run(run_id, owner="host-b:2", lease_until=utc_at(60))  # B took the run meanwhile

    answer = manager.send_event(run_id, "go")

    assert answer["accepted"] is False and "another process" in answer["reason"], answer
    assert harness.store.rows(run_id, kinds=("event",)) == []
    await until(lambda: run_id not in manager.live, what="A's copy to stop")


async def test_a_lost_run_does_not_consume_an_event_sent_before_the_takeover(harness):
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, runnable, until, utc_at

    manager = harness.manager()
    run_id = await manager.start(runnable(WAITING), backend=FakeBackend())
    await until(lambda: manager.live[run_id].ctx.status == "waiting", what="the wait")
    assert manager.send_event(run_id, "go")["accepted"]  # recorded while A still owns the run
    harness.store.update_run(run_id, owner="host-b:2", lease_until=utc_at(60))  # before A's loop takes it

    await until(lambda: run_id not in manager.live, what="A's copy to stop")

    events = harness.store.rows(run_id, kinds=("event",))
    assert [e["status"] for e in events] == ["pending"], "A consumed an event of a run B owns"


# ================================================================== review of the fix round itself

HANDLING_SUB = """\
stategraph: 1
id: sub
initial: t
states:
  t:
    do: {tool: story_create, args: {key: "{{ run.id }}"}}
    transitions:
      - target: end
      - trigger: error
        target: %s
  u:
    do: {agent: w, task: reconcile}
    transitions: [{target: end}]
  end: {type: final}
  bad: {type: final, status: failed}
"""


async def test_a_composite_retry_never_restarts_an_interrupted_child_the_sub_handled(harness):
    """The sub catches the tool's interrupted and fails on its own: the chain no longer says interrupted."""
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, crash_in, resume

    files = {**around_sub("{attempts: 2, backoff: 0}"), "sub.yaml": HANDLING_SUB % "bad"}
    run_id, _ = await crash_in(harness, files, "s/t", {})
    second = FakeBackend({"s/t": {"id": 7}})
    row = await resume(harness, run_id, second)

    assert second.count("s/t") == 0, "the retry started the interrupted tool the submachine had handled"
    assert (row["final_state"], row["output"]) == ("failed", ["submachine_failed", "final"]), row


async def test_a_parallel_retry_never_restarts_an_interrupted_branch_behind_another_failure(harness):
    """Both branches were in flight at the crash; on resume x fails first, so the join raises x's error."""
    from plugins.stategraph.kinds import ActivityError
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, held, resume, runnable, until

    files = machine("""\
context: {err: null}
initial: p
states:
  p:
    do:
      parallel:
        x: {agent: w, task: t}
        y: {tool: story_create, args: {key: k}}
      retry: {attempts: 2, backoff: 0}
    transitions:
      - target: done
      - trigger: error
        target: failed
        effect: ctx.err = error.type
  done: {type: final, output: done}
  failed: {type: final, output: "{{ ctx.err }}"}
""")
    first = FakeBackend({"p/x": held(asyncio.Event()), "p/y": held(asyncio.Event(), {"id": 1})})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("p/x") == 1 and first.count("p/y") == 1, what="both branches in flight")
    await manager.shutdown()

    second = FakeBackend({"p/x": ActivityError("agent_failed", "the agent refused"), "p/y": {"id": 2}})
    row = await resume(harness, run_id, second)

    assert second.count("p/y") == 0, "the retry started the interrupted branch a second time"
    assert (row["final_state"], row["output"]) == ("failed", "agent_failed"), row


async def test_an_interrupted_child_loaded_from_the_journal_still_stops_the_retry(harness):
    """Crash twice: the interrupted row of the second process is what the third process loads."""
    from plugins.stategraph.kinds import ActivityError
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, crash_in, held, resume, until

    files = {**around_sub("{attempts: 2, backoff: 0}"), "sub.yaml": HANDLING_SUB % "u"}
    run_id, _ = await crash_in(harness, files, "s/t", {})
    second = FakeBackend({"s/t": {"id": 2}, "s/u": held(asyncio.Event())})
    manager = harness.manager()
    await manager.resume(run_id, backend=second)  # t raises interrupted; the sub goes on to u
    await until(lambda: second.count("s/u") == 1, what="u in flight")
    await manager.shutdown()

    third = FakeBackend({"s/t": {"id": 3}, "s/u": ActivityError("agent_failed", "no answer")})
    row = await resume(harness, run_id, third)

    assert second.count("s/t") + third.count("s/t") == 0, "a retry started the interrupted tool again"
    assert (row["final_state"], row["output"]) == ("failed", ["submachine_failed", "agent_failed"]), row


@pytest.mark.parametrize("control", ["set_points", "pause"])
async def test_a_control_whose_write_finds_the_run_lost_is_refused(harness, control):
    """A has not noticed yet that B took the run: its own persist finds out -- the control is not 'done'."""
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, runnable, until, utc_at

    manager = harness.manager()
    run_id = await manager.start(runnable(WAITING), backend=FakeBackend())
    await until(lambda: manager.live[run_id].ctx.status == "waiting", what="the wait")
    harness.store.update_run(run_id, owner="host-b:2", lease_until=utc_at(60))  # B took the run meanwhile

    with pytest.raises(ValueError, match="another process owns"):
        if control == "set_points":
            manager.set_points(run_id, breakpoints=["w"])
        else:
            manager.control(run_id, "pause")
    assert not (harness.store.get_run(run_id).get("debug") or {}).get("breakpoints")


async def test_breakpoints_stored_while_a_resume_starts_apply_to_it(harness, monkeypatch):
    """A's set_points lands between B's first read of the row and B's lease: B must run with it."""
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, seed_run

    seed_run(harness.store, "r1", WAITING, owner="host-a:1", lease=-60, status="interrupted")
    other = harness.manager()
    take_lease = harness.store.take_lease

    def set_then_take(*args, **kwargs):
        other.set_points("r1", breakpoints=["w"])
        return take_lease(*args, **kwargs)

    monkeypatch.setattr(harness.store, "take_lease", set_then_take)
    manager = harness.manager()
    await manager.resume("r1", backend=FakeBackend())

    assert [b.state for b in manager.live["r1"].ctx.debugger.breakpoints] == ["w"]


async def test_a_run_that_ended_before_the_resume_took_its_lease_is_not_run_again(harness, monkeypatch):
    """The owner finished the run (and released its lease) between the resume's first read and its lease."""
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, seed_run, utc_at

    seed_run(harness.store, "r1", WAITING, owner="host-a:1", lease=-60, status="interrupted")
    take_lease = harness.store.take_lease

    def finish_then_take(*args, **kwargs):
        harness.store.update_run("r1", status="succeeded", output="finished", lease_until=utc_at(-1))
        return take_lease(*args, **kwargs)

    monkeypatch.setattr(harness.store, "take_lease", finish_then_take)
    manager = harness.manager()
    with pytest.raises(ValueError, match="is succeeded"):
        await manager.resume("r1", backend=FakeBackend())

    assert "r1" not in manager.live
    row = harness.store.get_run("r1")
    assert (row["status"], row["output"]) == ("succeeded", "finished")
    assert row["lease_until"] < utc_at(0), "the refused resume keeps the run's lease"


async def test_a_cancelled_caller_does_not_terminate_a_lost_copy(tmp_path):
    """server._wait: the caller stopped, but the run belongs to another process now -- nothing to end here."""
    import types

    from agent_system.config.models import AgentSystemConfig
    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import FakeBackend, held, runnable, tool_config, until

    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    try:
        manager = server.run_manager
        run_id = await manager.start(runnable(machine("""\
initial: a
states:
  a:
    do: {agent: w, task: t}
    transitions: [{target: done}]
  done: {type: final}
""")), backend=FakeBackend({"a": held(asyncio.Event())}))
        await until(lambda: manager.live[run_id].ctx.status == "running" and manager.live[run_id].ctx.busy,
                    what="the call in flight")
        manager.live[run_id].ctx.lost = True  # the window between losing the run and the copy's task ending

        row = await server._wait(run_id, max_wait=1.2, token=types.SimpleNamespace(is_cancelled=True))

        assert row["status"] == "running"
    finally:
        await server.stop_plugin()
