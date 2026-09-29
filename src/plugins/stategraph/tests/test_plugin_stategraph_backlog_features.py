"""Regressions for the backlog of the stategraph review of 2026-09-27 (docs/backlog.md), phase 5: features of the
format and the engine -- a timer state, an operator's repair of an activity's end, ...

The real engine (RunManager, RunStore on tmp_path) with the FakeBackend of the testkit; no LLM.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, crash_in, found, held,
                                                         resume, runnable, sequence, settle, until, validate)
from plugins.stategraph.tests.test_plugin_stategraph_semantics import files

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    made = Harness(tmp_path)
    yield made
    await made.close()


# ------------------------------------------------------------------ F4: a timer state

TIMER = """\
events: {{hurry: {{}}}}
initial: pause
states:
  pause:
    after: {after}
    transitions:
      - target: done
      - trigger: hurry
        target: hurried
  done: {{type: final, output: waited}}
  hurried: {{type: final, output: hurried}}
"""


async def test_a_timer_state_completes_when_its_time_is_up(harness):
    started = time.monotonic()

    row = await harness.run(files(TIMER.format(after="300ms")), timeout=5)

    assert (row["status"], row["output"]) == ("succeeded", "waited"), row
    assert time.monotonic() - started >= 0.3
    assert [r["key"] for r in harness.store.rows(row["id"], kinds=("timer",))] == ["s0:timer"]


async def test_an_event_the_timer_state_takes_may_come_first(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable(files(TIMER.format(after="1h"))))
    await manager.wait(run_id, timeout=2)
    assert manager.live[run_id].ctx.view()["frames"][0]["accepts"] == ["hurry"]  # it waits, and says for what

    manager.send_event(run_id, "hurry")
    row = await settle(manager, run_id)

    assert (row["status"], row["output"]) == ("succeeded", "hurried"), row


AFTER_THEN_WORK = """\
initial: pause
states:
  pause:
    after: 1s
    transitions: [{target: work}]
  work:
    do: {agent: writer, task: go}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx }}"}
"""


async def test_a_resumed_run_does_not_wait_out_a_timer_that_had_fired(harness):
    run_id, backend = await crash_in(harness, files(AFTER_THEN_WORK), "work", {})
    assert [r["key"] for r in harness.store.rows(run_id, kinds=("timer",))] == ["s0:timer"]
    started = time.monotonic()

    row = await resume(harness, run_id, FakeBackend({"work": "written"}), timeout=5)

    assert row["status"] == "succeeded", row
    assert time.monotonic() - started < 0.8, "the resumed run waited for the timer again"


@pytest.mark.parametrize("state,says", [
    ("{after: 5m, do: {call: one}, transitions: [{target: done}]}", "one without do"),
    ("{after: 5m, transitions: [{trigger: go, target: done}]}", "no completion transition"),
    ("{after: 5m, timeout: 1m, transitions: [{target: done}]}", "take one of them"),
])
def test_after_is_checked_where_it_cannot_work(state, says):
    tree = validate(files(f"events: {{go: {{}}}}\ninitial: t\nstates:\n  t: {state}\n  done: {{type: final}}\n"))

    assert any(says in problem.message for problem in found(tree, "SG003")), [p.message for p in tree.problems]


def test_a_final_state_has_no_after():
    tree = validate(files("initial: t\nstates:\n  t: {transitions: [{target: done}]}\n  done: {type: final, after: 1m}\n"))

    assert any("a final state has no after" in problem.message for problem in found(tree, "SG003"))


async def test_a_machine_agent_that_asks_waits_out_a_timer_that_takes_no_event(tmp_path):
    from plugins.stategraph.tests.test_plugin_stategraph_facade import Env, final_of

    env = Env(tmp_path, on_wait="ask")
    (tmp_path / "machines" / "m.yaml").write_text(
        "stategraph: 1\nid: m\nparams: {task: {type: string, required: true}}\ninitial: nap\nstates:\n"
        # longer than the facade's one-second look at the run: it sees the run waiting in the timer
        "  nap: {after: 1500ms, transitions: [{target: done}]}\n  done: {type: final, output: {rested: true}}\n",
        encoding="utf-8")
    try:
        answer = final_of(await env.ask("go", request_id="r1", session_id="s1"))
    finally:
        await env.close()

    assert answer["summary"] == '{"rested": true}', answer


async def test_the_run_tool_waits_out_a_timer_that_takes_no_event_and_says_so_meanwhile(tmp_path):
    from agent_system.config.models import AgentSystemConfig
    from plugins.stategraph.server import NEXT_TIMER, StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config

    (tmp_path / "machines").mkdir(exist_ok=True)
    (tmp_path / "machines" / "nap.yaml").write_text(
        "stategraph: 1\nid: nap\ninitial: pause\nstates:\n  pause: {after: 1500ms, transitions: [{target: done}]}\n"
        "  done: {type: final, output: rested}\n", encoding="utf-8")
    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    reads = []
    get_run = server.run_store.get_run
    server.run_store.get_run = lambda run_id: reads.append(run_id) or get_run(run_id)
    try:
        waited = await server.run_machine({"machine_id": "nap", "wait": "finish", "max_wait": 10})
        assert len(reads) < 50, f"{len(reads)} reads of the run while it waited 1.5s: a spin, not a wait"
        started = await server.run_machine({"machine_id": "nap", "wait": "background"})
        await until(lambda: server.run_store.get_run(started["run_id"])["status"] == "waiting", what="the timer")
        seen = await server.get_run({"run_id": started["run_id"]})
        finished = await server.get_run({"run_id": started["run_id"], "wait": "finish", "max_wait": 10})
    finally:
        await server.stop_plugin()

    assert waited["run_status"] == "succeeded", waited
    assert seen["next"] == NEXT_TIMER, seen
    assert finished["run_status"] == "succeeded", finished


async def test_a_continue_of_a_machine_agent_in_its_timer_waits_it_out(tmp_path):
    from plugins.stategraph.tests.test_plugin_stategraph_facade import Env, final_of

    env = Env(tmp_path, on_wait="ask")
    (tmp_path / "machines" / "m.yaml").write_text(
        "stategraph: 1\nid: m\nparams: {task: {type: string, required: true}}\ninitial: nap\nstates:\n"
        "  nap: {after: 2s, transitions: [{target: done}]}\n  done: {type: final, output: {rested: true}}\n",
        encoding="utf-8")
    try:
        first = asyncio.ensure_future(env.ask("go", request_id="r1", session_id="s1"))
        await until(lambda: any(r["status"] == "waiting" for r in env.runs()), what="the timer")
        first.cancel()  # the client went away: the run stays
        await asyncio.gather(first, return_exceptions=True)
        await env.server.run_manager.shutdown()  # so does the process (agent-cli: a process per message)
        env.server.run_manager._stopping = False

        answer = final_of(await env.ask("again", request_id="r2", session_id="s1"))
    finally:
        await env.close()

    assert answer["summary"] == '{"rested": true}', answer


def test_the_graph_names_a_state_s_timer():
    from plugins.stategraph.model.graph import graph_view

    graph = graph_view(runnable(files(TIMER.format(after="10m"))))

    assert {s["name"]: s["after"] for s in graph["states"]}["pause"] == "10m"


# ------------------------------------------------------------------ F7: an operator repairs an activity's end

REPAIR = """\
context: {got: null}
initial: fetch
states:
  fetch:
    do: {agent: fetcher, task: go}
    transitions:
      - target: work
        effect: ctx.got = out
      - trigger: error
        target: failed
  work:
    do: {agent: writer, task: go on}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.got }}"}
  failed: {type: final, status: failed}
"""


async def paused_at(manager, run_id: str, hook: str) -> None:
    await until(lambda: (manager.live[run_id].ctx.debugger.paused or {}).get("hook") == hook, what=f"the {hook} pause")


async def test_an_operator_repairs_a_failed_activity_at_its_error_breakpoint_and_a_resume_keeps_it(harness):
    backend = FakeBackend({"fetch": ActivityError("agent_failed", "down"), "work": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files(REPAIR)), backend=backend,
                                 breakpoints=[{"state": "fetch", "at": "error"}])
    await paused_at(manager, run_id, "error")

    assert manager.assign(run_id, "out", "{'rows': 3}") == {"rows": 3}
    manager.control(run_id, "continue")
    await until(lambda: backend.count("work") == 1, what="work in flight: the repaired fetch went on")
    await manager.shutdown()
    row = await resume(harness, run_id, FakeBackend({"work": "W"}))

    assert (row["status"], row["output"]) == ("succeeded", {"rows": 3}), row
    assert [edit["data"]["path"] for edit in harness.store.rows(run_id, kinds=("edit",))] == ["out"]


async def test_an_operator_gives_a_finished_activity_another_out_at_its_exit_breakpoint(harness):
    backend = FakeBackend({"fetch": "raw", "work": "W"})
    manager = harness.manager()
    run_id = await manager.start(runnable(files(REPAIR)), backend=backend,
                                 breakpoints=[{"state": "fetch", "at": "exit"}])
    await paused_at(manager, run_id, "exit")

    manager.assign(run_id, "out", "out.upper()")
    manager.control(run_id, "continue")
    row = await settle(manager, run_id)

    assert row["output"] == "RAW", row


async def test_out_is_set_only_where_an_activity_ended(harness):
    backend = FakeBackend({"fetch": "raw", "work": "W"})
    manager = harness.manager()
    run_id = await manager.start(runnable(files(REPAIR)), backend=backend,
                                 breakpoints=[{"state": "fetch", "at": "enter"}])
    await paused_at(manager, run_id, "enter")
    try:
        with pytest.raises(ValueError, match="exit or error breakpoint"):
            manager.assign(run_id, "out", "'x'")
        assert not harness.store.rows(run_id, kinds=("edit",)), "a refused repair was journaled"
    finally:
        manager.control(run_id, "continue")
        await settle(manager, run_id)


async def test_an_older_journal_s_out_edit_still_sets_ctx_out(harness):
    text = REPAIR.replace("context: {got: null}", "context: {got: null, out: null}").replace(
        'output: "{{ ctx.got }}"', 'output: "{{ ctx.got }}/{{ ctx.out }}"')
    backend = FakeBackend({"fetch": "raw", "work": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files(text)), backend=backend, breakpoints=[{"state": "fetch", "at": "exit"}])
    await paused_at(manager, run_id, "exit")
    run = manager.live[run_id].ctx
    frame = run.debugger.paused_frame
    run.record_edit(frame, "exit", "out", "old")  # as set wrote it before repairs: ctx.out, no marker
    frame.ctx["out"] = "old"
    manager.control(run_id, "continue")
    await until(lambda: backend.count("work") == 1, what="work in flight")
    await manager.shutdown()
    row = await resume(harness, run_id, FakeBackend({"work": "W"}))

    assert (row["status"], row["output"]) == ("succeeded", "raw/old"), (row["status"], row["output"])


# ------------------------------------------------------------------ F5: limits.concurrency

def fan(limit: str, fail: str = "fast") -> str:
    return f"""\
{limit}
context: {{r: null}}
initial: fan
states:
  fan:
    do:
      parallel:
        a: {{tool: t, args: {{}}}}
        b: {{tool: t, args: {{}}}}
        c: {{tool: t, args: {{}}}}
        d: {{tool: t, args: {{}}}}
      fail: {fail}
    transitions: [{{target: done, effect: ctx.r = out}}]
  done: {{type: final, output: "{{{{ ctx.r }}}}"}}
"""


def counting() -> tuple[dict[str, int], object]:
    seen = {"now": 0, "peak": 0}

    async def busy(call):
        seen["now"] += 1
        seen["peak"] = max(seen["peak"], seen["now"])
        await asyncio.sleep(0.05)
        seen["now"] -= 1
        return call["path"]

    return seen, busy


@pytest.mark.parametrize("limit,peak", [("limits: {concurrency: 2}", 2), ("", 4)])
async def test_limits_concurrency_bounds_the_activities_that_run_at_once(harness, limit, peak):
    seen, busy = counting()

    row = await harness.run(files(fan(limit)), backend=FakeBackend({f"fan/{b}": busy for b in "abcd"}))

    assert row["status"] == "succeeded" and seen["peak"] == peak, (row["error"], seen)


async def test_a_call_s_own_tools_run_within_its_turn(harness):
    module = "async def fetch(sg):\n    return await sg.tool('store_get', {'k': 1})\n"
    text = files("limits: {concurrency: 1}\ncontext: {r: null}\ninitial: get\nstates:\n  get:\n    do: {call: fetch}\n"
                 "    transitions: [{target: done, effect: ctx.r = out}]\n  done: {type: final, output: '{{ ctx.r }}'}\n",
                 **{"m.py": module})

    row = await harness.run(text, backend=FakeBackend({"get/store_get": "got"}), timeout=5)

    assert (row["status"], row["output"]) == ("succeeded", "got"), row


async def test_an_activity_that_waited_for_its_turn_was_not_started_when_the_run_stopped(harness):
    backend = FakeBackend({"fan/a": held(asyncio.Event()), **{f"fan/{b}": "later" for b in "bcd"}})
    manager = harness.manager()
    run_id = await manager.start(runnable(files(fan("limits: {concurrency: 1}", fail="collect"))), backend=backend)
    await until(lambda: backend.count("fan/a") == 1, what="a in flight, the others waiting for their turn")
    await manager.shutdown()

    row = await resume(harness, run_id, FakeBackend({f"fan/{b}": "later" for b in "abcd"}))

    out = row["output"]
    assert out["a"]["status"] == "failed" and out["a"]["error"]["type"] == "interrupted", out  # a was in flight
    assert [out[b]["status"] for b in "bcd"] == ["succeeded"] * 3, out  # they had waited: not started, not interrupted


def test_a_submachine_s_concurrency_is_said_to_be_ignored():
    tree = validate(files("imports: {sub: ./sub.yaml}\ninitial: s\nstates:\n  s:\n    do: {machine: sub}\n"
                          "    transitions: [{target: done}]\n  done: {type: final}\n",
                          **{"sub.yaml": "stategraph: 1\nid: sub\nlimits: {concurrency: 2}\ninitial: x\n"
                                         "states:\n  x: {type: final}\n"}))

    assert any("limits.concurrency of a submachine is ignored" in p.message for p in found(tree, "SG108")), \
        [p.message for p in tree.problems]


CAUGHT = """\
limits: {concurrency: 1}
context: {r: null}
initial: fan
states:
  fan:
    do:
      parallel:
        a: {tool: t, args: {}}
      fail: collect
    transitions: [{target: again}, {trigger: error, target: again}]
  again:
    do: {tool: t, args: {}}
    transitions: [{target: done}]
  done: {type: final, output: ok}
"""


async def test_a_started_row_that_does_not_land_gives_its_turn_back(harness, monkeypatch):
    import sqlite3

    from plugins.stategraph.engine.journal import RunStore

    real = RunStore.record
    failed = []

    def locked_once(self, run_id, kind, key, **fields):
        if kind == "activity" and key == "s0/b.a" and fields.get("status") == "started" and not failed:
            failed.append(key)
            raise sqlite3.OperationalError("database is locked")
        return real(self, run_id, kind, key, **fields)

    monkeypatch.setattr(RunStore, "record", locked_once)
    row = await harness.run(files(CAUGHT), backend=FakeBackend({"fan/a": "A", "again": "B"}))

    assert failed and (row["status"], row["output"]) == ("succeeded", "ok"), (row["status"], row["error"])


# ------------------------------------------------------------------ F6: emit

EMIT = """\
params: {topic: {type: string}}
initial: tell
states:
  tell:
    do: {emit: "drafting {{ params.topic }}"}
    transitions: [{target: work}]
  work:
    do: {agent: writer, task: go}
    transitions: [{target: done}]
  done: {type: final, output: written}
"""


@pytest.fixture
def status_lines(monkeypatch):
    import agent_system.tools.status as status

    seen: list[tuple[str, str, str]] = []

    async def publish(server, message, request_id=None, **_):
        seen.append((server, message, request_id))

    monkeypatch.setattr(status, "publish_status", publish)
    return seen


async def test_an_emit_tells_the_caller_and_the_run_s_session_once_even_across_a_resume(harness, status_lines):
    backend = FakeBackend({"work": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files(EMIT)), backend=backend, params={"topic": "the storm"})
    await until(lambda: backend.count("work") == 1, what="work in flight")
    await manager.shutdown()
    second = FakeBackend({"work": "W"})

    row = await resume(harness, run_id, second)

    assert row["status"] == "succeeded", row
    assert (backend.notes, second.notes) == (["drafting the storm"], []), "told again on the resume"
    assert status_lines == [("m", "drafting the storm", run_id)], status_lines


async def test_an_emit_is_a_message_in_the_run_s_session(harness, tmp_path, status_lines):
    from plugins.stategraph.tests.stategraph_testkit import AgentHost, FakeAgent
    from plugins.stategraph.tests.test_plugin_stategraph_agents import backend_for

    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    manager = harness.manager()
    run_id = await manager.start(runnable(files(EMIT)), backend_factory=backend_for(host), params={"topic": "x"})
    row = await settle(manager, run_id)

    assert row["status"] == "succeeded", row["error"]
    messages = (await host.sessions.load_session("ann", f"sg_{run_id}"))["messages"]
    assert [(m["role"], m["content"]) for m in messages[1:-1]] == [("assistant", "drafting x")], messages


async def test_an_emit_runs_in_a_mock_only_run(harness, status_lines):
    row = await harness.run(files(EMIT), params={"topic": "y"}, mocks={"work": "mocked"}, mock_only=True)

    assert (row["status"], [text for _, text, _ in status_lines]) == ("succeeded", ["drafting y"]), row


# ------------------------------------------------------------------ F2: a check of an agent's answer

CHECKED = """\
context: {r: null}
initial: write
states:
  write:
    do: {agent: writer, task: go, check: judge, parse_retries: 1}
    transitions:
      - target: done
        effect: ctx.r = out
      - trigger: error
        target: failed
        effect: ctx.r = [error.type, error.message]
  done: {type: final, output: "{{ ctx.r }}"}
  failed: {type: final, status: failed, output: "{{ ctx.r }}"}
"""
JUDGE = """\
def judge(out):
    if len(out) < 10:
        raise ValueError(f"{out!r} is too short")
"""


async def test_a_check_sends_its_reason_to_the_same_instance_and_takes_the_next_answer(harness):
    backend = FakeBackend({"write": sequence("short", "a long enough answer")})

    row = await harness.run(files(CHECKED, **{"m.py": JUDGE}), backend=backend)

    assert (row["status"], row["output"]) == ("succeeded", "a long enough answer"), row
    [create, follow_up] = backend.calls
    assert (create["kind"], follow_up["kind"], follow_up["instance_id"]) == ("agent_create", "agent_continue",
                                                                             "inst-write")
    assert "the check did not take it: 'short' is too short" in follow_up["message"], follow_up["message"]


async def test_an_answer_the_check_never_takes_fails_the_activity_with_check_failed(harness):
    row = await harness.run(files(CHECKED, **{"m.py": JUDGE}), backend=FakeBackend({"write": "short"}))

    assert row["output"][0] == "check_failed" and "'short' is too short" in row["output"][1], row["output"]


JUDGE_WITH_TOOL = """\
async def judge(sg, out):
    await sg.tool("lookup", {"answer": out})
    if out == "A":
        raise ValueError("A will not do")
"""


async def test_a_check_s_tools_belong_to_the_answer_so_a_resume_with_another_answer_does_not_diverge(harness):
    async def writer(call):
        if call["kind"] == "agent_create":
            return "A"
        await asyncio.Event().wait()  # the feedback round is in flight when the process stops

    first = FakeBackend({"write": writer, "write/lookup": "seen"})
    manager = harness.manager()
    run_id = await manager.start(runnable(files(CHECKED, **{"m.py": JUDGE_WITH_TOOL})), backend=first)
    await until(lambda: any(c["kind"] == "agent_continue" for c in first.calls), what="the feedback round")
    await manager.shutdown()

    second = FakeBackend({"write": "B, a new instance's answer", "write/lookup": "seen"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["output"]) == ("succeeded", "B, a new instance's answer"), (row["error"], row["output"])
    assert [c["args"] for c in second.calls if c["kind"] == "call_tool"] == [{"answer": "B, a new instance's answer"}]


def test_a_check_names_a_function_of_the_companion_module():
    tree = validate(files(CHECKED, **{"m.py": "def other(out):\n    pass\n"}))

    assert any("check: the companion module defines no function 'judge'" in p.message for p in found(tree, "SG004"))


# ------------------------------------------------------------------ F1: join policies

def race(join: str, branches: str = "a: {tool: t, args: {}}\n        b: {tool: t, args: {}}\n        c: {tool: t, args: {}}") -> str:
    return f"""\
context: {{r: null, e: null}}
imports: {{sub: ./sub.yaml}}
initial: race
states:
  race:
    do:
      parallel:
        {branches}
      join: {join}
    transitions:
      - target: done
        effect: ctx.r = out
      - trigger: error
        target: failed
        effect: ctx.e = [error.type, sorted(error.data)]
  done: {{type: final, output: "{{{{ ctx.r }}}}"}}
  failed: {{type: final, status: failed, output: "{{{{ ctx.e }}}}"}}
"""


SUB = """\
stategraph: 1
id: sub
finally: {tool: cleanup, args: {}}
initial: work
states:
  work:
    do: {tool: t, args: {}}
    transitions: [{target: end}]
  end: {type: final}
"""


def raced(join: str, **branches: str) -> dict[str, str]:
    lines = "\n        ".join(f"{name}: {body}" for name, body in branches.items())
    return files(race(join, lines) if branches else race(join), **{"sub.yaml": SUB})


async def test_join_first_takes_the_first_branch_that_succeeds_and_cancels_the_rest(harness):
    backend = FakeBackend({"race/a": held(asyncio.Event()), "race/b": "B", "race/c": held(asyncio.Event())})

    row = await harness.run(raced("first"), backend=backend)

    assert (row["status"], row["output"]) == ("succeeded", {"b": "B"}), row
    assert sorted(backend.cancelled) == ["race/a", "race/c"], backend.cancelled


async def test_join_count_needs_that_many_and_survives_the_failures_it_can(harness):
    backend = FakeBackend({"race/a": "A", "race/b": ActivityError("tool_failed", "down"), "race/c": "C"})

    row = await harness.run(raced("{count: 2}"), backend=backend)

    assert (row["status"], row["output"]) == ("succeeded", {"a": "A", "c": "C"}), row


async def test_a_join_that_cannot_reach_its_count_fails_with_every_failure(harness):
    backend = FakeBackend({"race/a": ActivityError("tool_failed", "down"), "race/b": ActivityError("tool_failed", "x"),
                           "race/c": held(asyncio.Event())})

    row = await harness.run(raced("{count: 2}"), backend=backend)

    assert row["output"] == ["join_failed", ["a", "b"]], row["output"]


async def test_a_resume_takes_the_winner_the_journal_says_ended_first(harness):
    """b ended first and won; a ended right after, before the cut -- a later end that does not count. The run
    stops while the cut branch's finally runs: the resume must take b again, not the branch it replays first."""
    release = asyncio.Event()

    async def b(call):
        release.set()  # a's answer is due in the same round: it ends before the join cuts it
        return "B"

    async def a(call):
        await release.wait()
        return "A"

    backend = FakeBackend({"race/a": a, "race/b": b, "race/slow/work": held(asyncio.Event()),
                           "race/slow/sub.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(raced("first", a="{tool: t, args: {}}", b="{tool: t, args: {}}",
                                                slow="{machine: sub}")), backend=backend)
    await until(lambda: backend.count("race/slow/sub.finally") == 1, what="the cut branch's finally in flight")
    ends = [row["key"] for row in harness.store.rows(run_id, kinds=("trace",)) if row["status"] == "joined"]
    assert ends == ["s0/b.b:joined", "s0/b.a:joined"], ends  # fixture: both ended, b first
    await manager.shutdown()

    row = await resume(harness, run_id, FakeBackend({"race/slow/sub.finally": "cleaned"}))

    assert (row["status"], row["output"]) == ("succeeded", {"b": "B"}), (row["error"], row["output"])


@pytest.mark.parametrize("join,says", [
    ("{count: 4}", "more than the 3 branch(es)"),
])
def test_a_join_count_is_checked(join, says):
    tree = validate(raced(join))

    assert any(says in problem.message for problem in found(tree, "SG005")), [p.message for p in tree.problems]


def test_fail_belongs_to_join_all():
    tree = validate(files(race("first").replace("      join: first", "      join: first\n      fail: collect"),
                          **{"sub.yaml": SUB}))

    assert any("fail belongs to join: all" in p.message for p in found(tree, "SG005")), [p.message for p in tree.problems]


SCAN = """\
context: {r: null}
initial: scan
states:
  scan:
    do:
      map: "[1, 2, 3, 4, 5]"
      each: {tool: t, args: {n: "{{ item }}"}}
      concurrency: {concurrency}
      until: out >= 30 and item >= 3
    transitions: [{target: done, effect: ctx.r = out}]
  done: {type: final, output: "{{ ctx.r }}"}
"""


@pytest.mark.parametrize("concurrency", [1, 3])
async def test_map_until_ends_at_the_first_item_in_order_it_holds_for(harness, concurrency):
    async def times_ten(call):
        await asyncio.sleep({1: 0.1, 2: 0.05}.get(call["args"]["n"], 0))  # the first items end last
        return call["args"]["n"] * 10

    backend = FakeBackend({f"scan/{i}": times_ten for i in range(5)})

    row = await harness.run(files(SCAN.replace("{concurrency}", str(concurrency))), backend=backend)

    assert (row["status"], row["output"]) == ("succeeded", [10, 20, 30]), row
    assert sorted(call["args"]["n"] for call in backend.calls) == [1, 2, 3], "an item past the cut started"


@pytest.mark.parametrize("concurrency", [1, 3])
async def test_map_until_cut_by_an_item_that_ends_at_once_ends_the_map(harness, concurrency):
    # a mock (or a replay) ends in its first step: the items after the cut have not begun yet
    row = await harness.run(files(SCAN.replace("{concurrency}", str(concurrency))),
                            mocks={f"scan/{i}": (i + 1) * 10 for i in range(5)}, mock_only=True)

    assert (row["status"], row["output"]) == ("succeeded", [10, 20, 30]), (row["error"], row["output"])


def test_until_sees_out_item_and_index():
    tree = validate(files(SCAN.replace("{concurrency}", "1").replace("out >= 30 and item >= 3",
                                                                     "out >= 30 and item >= 3 and index >= 2")))

    assert not tree.problems, [p.message for p in tree.problems]


# ------------------------------------------------------------------ F3: machines inside a machine's file

LOCAL = """\
context: {r: null}
machines:
  twice:
    params: {n: {type: integer, required: true}}
    context: {got: null}
    initial: calc
    states:
      calc:
        do: {call: double, args: {n: "{{ params.n }}"}}
        transitions: [{target: ask, effect: ctx.got = out}]
      ask:
        do: {agent: writer, task: "{{ ctx.got }}"}
        transitions: [{target: end}]
      end: {type: final, output: "{{ ctx.got }}"}
initial: run_it
states:
  run_it:
    do: {machine: twice, params: {n: 21}}
    transitions: [{target: done, effect: ctx.r = out}]
  done: {type: final, output: "{{ ctx.r }}"}
"""
DOUBLE = "def double(n):\n    return n * 2\n"


async def test_a_machine_inside_the_file_runs_as_a_submachine_with_the_file_s_module(harness):
    row = await harness.run(files(LOCAL, **{"m.py": DOUBLE}), backend=FakeBackend({"run_it/ask": "fine"}))

    assert (row["status"], row["output"]) == ("succeeded", 42), (row["error"], row["output"])
    machines = {r["data"].get("machine") for r in harness.store.rows(row["id"], kinds=("trace",)) if r["data"]}
    assert "m.twice" in machines, machines


async def test_a_run_resumes_inside_a_machine_of_the_file(harness):
    run_id, _ = await crash_in(harness, files(LOCAL, **{"m.py": DOUBLE}), "run_it/ask", {})

    row = await resume(harness, run_id, FakeBackend({"run_it/ask": "fine"}))

    assert (row["status"], row["output"]) == ("succeeded", 42), (row["error"], row["output"])


def test_a_problem_inside_a_machine_of_the_file_is_reported_where_it_lies():
    tree = validate(files(LOCAL.replace("ctx.got = out", "ctx.got = nowhere"), **{"m.py": DOUBLE}))

    [problem] = [p for p in tree.problems if "nowhere" in p.message]
    assert (problem.file, problem.path.split(".")[:4]) == ("m.yaml", ["machines", "twice", "states", "calc"]), problem
    # the machine file's line: files() puts three lines before the text
    assert problem.line == LOCAL.splitlines().index("        transitions: [{target: ask, effect: ctx.got = out}]") + 4


@pytest.mark.parametrize("change,says", [
    (("initial: run_it", "imports: {twice: ./sub.yaml}\ninitial: run_it"), "an import has that name too"),
    (("      end: {type: final, output: \"{{ ctx.got }}\"}",
      "      end: {type: final, output: \"{{ ctx.got }}\"}\n  other:\n    initial: go\n    states:\n"
      "      go:\n        do: {machine: twice, params: {n: 1}}\n        transitions: [{target: fin}]\n"
      "      fin: {type: final}"), "submachine 'twice' is not imported"),
])
def test_a_machine_of_the_file_is_named_once_and_runs_no_other(change, says):
    tree = validate(files(LOCAL.replace(*change), **{"m.py": DOUBLE, "sub.yaml": "stategraph: 1\nid: sub\ninitial: x\n"
                                                                                  "states:\n  x: {type: final}\n"}))

    assert any(says in p.message for p in tree.problems), [p.message for p in tree.problems]


def test_the_panel_sees_the_file_once_and_the_machines_inside_it(tmp_path):
    from plugins.stategraph.engine.journal import RunStore
    from plugins.stategraph.service import StateGraphService
    from plugins.stategraph.store import MachineStore
    from types import SimpleNamespace

    root = tmp_path / "machines"
    root.mkdir()
    for name, text in files(LOCAL, **{"m.py": DOUBLE}).items():
        (root / name).write_text(text, encoding="utf-8")
    runs = RunStore(tmp_path / "runs.db")
    service = StateGraphService(SimpleNamespace(name="stategraph", system_config=None, runner_agent="r", inject_params={},
                                                machines=MachineStore([str(root)], [str(root)], base=tmp_path),
                                                run_store=runs, run_manager=None, agents_of=lambda machine_id: []))

    machine = service.get_machine("m")

    assert sorted(machine["files"]) == ["m.py", "m.yaml"], sorted(machine["files"])
    assert machine["graph"]["machines"] == ["twice"] and not machine["problems"], machine["problems"]


# ------------------------------------------------------------------ F9: schedules

NAP = """\
stategraph: 1
id: nap
python: nap.py
initial: sleep
states:
  sleep:
    do: {call: nap, args: {}}
    transitions: [{target: done}]
  done: {type: final, output: rested}
"""
HOUR = 3600.0
SLOT = 1_000 * HOUR  # a slot start of an every: 1h schedule (slots are whole hours since the epoch)


def scheduled(tmp_path, nap: float = 0.0, **schedule):
    from agent_system.config.models import AgentSystemConfig
    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config

    machines = tmp_path / "machines"
    machines.mkdir(exist_ok=True)
    (machines / "nap.yaml").write_text(NAP, encoding="utf-8")
    (machines / "nap.py").write_text(f"import time\n\n\ndef nap():\n    time.sleep({nap})\n    return 1\n", encoding="utf-8")
    entry = {"name": "hourly", "machine": "nap", "every": "1h", **schedule}
    return StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path, schedules=[entry]))


def test_schedules_are_read_and_what_is_wrong_with_one_is_said():
    from plugins.stategraph.schedules import parse_schedules

    good, problems = parse_schedules([
        {"name": "nightly", "machine": "digest", "every": "24h", "offset": "3h", "params": {"topic": "x"}},
        {"name": "fast", "machine": "digest", "every": "10s"},
        {"name": "nightly", "machine": "digest", "every": "1h"},
        {"name": "odd", "machine": "digest", "every": "1h", "when": "never"},
        {"name": "blink", "machine": "digest", "every": "1h", "late": "10s"},
    ])

    assert [(s.name, s.every, s.offset, s.late) for s in good] == [("nightly", 86400.0, 10800.0, 3600.0)]
    assert [p.split(":")[0] for p in problems] == [f"schedules[{i}]" for i in range(1, 5)], problems
    assert "at least 60s" in problems[0] and "taken" in problems[1] and "when" in problems[2]
    assert "late must be at least 60s" in problems[3], problems[3]


async def test_a_schedule_starts_one_run_per_slot_and_leaves_out_a_slot_seen_too_late(tmp_path):
    from plugins.stategraph.schedules import Scheduler

    server = scheduled(tmp_path, late="10m")
    scheduler = Scheduler(server, server.schedules)
    try:
        first = await scheduler.tick(SLOT + 5)
        again = await scheduler.tick(SLOT + 60)
        late = await scheduler.tick(SLOT + HOUR + 11 * 60)
        second = await scheduler.tick(SLOT + 2 * HOUR + 1)
    finally:
        await server.stop_plugin()

    assert (len(first), again, late, len(second)) == (1, [], [], 1), (first, again, late, second)
    rows = {row["id"]: row for row in server.run_store.list_runs("nap")}
    assert rows[first[0]]["run_key"] == "schedule:hourly:1000" and rows[second[0]]["run_key"] == "schedule:hourly:1002"


async def test_one_process_schedules_and_another_takes_over_when_its_lease_ran_out(tmp_path):
    from plugins.stategraph.schedules import LEASE, Scheduler

    # two processes, one runs.db; late is over when the second takes over -- a slot's run goes on all the same
    one, two = scheduled(tmp_path, nap=2.0, late="1m"), scheduled(tmp_path, nap=2.0, late="1m")
    try:
        [run_id] = await Scheduler(one, one.schedules).tick(SLOT + 5)
        assert await Scheduler(two, two.schedules).tick(SLOT + 30) == [], "a second process scheduled too"
        await until(lambda: any(r["status"] == "started" for r in one.run_store.rows(run_id, kinds=("activity",))),
                    what="the slot's run under way")
        await one.run_manager.shutdown()  # the first process stops: its slot's run is interrupted
        assert one.run_store.get_run(run_id)["status"] == "interrupted"

        resumed = await Scheduler(two, two.schedules).tick(SLOT + 5 + LEASE + 1)

        assert resumed == [run_id], resumed
        assert (await settle(two.run_manager, run_id, timeout=10))["status"] == "succeeded"
    finally:
        await one.stop_plugin()
        await two.stop_plugin()


async def test_a_slot_that_fails_transiently_is_tried_again_later_and_at_most_three_times(tmp_path):
    from plugins.stategraph.schedules import ATTEMPTS, RETRY_AFTER, Scheduler

    now = time.time()  # a run's end is stamped by the real clock: the slot is the one that began a minute ago
    server = scheduled(tmp_path, nap=0.3, every="24h", offset=f"{int(now - 60) % 86400}s")
    (tmp_path / "machines" / "nap.yaml").write_text(NAP.replace("args: {}}", "args: {}, timeout: 50ms}"),
                                                    encoding="utf-8")
    scheduler = Scheduler(server, server.schedules)
    started = []
    try:  # 30s after a failure: too soon; RETRY_AFTER on: again; after ATTEMPTS runs: no more
        for at in (0, 30, RETRY_AFTER + 1, RETRY_AFTER + 2, RETRY_AFTER + 3):
            for run_id in await scheduler.tick(now + at):
                started.append((at, (await settle(server.run_manager, run_id, timeout=10))["error"]["type"]))
    finally:
        await server.stop_plugin()

    assert ATTEMPTS == 3, ATTEMPTS
    assert started == [(0, "timeout"), (RETRY_AFTER + 1, "timeout"), (RETRY_AFTER + 2, "timeout")], started


async def test_a_stopped_instance_gives_its_schedule_up_at_once(tmp_path):
    from plugins.stategraph.schedules import _stamp

    one, two = scheduled(tmp_path), scheduled(tmp_path)  # two processes, one runs.db
    try:
        await one.start_plugin()
        await until(lambda: [r for r in one.run_store.list_runs("nap") if r["status"] == "succeeded"],
                    what="the first process's slot")
        await one.stop_plugin()

        taken = two.run_store.take_scheduler("stategraph", two.run_manager.owner, _stamp(time.time() + 90),
                                             now=_stamp(time.time()))
        assert taken, "the stopped process still holds the schedule"
    finally:
        await two.stop_plugin()


async def test_a_lease_that_cannot_be_given_up_does_not_stop_the_stop(tmp_path):
    import sqlite3

    server = scheduled(tmp_path)
    await server.start_plugin()

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    server.run_store.release_scheduler = locked
    await server.stop_plugin()  # it runs out by itself: the runs stop and the store closes all the same

    assert server.run_manager._stopping and server.run_store._conn is None


# ------------------------------------------------------------------ F8: a callback URL per event

ASK = """\
stategraph: 1
id: ask
events:
  approve: {description: fine, data: {type: object, properties: {by: {type: string}}, required: [by]}}
context: {url: null, by: null}
initial: link
states:
  link:
    do: {callback: approve, expires: 1h}
    transitions: [{target: wait, effect: 'ctx.url = out["url"]'}]
  wait:
    transitions: [{trigger: approve, target: done, effect: 'ctx.by = event.data["by"]'}]
  done: {type: final, output: "{{ ctx.by }}"}
"""


@pytest.fixture
async def hive(tmp_path):
    """A stategraph instance under a public URL, with its web routes on the test's own loop."""
    import httpx
    from fastapi import FastAPI
    from types import SimpleNamespace

    from agent_system.config.models import AgentSystemConfig
    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config
    from plugins.stategraph.web_endpoints import StateGraphWebEndpoints

    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path, public_url="https://hive.test/"))
    (tmp_path / "machines" / "ask.yaml").write_text(ASK, encoding="utf-8")
    app = FastAPI()
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=False))
    app.include_router(StateGraphWebEndpoints(server).get_web_router())
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hive.test")
    yield server, client
    await client.aclose()
    await server.stop_plugin()


async def waiting_url(server) -> tuple[str, str]:
    started = await server.service.start_run("ask", params={})
    run_id = started["run_id"]
    await until(lambda: server.run_store.get_run(run_id)["status"] == "waiting", what="the wait")
    url = server.run_manager.live[run_id].root.ctx["url"]
    return run_id, url


async def test_a_callback_url_sends_its_event_once_and_keeps_only_its_hash(hive):
    server, client = hive
    run_id, url = await waiting_url(server)
    assert url.startswith("https://hive.test/plugins/stategraph/callback?token="), url
    path = url.removeprefix("https://hive.test")

    page = await client.get(path)
    assert page.status_code == 200 and "Send approve" in page.text and server.run_store.get_run(run_id)["status"] == "waiting"
    sent = await client.post(path, json={"data": {"by": "mail"}})
    again = await client.post(path, json={"data": {"by": "mail"}})

    assert (sent.status_code, sent.json()) == (200, {"sent": "approve", "queued": False}), sent.text
    assert (await settle(server.run_manager, run_id))["output"] == "mail"
    assert again.status_code == 404 and (await client.get(path)).status_code == 404
    token = path.partition("?token=")[2]
    rows = server.run_store._db().execute("SELECT * FROM callbacks").fetchall()
    assert len(rows) == 1 and token not in str(dict(rows[0])), "the token is kept in clear"


async def test_a_url_made_with_the_token_in_its_path_still_sends(hive):
    server, client = hive
    run_id, url = await waiting_url(server)
    before = f"/plugins/stategraph/callback/{url.partition('?token=')[2]}"  # the form a URL had before

    page = await client.get(before)
    sent = await client.post(before, json={"data": {"by": "an old mail"}})

    assert (page.status_code, sent.status_code) == (200, 200), (page.text, sent.text)
    assert (await settle(server.run_manager, run_id))["output"] == "an old mail"


async def test_a_system_outside_reaches_the_url_through_remote_paths(hive):
    """network.remote_paths lists exact paths: a token in the path could never be listed."""
    import httpx
    from fastapi import FastAPI
    from types import SimpleNamespace

    from agent_system.auth.remote_paths import install
    from agent_system.config.models import NetworkConfig
    from plugins.stategraph.web_endpoints import StateGraphWebEndpoints

    server, _ = hive
    run_id, url = await waiting_url(server)
    app = FastAPI()
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=False))
    app.include_router(StateGraphWebEndpoints(server).get_web_router())
    install(app, NetworkConfig(remote_paths=["/plugins/stategraph/callback"]))
    outside = httpx.ASGITransport(app=app, client=("203.0.113.5", 40000))
    async with httpx.AsyncClient(transport=outside, base_url="https://hive.test") as remote:
        page = await remote.get(url)
        sent = await remote.post(url, json={"data": {"by": "outside"}})

    assert (page.status_code, sent.status_code) == (200, 200), (page.text, sent.text)
    assert (await settle(server.run_manager, run_id))["output"] == "outside"


async def test_data_that_does_not_fit_leaves_the_url_to_use_again(hive):
    server, client = hive
    run_id, url = await waiting_url(server)
    path = url.removeprefix("https://hive.test")

    wrong = await client.post(path, json={"data": {"who": "x"}})
    right = await client.post(path, json={"data": {"by": "phone"}})

    assert (wrong.status_code, right.status_code) == (422, 200), (wrong.text, right.text)
    assert (await settle(server.run_manager, run_id))["output"] == "phone"


async def test_an_expired_or_unknown_url_is_not_found_and_a_big_body_refused(hive, monkeypatch):
    import time as clock

    from plugins.stategraph import service as service_module

    server, client = hive
    run_id, url = await waiting_url(server)
    path = url.removeprefix("https://hive.test")

    big = await client.post(path, content=b'{"data": "' + b"x" * 70_000 + b'"}', headers={"content-type": "application/json"})
    unknown = await client.post(path[:-4] + "abcd", json={})
    monkeypatch.setattr(service_module.time, "time", lambda: clock.monotonic() * 0 + 4e12)  # far past its expiry
    expired = await client.post(path, json={"data": {"by": "late"}})

    assert (big.status_code, unknown.status_code, expired.status_code) == (413, 404, 404)
    assert server.run_store.get_run(run_id)["status"] == "waiting"


async def test_a_body_is_read_only_for_a_url_that_holds_and_counted_without_its_length(hive):
    server, client = hive
    run_id, url = await waiting_url(server)
    path = url.removeprefix("https://hive.test")
    big = b'{"data": "' + b"x" * 70_000 + b'"}'

    async def chunks():  # no content-length: the body comes chunked
        for start in range(0, len(big), 8192):
            yield big[start:start + 8192]

    unknown = await client.post(path[:-4] + "abcd", content=big)
    streamed = await client.post(path, content=chunks())

    assert (unknown.status_code, streamed.status_code) == (404, 413), (unknown.text, streamed.text)
    assert server.run_store.get_run(run_id)["status"] == "waiting"


async def test_the_url_of_an_ended_run_is_gone(hive):
    from plugins.stategraph.service import ServiceError

    server, client = hive
    run_id, url = await waiting_url(server)
    path = url.removeprefix("https://hive.test")

    await server.service.control_run(run_id, "terminate")

    assert server.run_store.get_run(run_id)["status"] == "cancelled"
    assert ((await client.get(path)).status_code, (await client.post(path, json={})).status_code) == (404, 404)
    with pytest.raises(ServiceError) as refused:  # the service says so itself, not only the route before it
        await server.service.use_callback(path.partition("?token=")[2], {"by": "x"})
    assert refused.value.status == 404, refused.value


async def test_a_run_that_cannot_take_the_event_keeps_its_internals_and_the_url(hive, monkeypatch):
    server, client = hive
    run_id, url = await waiting_url(server)
    path = url.removeprefix("https://hive.test")
    real = server.run_manager.send_event

    def owned(*args):
        raise ValueError("run is owned by host:1:abc until later")

    def broken(*args):
        raise RuntimeError("the database is closed")

    def lost(*args):
        return {"accepted": False, "reason": "another process owns this run now; send the event there"}

    monkeypatch.setattr(server.run_manager, "send_event", owned)
    refused = await client.post(path, json={"data": {"by": "x"}})
    monkeypatch.setattr(server.run_manager, "send_event", lost)
    not_taken = await client.post(path, json={"data": {"by": "x"}})
    monkeypatch.setattr(server.run_manager, "send_event", broken)
    with pytest.raises(RuntimeError):
        await client.post(path, json={"data": {"by": "x"}})
    monkeypatch.setattr(server.run_manager, "send_event", real)
    sent = await client.post(path, json={"data": {"by": "again"}})

    assert refused.status_code == 409 and "host:1" not in refused.text, refused.text
    assert not_taken.status_code == 409 and "another process" not in not_taken.text, not_taken.text
    assert sent.status_code == 200, sent.text
    assert (await settle(server.run_manager, run_id))["output"] == "again"


def test_a_new_callback_drops_the_expired_ones(tmp_path):
    from plugins.stategraph.engine.journal import RunStore

    store = RunStore(tmp_path / "runs.db")
    try:
        store.add_callback("old", "r", "approve", None, 100.0, now=50.0)
        store.add_callback("live", "r", "approve", None, 300.0, now=50.0)
        store.add_callback("new", "r", "approve", None, 400.0, now=200.0)
        kept = sorted(row["hash"] for row in store._db().execute("SELECT hash FROM callbacks"))
    finally:
        store.close()

    assert kept == ["live", "new"], kept


@pytest.mark.parametrize("change,code,says", [
    (("do: {callback: approve, expires: 1h}", "do: {callback: reject, expires: 1h}"), "SG006", "declares no event 'reject'"),
    (("expires: 1h", "expires: 800h"), "SG005", "at most 720h"),
])
def test_a_callback_names_a_declared_event_and_holds_at_most_thirty_days(change, code, says):
    tree = validate({"ask.yaml": ASK.replace(*change)}, root="ask.yaml")

    assert any(says in p.message for p in found(tree, code)), [p.message for p in tree.problems]


# ------------------------------------------------------------------ what the mutants asked for

async def test_a_branch_that_ends_right_after_the_winner_does_not_count(harness):
    release = asyncio.Event()

    async def b(call):
        release.set()  # a's answer is due in the same round: it ends before the join cuts it
        return "B"

    async def a(call):
        await release.wait()
        return "A"

    row = await harness.run(raced("first", a="{tool: t, args: {}}", b="{tool: t, args: {}}"),
                            backend=FakeBackend({"race/a": a, "race/b": b}))

    assert row["output"] == {"b": "B"}, row["output"]


async def test_map_until_cancels_the_later_items_that_are_running(harness):
    async def times_ten(call):
        await asyncio.sleep({1: 0.1, 2: 0.05, 3: 0.02}.get(call["args"]["n"], 0.5))
        return call["args"]["n"] * 10

    backend = FakeBackend({f"scan/{i}": times_ten for i in range(5)})

    row = await harness.run(files(SCAN.replace("{concurrency}", "5")), backend=backend)

    assert row["output"] == [10, 20, 30], row["output"]
    assert sorted(backend.cancelled) == ["scan/3", "scan/4"], backend.cancelled


def test_a_run_s_snapshot_holds_the_file_not_the_machines_inside_it():
    tree = runnable(files(LOCAL, **{"m.py": DOUBLE}))

    assert sorted(tree.snapshot()["files"]) == ["m.py", "m.yaml"], sorted(tree.snapshot()["files"])


def test_the_scheduler_is_held_by_one_owner_until_its_lease_runs_out(tmp_path):
    from plugins.stategraph.engine.journal import RunStore

    store = RunStore(tmp_path / "runs.db")
    try:
        assert store.take_scheduler("sg", "one", "2026-01-01T00:01:30.000+00:00", now="2026-01-01T00:00:00.000+00:00")
        assert not store.take_scheduler("sg", "two", "2026-01-01T00:02:00.000+00:00",
                                        now="2026-01-01T00:00:30.000+00:00")
        assert store.take_scheduler("other", "two", "2026-01-01T00:02:00.000+00:00",
                                    now="2026-01-01T00:00:30.000+00:00"), "one instance's lease held another's"
        assert store.take_scheduler("sg", "one", "2026-01-01T00:02:00.000+00:00", now="2026-01-01T00:00:30.000+00:00")
        assert store.take_scheduler("sg", "two", "2026-01-01T00:04:00.000+00:00", now="2026-01-01T00:02:01.000+00:00")
        store.release_scheduler("sg", "one")  # not its holder: nothing given up
        assert not store.take_scheduler("sg", "one", "2026-01-01T00:05:00.000+00:00",
                                        now="2026-01-01T00:02:02.000+00:00")
        store.release_scheduler("sg", "two")  # a stop: the next one takes it at once
        assert store.take_scheduler("sg", "one", "2026-01-01T00:05:00.000+00:00", now="2026-01-01T00:02:03.000+00:00")
    finally:
        store.close()


async def test_a_callback_resumes_a_run_no_process_runs_and_sends_its_event(hive):
    server, client = hive
    run_id, url = await waiting_url(server)
    await server.run_manager.shutdown()  # the process stops: the run is interrupted in its wait
    server.run_manager._stopping = False
    assert server.run_store.get_run(run_id)["status"] == "interrupted"

    sent = await client.post(url.removeprefix("https://hive.test"), json={"data": {"by": "after the restart"}})

    assert sent.status_code == 200, sent.text
    assert (await settle(server.run_manager, run_id))["output"] == "after the restart"


# ------------------------------------------------------------------ W1: a fork's resources open at its fork point

FORKED = {"m.yaml": """\
stategraph: 1
id: m
context: {docs: [], ns: null}
resources:
  store:
    open: {tool: ns_open}
    fork: {tool: ns_copy, args: {source: "{{ fork_source }}", docs: "{{ ctx.docs }}"}}
    close: {tool: ns_close, args: {ns: "{{ resources.store }}"}}
vars: {json_namespace: "{{ resources.store }}"}
initial: a
states:
  a:
    do: {tool: make, args: {ns: "{{ resources.store }}", name: one}}
    transitions:
      - target: b
        effect: |
          ctx.docs = ctx.docs + [out]
          ctx.ns = resources.store
  b:
    do: {tool: make, args: {ns: "{{ resources.store }}", name: two}}
    transitions:
      - target: c
        effect: ctx.docs = ctx.docs + [out]
  c:
    do: {agent: w, task: "finish {{ ctx.ns }}"}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.docs }}"}
"""}


def store_world(namespace: str, forked: str = "ns_forked", **more) -> FakeBackend:
    def make(call):
        return f"{call['args']['ns']}/{call['args']['name']}"

    return FakeBackend({"resources/store/open": namespace, "resources/store/fork": forked,
                        "resources/store/close": "ok", "a": make, "b": make, "c": "C", **more})


async def forked_source(manager) -> str:
    source = await manager.start(runnable(FORKED), backend=store_world("ns_source"))
    assert (await settle(manager, source))["status"] == "succeeded"
    return source


async def test_a_fork_hook_sees_the_ctx_of_its_fork_point_and_the_fork_names_its_own_store(harness):
    manager = harness.manager()
    source = await forked_source(manager)
    forked = store_world("ns_other")

    row = await settle(manager, await manager.fork(source, at_step=2, backend=forked))

    calls = {call["path"]: call for call in forked.calls}
    assert calls["resources/store/fork"]["args"] == {"source": "ns_source",
                                                     "docs": ["ns_source/one", "ns_source/two"]}, forked.calls
    assert [call["path"] for call in forked.calls] == ["resources/store/fork", "c", "resources/store/close"]
    assert (calls["c"]["task"], calls["c"]["vars"]) == ("finish ns_forked", {"json_namespace": "ns_forked"})
    assert calls["resources/store/close"]["args"] == {"ns": "ns_forked"}
    assert (row["status"], row["output"]) == ("succeeded", ["ns_forked/one", "ns_forked/two"]), row


async def test_a_resumed_fork_forks_its_resources_again_where_it_did_from_the_journal(harness):
    manager = harness.manager()
    source = await forked_source(manager)
    first = store_world("ns_other", c=held(asyncio.Event()))
    fork = await manager.fork(source, at_step=2, backend=first)
    await until(lambda: first.count("c") == 1, what="c in flight")
    await manager.shutdown()

    second = store_world("ns_other", forked="ns_again")
    row = await resume(harness, fork, second)

    assert (row["status"], row["output"]) == ("succeeded", ["ns_forked/one", "ns_forked/two"]), row["error"]
    assert second.count("resources/store/fork") == 0 and second.calls[0]["task"] == "finish ns_forked", second.calls


TIDIED = {"m.yaml": FORKED["m.yaml"].replace("initial: a\n", 'finally: {tool: tidy, args: {what: "{{ ctx.ns }}"}}\ninitial: a\n')}


@pytest.mark.parametrize("at_step", [3, None])  # 3: the final; None: where the finished source stands
async def test_a_fork_that_ends_before_its_fork_point_touches_no_store(harness, at_step):
    manager = harness.manager()
    source = await manager.start(runnable(TIDIED), backend=store_world("ns_source", **{"m.finally": "ok"}))
    assert (await settle(manager, source))["status"] == "succeeded"
    forked = store_world("ns_other", **{"m.finally": "ok"})

    row = await settle(manager, await manager.fork(source, at_step=at_step, backend=forked))

    assert row["status"] == "succeeded", row["error"]
    assert forked.calls == [], "it forked, closed, or ran its source's finally against the source's store"


ZERO = {"m.yaml": """\
stategraph: 1
id: m
context: {prod: null}
resources:
  store:
    open: {tool: ns_open}
    fork: {tool: ns_copy, args: {source: "{{ fork_source }}"}}
initial: a
states:
  a:
    entry: ctx.prod = resources.store.endswith("source")
    do: {tool: make, args: {ns: "{{ resources.store }}", name: one}}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.prod }}"}
"""}


async def test_a_fork_at_step_0_opens_its_store_before_the_first_entry(harness):
    manager = harness.manager()
    source = await manager.start(runnable(ZERO), backend=store_world("ns_source"))
    assert (await settle(manager, source))["output"] is True

    row = await settle(manager, await manager.fork(source, at_step=0, backend=store_world("ns_other")))

    assert (row["status"], row["output"]) == ("succeeded", False), (row["output"], row["error"])


async def test_a_fork_of_an_older_fork_replays_its_first_step_with_that_fork_s_store(harness):
    import json

    manager = harness.manager()
    source = await manager.start(runnable(ZERO), backend=store_world("ns_source"))
    await settle(manager, source)
    first = await manager.fork(source, at_step=0, backend=store_world("ns_other", forked="ns_first"))
    assert (await settle(manager, first))["output"] is False
    # as an engine before the fork point wrote it: the source's values only, the fork hooks ran at its start
    harness.store._db().execute("UPDATE journal SET key = 'resource_sources', status = 'resource_sources', data = ? "
                                "WHERE run_id = ? AND key = 'fork_resources'", (json.dumps({"store": "ns_source"}), first))

    row = await settle(manager, await manager.fork(first, at_step=1, backend=store_world("ns_other", forked="x")))

    assert (row["status"], row["output"]) == ("succeeded", False), row["error"]  # its entry saw ns_first, not ns_source


async def test_a_fork_whose_fork_hook_fails_runs_no_finally_and_closes_what_it_opened(harness):
    logged = {"m.yaml": TIDIED["m.yaml"].replace(
        "resources:\n", "resources:\n  log:\n    open: {tool: log_open}\n    close: {tool: log_close}\n", 1).replace(
        '    do: {tool: make, args: {ns: "{{ resources.store }}", name: one}}\n',
        '    do: {tool: make, args: {ns: "{{ resources.store }}", name: one}}\n'
        '    finally: {tool: tidy, args: {ns: "{{ resources.store }}"}}\n')}
    world = {"resources/log/open": "log_1", "resources/log/close": "ok", "m.finally": "ok", "a/finally": "ok"}
    manager = harness.manager()
    source = await manager.start(runnable(logged), backend=store_world("ns_source", **world))
    assert (await settle(manager, source))["status"] == "succeeded"
    forked = store_world("ns_other", forked=ActivityError("tool_failed", "down"), **world)

    row = await settle(manager, await manager.fork(source, at_step=1, backend=forked))

    assert row["status"] == "failed", row
    assert [call["path"] for call in forked.calls] == ["resources/log/open", "resources/store/fork",
                                                       "resources/log/close"], forked.calls


async def test_a_fork_where_the_source_stands_keeps_the_operator_s_edit_there(harness):
    manager = harness.manager()
    first = store_world("ns_source", c="C")
    source = await manager.start(runnable(FORKED), backend=first, breakpoints=[{"state": "b", "at": "enter"}])
    await paused_at(manager, source, "enter")
    manager.assign(source, "ns", "'EDITED'")
    forked = store_world("ns_other")
    try:
        row = await settle(manager, await manager.fork(source, backend=forked, breakpoints=[]))  # not held at b
    finally:
        manager.control(source, "continue")
        await settle(manager, source)

    assert [call["task"] for call in forked.calls if call["path"] == "c"] == ["finish EDITED"], forked.calls
    assert row["status"] == "succeeded", row["error"]


async def test_a_fork_of_a_fork_replays_outputs_of_every_run_before_it(harness):
    manager = harness.manager()
    source = await forked_source(manager)
    first = await manager.fork(source, at_step=1, backend=store_world("ns_other", forked="ns_first"))
    assert (await settle(manager, first))["output"] == ["ns_first/one", "ns_first/two"]  # the replayed one swapped
    second = store_world("ns_other", forked="ns_second")

    row = await settle(manager, await manager.fork(first, at_step=2, backend=second))

    assert row["status"] == "succeeded", row["error"]
    assert row["output"] == ["ns_second/one", "ns_second/two"], row["output"]
    assert second.calls[0]["args"]["source"] == "ns_first" and second.count("a") + second.count("b") == 0


async def test_a_fork_of_an_older_fork_takes_its_history_from_the_row_it_left(harness):
    import json

    manager = harness.manager()
    source = await forked_source(manager)
    first = await manager.fork(source, at_step=2, backend=store_world("ns_other", forked="ns_first"))
    await settle(manager, first)
    # as an engine before the fork point wrote it: the source's values only, the fork hooks ran at its start
    harness.store._db().execute("UPDATE journal SET key = 'resource_sources', status = 'resource_sources', data = ? "
                                "WHERE run_id = ? AND key = 'fork_resources'",
                                (json.dumps({"store": "ns_source"}), first))

    second = await manager.fork(first, at_step=2, backend=store_world("ns_other", forked="ns_second"))
    await settle(manager, second)

    [written] = [r for r in harness.store.rows(second, kinds=("trace",)) if r["status"] == "fork_resources"]
    assert written["data"] == {"sources": {"store": "ns_first"}, "chain": [{"values": {"store": "ns_source"}, "until": 0}],
                               "at": 2}, written["data"]


FIXED = {"m.yaml": FORKED["m.yaml"].replace(
    '    do: {agent: w, task: "finish {{ ctx.ns }}"}\n    transitions: [{target: done}]\n',
    '    do: {agent: w, task: "finish {{ ctx.ns }}"}\n    transitions: [{target: done}, {trigger: error, target: recover}]\n'
    '  recover:\n    do: {tool: note, args: {ns: "{{ resources.store }}"}}\n    transitions: [{target: done}]\n')}


async def test_a_fork_of_the_whole_journal_forks_where_the_journal_ends(harness):
    manager = harness.manager()
    source = await manager.start(runnable(FORKED), backend=store_world("ns_source", c=ActivityError("agent_failed", "x")))
    assert (await settle(manager, source))["status"] == "failed"
    forked = store_world("ns_other", recover="noted")

    row = await settle(manager, await manager.fork(source, tree=runnable(FIXED), backend=forked))  # the fixed one

    [written] = [r for r in harness.store.rows(row["id"], kinds=("trace",)) if r["status"] == "fork_resources"]
    assert written["data"]["at"] == 3, written["data"]  # c's end is journaled: what comes after it runs live
    assert [call["path"] for call in forked.calls] == ["resources/store/fork", "recover", "resources/store/close"]
    assert forked.calls[1]["args"] == {"ns": "ns_forked"} and row["status"] == "succeeded", (forked.calls, row["error"])


async def test_a_fork_past_the_journal_forks_where_the_source_stands(harness):
    manager = harness.manager()
    gate = asyncio.Event()
    first = store_world("ns_source", b=held(gate, "ns_source/two"))
    source = await manager.start(runnable(FORKED), backend=first)
    await until(lambda: first.count("b") == 1, what="b in flight")
    forked = store_world("ns_other")

    row = await settle(manager, await manager.fork(source, at_step=9, backend=forked))
    gate.set()
    await settle(manager, source)

    [written] = [r for r in harness.store.rows(row["id"], kinds=("trace",)) if r["status"] == "fork_resources"]
    assert written["data"]["at"] == 1, written["data"]
    assert [call["path"] for call in forked.calls] == ["resources/store/fork", "b", "c", "resources/store/close"]
    assert row["output"] == ["ns_forked/one", "ns_forked/two"], row["output"]


async def test_the_finally_of_the_state_the_fork_point_leaves_runs_on_the_fork_s_store(harness):
    tidied = {"m.yaml": FORKED["m.yaml"].replace(
        '    do: {tool: make, args: {ns: "{{ resources.store }}", name: one}}\n',
        '    do: {tool: make, args: {ns: "{{ resources.store }}", name: one}}\n'
        '    finally: {tool: tidy, args: {ns: "{{ resources.store }}"}}\n')}
    manager = harness.manager()
    source = await manager.start(runnable(tidied), backend=store_world("ns_source", **{"a/finally": "ok"}))
    await settle(manager, source)
    forked = store_world("ns_other", **{"a/finally": "ok"})

    await settle(manager, await manager.fork(source, at_step=1, backend=forked))

    assert [call["args"] for call in forked.calls if call["path"] == "a/finally"] == [{"ns": "ns_forked"}], forked.calls


async def test_a_fork_of_a_fork_swaps_each_old_value_once(harness):
    def derived(call):
        return call["args"]["source"] + "_f"

    manager = harness.manager()
    source = await forked_source(manager)
    first = await manager.fork(source, at_step=1, backend=store_world("ns_other", forked=derived))
    assert (await settle(manager, first))["output"] == ["ns_source_f/one", "ns_source_f/two"]

    row = await settle(manager, await manager.fork(first, at_step=2, backend=store_world("ns_other", forked=derived)))

    assert row["output"] == ["ns_source_f_f/one", "ns_source_f_f/two"], (row["output"], row["error"])


async def test_a_fork_of_a_fork_that_never_opened_its_store_replays_on_the_source_s(harness):
    manager = harness.manager()
    source = await forked_source(manager)
    broken = await manager.fork(source, at_step=1, backend=store_world("ns_other", forked=ActivityError("tool_failed", "down")))
    assert (await settle(manager, broken))["status"] == "failed"

    row = await settle(manager, await manager.fork(broken, at_step=1, backend=store_world("ns_other", forked="ns_second")))

    assert (row["status"], row["output"]) == ("succeeded", ["ns_second/one", "ns_second/two"]), row["error"]


async def test_a_fork_of_a_fork_replays_each_step_with_the_values_its_run_had(harness):
    guarded = {"m.yaml": FORKED["m.yaml"].replace(
        "      - target: c\n        effect: ctx.docs = ctx.docs + [out]\n",
        "      - target: c\n        guard: ctx.docs[0].startswith(resources.store)\n        effect: ctx.docs = ctx.docs + [out]\n")}
    assert guarded != FORKED
    manager = harness.manager()
    source = await manager.start(runnable(guarded), backend=store_world("ns_source"))
    assert (await settle(manager, source))["status"] == "succeeded"
    first = await manager.fork(source, at_step=2, backend=store_world("ns_other", forked="ns_first"))
    assert (await settle(manager, first))["status"] == "succeeded"

    row = await settle(manager, await manager.fork(first, at_step=3, backend=store_world("ns_other", forked="ns_second")))

    assert (row["status"], row["output"]) == ("succeeded", ["ns_first/one", "ns_first/two"]), row["error"]


async def test_a_fork_of_a_finished_machine_without_resources_runs_no_finally_of_its_source(harness):
    plain = {"m.yaml": "stategraph: 1\nid: m\nfinally: {tool: tidy}\ninitial: a\nstates:\n"
                       "  a:\n    do: {tool: work}\n    transitions: [{target: done}]\n  done: {type: final}\n"}
    manager = harness.manager()
    source = await manager.start(runnable(plain), backend=FakeBackend({"a": "A", "m.finally": "ok"}))
    assert (await settle(manager, source))["status"] == "succeeded"
    forked = FakeBackend({"a": "A", "m.finally": "ok"})

    row = await settle(manager, await manager.fork(source, backend=forked))

    assert row["status"] == "succeeded" and forked.calls == [], forked.calls


async def test_a_fork_that_fails_again_where_its_source_did_runs_no_finally_of_it(harness):
    manager = harness.manager()
    source = await manager.start(runnable(TIDIED), backend=store_world("ns_source", c=ActivityError("agent_failed", "x"),
                                                                        **{"m.finally": "ok"}))
    assert (await settle(manager, source))["status"] == "failed"
    forked = store_world("ns_other", **{"m.finally": "ok"})

    row = await settle(manager, await manager.fork(source, backend=forked))  # it fails again, where the source did

    assert row["status"] == "failed" and forked.calls == [], forked.calls


def test_a_source_s_resource_object_is_swapped_for_the_fork_s_as_a_whole_and_inside():
    from plugins.stategraph.engine.interpreter import _swap_pairs, _swapped

    old = {"ns": "ns_source", "id": 7, "tags": ["group_source"], "ids": [1], "flags": {"ro": False}, "short": "abc"}
    new = {"ns": "ns_forked", "id": 7, "tags": ["group_forked"], "ids": [2], "flags": {"ro": True}, "short": "xyz"}
    ctx = {"store": dict(old), "path": "ns_source/doc", "tag": "group_source", "short": "abc", "n": 7,
           "picked": [1], "mode": {"ro": False}}

    assert _swapped(ctx, _swap_pairs(old, new)) == {
        "store": new, "path": "ns_forked/doc", "tag": "group_forked", "short": "abc", "n": 7,
        "picked": [1], "mode": {"ro": False}}  # a small object inside the value is no name of it
    assert _swap_pairs("ns_same", "ns_same") == [] and _swap_pairs("abc", "xyz") == []


# ------------------------------------------------------------------ N7: a machine's agent: block offers it as an agent

OFFERED = """\
stategraph: 1
id: helper
title: A helper
params: {topic: {type: string, required: true}}
agent: {on_wait: block, visibility: both}
initial: done
states:
  done: {type: final, output: "{{ params.topic }}"}
"""


def offering_config(tmp_path, **machines):
    from agent_system.config.models import AgentSystemConfig, PluginsConfig, ToolServerConfig

    folder = tmp_path / "machines"
    folder.mkdir(exist_ok=True)
    for name, text in machines.items():
        (folder / f"{name}.yaml").write_text(text, encoding="utf-8")
    return AgentSystemConfig(plugins=PluginsConfig(servers={
        "stategraph": ToolServerConfig(type="stategraph", enabled=True, machine_dirs=[str(folder)]),
        "off": ToolServerConfig(type="stategraph", enabled=False, machine_dirs=[str(tmp_path / "other")])}))


def test_a_machine_with_an_agent_block_is_offered_as_the_entry_it_stands_for(tmp_path):
    from plugins.stategraph.facade import offered_servers

    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "hidden.yaml").write_text(OFFERED.replace("id: helper", "id: hidden"), encoding="utf-8")
    config = offering_config(tmp_path, helper=OFFERED, plain=OFFERED.replace("id: helper", "id: plain").replace(
        "agent: {on_wait: block, visibility: both}\n", ""), wrong=OFFERED.replace("id: helper", "id: stray"),
        broken=OFFERED.replace("id: helper", "id: broken") + "  bad: [\n")

    offered = offered_servers(config)

    assert offered == {"helper_agent": {
        "type": "stategraph_machine", "enabled": True, "machine": "helper", "stategraph": "stategraph",
        "description": "A helper", "input": "text", "task_param": "topic", "on_wait": "block", "params": {},
        "promote": [], "metadata": {"visibility": "both"}, "agent_config": {}, "from_machine_file": True}}, offered


def test_an_agent_block_is_read_as_the_loader_reads_the_file_and_is_private_unless_it_says_otherwise(tmp_path):
    import json

    from plugins.stategraph.facade import offered_servers

    bare = OFFERED.replace("id: helper", "id: bare").replace("agent: {on_wait: block, visibility: both}", "agent: {}")
    twice = bare.replace("id: bare", "id: twice").replace("title: A helper\n", "title: A helper\ntitle: again\n")
    switch = bare.replace("id: bare", "id: switch").replace("initial: done\nstates:\n  done:",
                                                            "initial: on\nstates:\n  on:\n    transitions: [{target: off}]\n  off:")
    bomb = bare.replace("id: bare", "id: bomb").replace("initial: done", "context:\n  a0: &a0 [x, x]\n" + "".join(
        f"  a{i}: &a{i} [*a{i - 1}, *a{i - 1}]\n" for i in range(1, 18)) + "initial: done")  # 2**18 values
    quoted = bare.replace("id: bare", "id: quoted").replace("agent: {}", '"agent": {}')
    marked = "\ufeff" + bare.replace("id: bare", "id: marked")
    flow = json.dumps({"stategraph": 1, "id": "flow", "agent": {}, "initial": "done",
                       "states": {"done": {"type": "final"}}})
    config = offering_config(tmp_path, bare=bare, twice=twice, switch=switch, bomb=bomb, quoted=quoted, marked=marked,
                             flow=flow)

    offered = offered_servers(config)

    assert sorted(offered) == ["bare_agent", "flow_agent", "marked_agent", "quoted_agent", "switch_agent"], \
        sorted(offered)  # YAML 1.2: on and off are names
    assert offered["bare_agent"]["metadata"] == {"visibility": "private"}


def test_an_agent_block_s_params_and_its_name_are_validated():
    from plugins.stategraph.engine.backend import make_config_check
    from agent_system.config.models import AgentSystemConfig, PluginsConfig, ToolServerConfig

    wrong = OFFERED.replace("agent: {on_wait: block, visibility: both}", "agent: {task_param: subject}")
    named = OFFERED.replace("agent: {on_wait: block, visibility: both}", "agent: {name: helper_agent}")
    system = AgentSystemConfig(plugins=PluginsConfig(servers={
        "helper_agent": ToolServerConfig(type="basic_agent", enabled=True),
        "own_agent": ToolServerConfig(type="stategraph_machine", enabled=True, machine="helper", stategraph="stategraph",
                                      from_machine_file=True),
        "pasted_agent": ToolServerConfig(type="stategraph_machine", enabled=True, machine="helper",
                                         stategraph="stategraph")}))
    check = make_config_check(system, runner="stategraph_runner", own_instance="stategraph")

    params = validate({"helper.yaml": wrong}, root="helper.yaml")
    taken = validate({"helper.yaml": named}, root="helper.yaml", config_check=check)
    own = validate({"helper.yaml": named.replace("name: helper_agent", "name: own_agent")}, root="helper.yaml",
                   config_check=check)

    assert any("task_param 'subject' is no param of helper" in p.message for p in found(params, "SG111")), \
        [p.message for p in params.problems]
    assert any("'helper_agent' is the name of another server" in p.message and p.path == "agent.name"
               for p in found(taken, "SG111")), [(p.path, p.message) for p in taken.problems]
    assert not found(own, "SG111"), "its own offer, declared at the start, is taken by itself"
    pasted = validate({"helper.yaml": named.replace("name: helper_agent", "name: pasted_agent")}, root="helper.yaml",
                      config_check=check)
    assert any("a config entry holds 'pasted_agent' for this machine" in p.message for p in found(pasted, "SG111")), \
        [p.message for p in pasted.problems]
    assert not [p for tree in (params, taken, pasted) for p in tree.problems if p.level == "error"], \
        "a problem of the offer keeps the machine itself from running"
    other = make_config_check(system, runner="stategraph_runner", own_instance="sg2")  # an instance sharing the folder
    shared = validate({"helper.yaml": named.replace("name: helper_agent", "name: own_agent")}, root="helper.yaml",
                      config_check=other)
    assert not found(shared, "SG111"), [p.message for p in shared.problems]


def test_a_config_entry_that_holds_the_block_s_name_is_not_its_offer(tmp_path):
    from agent_system.config.models import AgentSystemConfig, PluginsConfig, ToolServerConfig
    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config

    system = AgentSystemConfig(plugins=PluginsConfig(servers={"helper_agent": ToolServerConfig(
        type="stategraph_machine", enabled=True, machine="helper", stategraph="stategraph")}))
    server = StateGraphServer("stategraph", system, tool_config(tmp_path))
    (tmp_path / "machines" / "helper.yaml").write_text(OFFERED, encoding="utf-8")

    assert server.service.get_machine("helper")["offer"] == {"name": "helper_agent", "declared": False}


async def test_a_runtime_offers_a_machine_as_an_agent_that_runs_it(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_system.config.models import (AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
                                            PluginsConfig, ToolServerConfig)
    from agent_system.runtime import Runtime
    from plugins.stategraph.facade import MachineAgent

    folder = tmp_path / "machines"
    folder.mkdir()
    (folder / "helper.yaml").write_text(OFFERED, encoding="utf-8")
    repo = Path(__file__).resolve().parents[4]
    config = AgentSystemConfig(
        llm_system=LLMSystemConfig(models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
                                   profiles={"normal": LLMProfile(model_ref="m")}, default_profile="normal"),
        plugins=PluginsConfig(plugin_dirs=[str(repo / "src" / "plugins")], servers={
            "stategraph": ToolServerConfig(type="stategraph", enabled=True, machine_dirs=[str(folder)],
                                           writable_machine_dirs=[str(folder)], runs_db=str(tmp_path / "runs.db"))}))

    monkeypatch.setattr(Runtime, "last_started", Runtime.last_started)  # start() sets it: given back afterwards
    runtime = Runtime(config).start()
    server = runtime.registry.get("stategraph")
    try:
        agent = runtime.registry.get("helper_agent")
        described = server.service.get_machine("helper")

        assert isinstance(agent, MachineAgent) and (agent.machine_id, agent.on_wait) == ("helper", "block")
        assert (agent._tool_public, agent._tool_visible) == (True, True), "visibility both"
        assert runtime.describe("helper_agent").offered_by == "stategraph_machine"
        assert described["offer"] == {"name": "helper_agent", "declared": True}, described["offer"]
        assert [a["name"] for a in described["agents"] if not a["problems"]] == ["helper_agent"], described["agents"]
        (folder / "later.yaml").write_text(OFFERED.replace("id: helper", "id: later"), encoding="utf-8")
        assert server.service.get_machine("later")["offer"] == {"name": "later_agent", "declared": False}
    finally:
        await server.stop_plugin()
