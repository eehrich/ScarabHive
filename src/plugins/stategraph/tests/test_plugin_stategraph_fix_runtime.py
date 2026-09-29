"""Regressions for the runtime findings of the stategraph review of 2026-09-26 (runner, service, facade, backend,
debugger, journal, server).

Machines run through the real RunManager, service, server and facade on stores under tmp_path; only the boundary
behind the engine is faked (FakeBackend, or AgentHost's FakeAgents with real sessions on disk). A "crash" is a real
``RunManager.shutdown()``; a resume is a NEW manager -- or a new server -- on the same store.

Mutation checks run, each in a copy of src/plugins + src/agent_system (every one turned the named tests red; the
copy and a control after the last mutant green):
- runner.failed_transiently: never / always transient
    -> test_only_a_transient_failure_*, test_a_transient_cause_*
- service.start_run: an ended run of the key not answered (old)
    -> test_a_request_waiting_on_*, test_only_a_transient_*, test_the_same_request_after_a_failure_*, review_engine
       E9
- service.backend_factory: the resumer's user / no user
    -> test_a_resume_someone_else_triggers_runs_as_the_run_s_user
- debugger._fields: types unchecked; parse_points: no list check
    -> test_malformed_points_*, test_control_arguments_*
- service._check_control_args: skipped; a bool as at_step; any definition
    -> test_control_arguments_of_the_wrong_type_are_refused
- debugger.evaluate / after_step: ``plain`` instead of ``_data``
    -> test_evaluate_answers_*, test_a_watchpoint_on_a_module_*
- debugger.from_state: pause or run_to not restored; pause(): the record cleared on stop
    -> test_a_run_stopped_while_paused_*, test_a_run_to_survives_*
- runner.resume: the lease kept on failure
    -> test_a_resume_that_fails_leaves_the_run_to_any_process
- runner: request_seq not journaled / not restored
    -> test_request_ids_are_not_given_twice_across_a_resume
- runner._refuse_while_stopping off; server.start_plugin keeps ``_stopping``
    -> test_nothing_starts_*, test_a_plugin_started_again_*
- runner._execute: ``persist`` for ``persist_end``; control(): terminate of a finished run not skipped
    -> test_a_failed_end_write_*, test_a_terminate_while_the_end_*
- service._terminate_elsewhere: the plain reason
    -> test_terminating_a_run_whose_definition_does_not_load_*
- runner.fork: wait deadlines copied again
    -> test_a_fork_after_an_internal_transition_*
- debugger.stored_points: a point no longer valid raises again; a stored non-list taken apart
    -> test_stored_points_*, test_a_run_with_a_stored_point_*
- runner.TRANSIENT_ERRORS without internal / diverged
    -> test_a_transient_cause_*, test_a_run_the_engine_failed_*
- runner.fork: the source's nesting again
    -> test_a_fork_takes_nothing_of_the_source_s_caller
- runner._execute: run_ended whatever persist_end did; a fenced end write counted as stored
    -> test_the_run_session_tells_the_end_only_once_it_is_stored
- runner.renew_leases: finished runs renewed
    -> test_the_heartbeat_does_not_take_a_finished_run_s_lease_*
- runner.persist_end: every retry while stopping
    -> test_a_stopping_process_tries_each_end_write_once
- service.control_run: steps checked after the action
    -> test_a_control_with_bad_steps_does_nothing
- backend.run_began: the run session without a depth
    -> test_a_run_s_session_is_one_the_chat_opens_read_only
- server.control_run: steps dropped / no journal in the answer
    -> test_the_control_tool_answers_with_the_journal_rows_*
- server.sees_run: True; get_run tool without the user; service.send_event without ``_run``
    -> test_a_user_sees_and_controls_only_their_own_runs
- service.start_run: the second disk load runs
    -> test_a_run_runs_the_tree_it_validated
- service.control_run: the answer with get_run's default steps
    -> test_a_control_answer_carries_the_journal_rows_asked_for
- server.run_machine / facade._start: no caller_session; backend: depth not written, no level left not refused
    -> test_agent_instances_sit_*, test_a_machine_agent_s_*
- agents/stategraph.yaml: file_retention_hours left at json_store's default (config copy via AGENT_CONFIG_PATH)
    -> test_the_machines_json_store_keeps_its_documents_*
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.stategraph.engine.journal import TERMINAL_STATUSES
from plugins.stategraph.engine.machine import CompileError
from plugins.stategraph.engine.runner import failed_transiently
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.service import ServiceError
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, AgentHost, FakeAgent, FakeBackend, Harness,
                                                         held, runnable, settle, tool_config, until, utc_at)
from plugins.stategraph.tests.test_plugin_stategraph_facade import COMPANION, STORY, Env, final_of

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


@pytest.fixture
async def env(tmp_path):
    made = Env(tmp_path)
    yield made
    await made.close()


def _stop_cancellation_monitor() -> None:
    from agent_system.core.cancellation import get_cancellation_manager

    task = get_cancellation_manager()._monitor_task
    if task is not None:
        task.cancel()


def sg_server(tmp_path, host: Any = None, machines: dict[str, str] | None = None, **config: Any) -> StateGraphServer:
    """A StateGraphServer on tmp_path -- a process -- whose runner is ``host`` (AgentHost), with these machine files."""
    from agent_system.config.models import AgentSystemConfig

    system = config.pop("system_config", None) or AgentSystemConfig()
    server = StateGraphServer("stategraph", system, tool_config(tmp_path, **config))
    for name, text in (machines or {}).items():
        (tmp_path / "machines" / name).write_text(text, encoding="utf-8")
    if host is not None:
        server._registry = SimpleNamespace(get=lambda name: host)  # resolve_runner finds the runner here
    return server


CHAIN = {"m.yaml": """\
stategraph: 1
id: m
initial: a
states:
  a:
    do: {agent: w, task: a}
    transitions: [{target: b}]
  b:
    do: {agent: w, task: b}
    transitions: [{target: c}]
  c:
    do: {agent: w, task: c}
    transitions: [{target: done}]
  done: {type: final, output: done}
"""}

WAITING = {"m.yaml": """\
stategraph: 1
id: m
events: {go: {}}
initial: w
states:
  w:
    transitions: [{trigger: go, target: done}]
  done: {type: final, output: finished}
"""}


def broken_snapshot() -> dict[str, Any]:
    """A definition snapshot that no longer compiles (initial and states gone)."""
    snapshot = runnable(WAITING).snapshot()
    snapshot["files"]["m.yaml"] = "stategraph: 1\nid: m\n"
    return snapshot


# ------------------------------------------------------------------ H2/P7: the same request after its run ended

async def test_a_request_waiting_on_another_process_s_run_gets_its_answer_once_it_succeeds(env):
    """H2: B waits while process A runs the request; A's run succeeds -- B answers it instead of a second run."""
    store = env.server.run_store
    tree = env.server.machines.load("m")
    store.create_run("rX_sgother", "m", tree.snapshot(), params={"task": "Nachtzug"}, mocks={},
                     owner="otherhost:1:aa", lease_until=utc_at(120), status="running", run_key="story_machine:rX",
                     session_id="sg_rX_sgother")
    task = asyncio.ensure_future(env.ask("Nachtzug", request_id="rX", session_id="sY"))
    await asyncio.sleep(1.0)
    assert not task.done(), "fixture: the request waits for the other process's run"

    store.update_run("rX_sgother", status="succeeded", output={"story_id": 1, "title": "Nachtzug"},
                     lease_until=utc_at(-1))
    answer = final_of(await asyncio.wait_for(task, 20))

    assert answer["type"] == "final" and answer["run_id"] == "rX_sgother", answer
    assert [row["id"] for row in env.runs()] == ["rX_sgother"], "no second run of the same request"


async def test_the_same_request_after_a_failure_gets_the_failure_again(env):
    first = final_of(await env.ask("fail please", request_id="job-3", session_id="s1"))
    again = final_of(await env.ask("fail please", request_id="job-3", session_id="s2"))

    assert first["type"] == again["type"] == "error", (first, again)
    assert again["run_id"] == first["run_id"] and len(env.runs()) == 1


@pytest.mark.parametrize("error_type,anew", [
    ("agent_failed", True), ("decision_failed", True), ("timeout", True),
    ("tool_failed", False), ("call_failed", False),
])
async def test_only_a_transient_failure_lets_the_same_request_run_anew(env, error_type, anew):
    service = env.server.service
    mocks = {"design": {"$error": {"type": error_type, "message": "boom"}}}
    first = await service.start_run("m", params={"task": "x"}, mocks=mocks, run_key="k")
    row = await settle(env.server.run_manager, first["run_id"])
    assert row["status"] == "failed" and row["error"]["type"] == error_type, "fixture: the run failed so"

    again = await service.start_run("m", params={"task": "x"}, mocks=mocks, run_key="k")

    assert (again["run_id"] != first["run_id"]) is anew, again
    assert len(env.runs()) == (2 if anew else 1)
    if not anew:
        assert again == {"run_id": first["run_id"], "ended": "failed"}


@pytest.mark.parametrize("error,transient", [
    ({"type": "submachine_failed", "cause": {"type": "agent_failed"}}, True),
    ({"type": "submachine_failed", "cause": {"type": "submachine_failed", "cause": {"type": "timeout"}}}, True),
    ({"type": "internal"}, True),
    ({"type": "diverged"}, True),
    ({"type": "final"}, False),
    ({"type": "timed_out"}, False),
    ({"type": "schema_invalid"}, False),
    ({"type": "parse_failed"}, False),
    (None, False),
])
def test_a_transient_cause_makes_the_failure_transient(error, transient):
    assert failed_transiently(error) is transient


async def test_a_run_the_engine_failed_is_tried_anew_by_the_same_request(env, monkeypatch):
    """One "database is locked" on a journal write fails the run as internal -- not the machine's answer."""
    store = env.server.run_store
    real = store.record
    locked: list[str] = []

    def locked_once(run_id: str, kind: str, key: str, **fields: Any) -> bool:
        if kind == "activity" and fields.get("status") == "started" and not locked:
            locked.append(key)
            raise sqlite3.OperationalError("database is locked")
        return real(run_id, kind, key, **fields)

    monkeypatch.setattr(store, "record", locked_once)
    first = await env.server.service.start_run("m", params={"task": "x"}, run_key="k")
    row = await settle(env.server.run_manager, first["run_id"])
    assert row["status"] == "failed" and row["error"]["type"] == "internal", "fixture: the engine failed the run"

    again = await env.server.service.start_run("m", params={"task": "x"}, run_key="k")

    assert again["run_id"] != first["run_id"], again
    assert (await settle(env.server.run_manager, again["run_id"]))["status"] == "succeeded"


# ------------------------------------------------------------------ H9/P1: a resume runs as the run's user

CONTINUE_AFTER_WAIT = """\
stategraph: 1
id: c
events: {go: {}}
context: {inst: null}
initial: a
states:
  a:
    do: {agent: writer, task: draft it}
    transitions: [{target: w, effect: "ctx.inst = activity.instance_id"}]
  w:
    transitions: [{trigger: go, target: b}]
  b:
    do: {agent: writer, continue: "{{ ctx.inst }}", task: shorten it}
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_a_resume_someone_else_triggers_runs_as_the_run_s_user(tmp_path):
    """The admin who resumes only triggers it: the continue finds the instance the run's user created."""
    one = sg_server(tmp_path, AgentHost(tmp_path / "sessions", FakeAgent("writer")), {"c.yaml": CONTINUE_AFTER_WAIT})
    run_id = (await one.service.start_run("c", user_id="ann"))["run_id"]
    await until(lambda: one.run_manager.live[run_id].ctx.status == "waiting", what="the wait")
    await one.stop_plugin()  # the process stops

    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    two = sg_server(tmp_path, host)
    try:
        await two.service.control_run(run_id, "resume", user_id="admin")
        two.service.send_event(run_id, "go")
        row = await settle(two.run_manager, run_id)
    finally:
        await two.stop_plugin()
        _stop_cancellation_monitor()

    assert row["status"] == "succeeded", row["error"]
    assert row["user_id"] == "ann"
    [call] = host.calls()
    assert call["history"] == ["draft it", "writer answers draft it"], "the continue carried ann's instance"
    stored = await host.sessions.load_session("ann", call["session"])  # the instance is ann's, not admin's
    assert stored["messages"][-1]["content"] == "writer answers shorten it"


# ------------------------------------------------------------------ H11: debugger input checked at the boundary

@pytest.mark.parametrize("points", [
    {"breakpoints": [{"state": "design", "condition": True}]},
    {"breakpoints": "design"},
    {"breakpoints": [{"state": "design", "enabled": "no"}]},
    {"watchpoints": [{"expr": 5}]},
    {"watchpoints": [{"expr": "ctx.story", "machine": 3}]},
], ids=["condition_bool", "a_string", "enabled_str", "expr_int", "machine_int"])
async def test_malformed_points_are_refused_before_anything_runs(env, points):
    with pytest.raises(ServiceError) as refused:
        await env.server.service.start_run("m", params={"task": "x"}, **points)

    assert refused.value.status == 422, refused.value.message
    assert env.runs() == []


@pytest.mark.parametrize("action,args", [
    ("fork", {"at_step": "3"}), ("fork", {"at_step": True}), ("fork", {"definition": "latest"}),
    ("run_to", {"state": 5}), ("set_breakpoints", {"breakpoints": "ab"}),
    ("set_watchpoints", {"watchpoints": [{"expr": 5}]}),
], ids=["at_step_str", "at_step_bool", "definition", "state_int", "breakpoints_str", "watch_expr_int"])
async def test_control_arguments_of_the_wrong_type_are_refused(env, action, args):
    run_id = (await env.server.service.start_run("m", params={"task": "hold on"}))["run_id"]
    await until(lambda: env.server.run_manager.live[run_id].ctx.status == "waiting", what="the wait")

    with pytest.raises(ServiceError) as refused:
        await env.server.service.control_run(run_id, action, **args)

    assert refused.value.status == 422, refused.value.message
    assert len(env.runs()) == 1, "no fork was made"


async def test_evaluate_answers_any_value_as_json_data(env):
    run_id = (await env.server.service.start_run("m", params={"task": "hold on"}, breakpoints=["gate"]))["run_id"]
    await until(lambda: env.server.run_manager.live[run_id].ctx.status == "paused", what="the breakpoint")

    answer = await env.server.service.control_run(run_id, "evaluate", expr="len")

    json.dumps(answer)  # the panel and the tool send it as JSON
    assert isinstance(answer["value"], str) and "len" in answer["value"], answer


async def test_a_watchpoint_on_a_module_does_not_end_the_run(env):
    answer = final_of(await env.ask("Nachtzug"))  # fixture: the machine succeeds without the watchpoint
    assert answer["type"] == "final"

    run_id = (await env.server.service.start_run("m", params={"task": "Nachtzug"},
                                                 watchpoints=["__import__('json')"]))["run_id"]
    row = await settle(env.server.run_manager, run_id)

    assert row["status"] == "succeeded", row["error"]


# ------------------------------------------------------------------ H14: a pause survives the stop

async def test_a_run_stopped_while_paused_pauses_again_after_its_resume(harness):
    backend = FakeBackend({"a": "A", "b": "B", "c": "C"})
    first = harness.manager()
    run_id = await first.start(runnable(CHAIN), backend=backend, pause_at_start=True)
    await until(lambda: first.live[run_id].ctx.status == "paused", what="the pause at the start")
    await first.shutdown()

    second = harness.manager()
    await second.resume(run_id, backend=backend)
    await until(lambda: run_id not in second.live or second.live[run_id].ctx.status == "paused", what="the resume")

    assert run_id in second.live and second.live[run_id].ctx.debugger.paused["state"] == "a", "paused again"
    assert backend.calls == [], "no activity the operator had halted ran"
    second.control(run_id, "continue")
    assert (await settle(second, run_id))["status"] == "succeeded"


async def test_a_run_to_survives_the_stop(harness):
    gate = asyncio.Event()
    first_backend = FakeBackend({"a": held(gate, "A"), "b": "B", "c": "C"})
    first = harness.manager()
    run_id = await first.start(runnable(CHAIN), backend=first_backend)
    await until(lambda: first_backend.count("a") == 1, what="a in flight")
    first.control(run_id, "run_to", state="c")
    await first.shutdown()

    backend = FakeBackend({"a": "A", "b": "B", "c": "C"})
    second = harness.manager()
    await second.resume(run_id, backend=backend)
    await until(lambda: run_id not in second.live or second.live[run_id].ctx.status == "paused", what="the resume")

    assert run_id in second.live and second.live[run_id].ctx.debugger.paused["state"] == "c", "held at c"
    assert (backend.count("b"), backend.count("c")) == (1, 0)


# ------------------------------------------------------------------ a failed resume gives its lease back

async def test_a_resume_that_fails_leaves_the_run_to_any_process(harness):
    harness.store.create_run("r1", "m", broken_snapshot(), params={}, mocks={}, owner="gone:1:x",
                             lease_until=utc_at(-5), status="interrupted", session_id="sg_r1")
    with pytest.raises(CompileError):
        await harness.manager().resume("r1")

    assert harness.store.get_run("r1")["status"] == "interrupted"
    assert harness.store.take_lease("r1", "other:2:y", utc_at(60), now=utc_at(0)), "another process may take it"


# ------------------------------------------------------------------ P3: request ids stay unique across a resume

class RecordingBackend(FakeBackend):
    def __init__(self, handlers: dict[str, Any], ids: list[str]):
        super().__init__(handlers)
        self.ids = ids

    async def call_tool(self, act: Any, *, tool: str, args: dict[str, Any]) -> Any:
        self.ids.append(act.request_id())
        return await super().call_tool(act, tool=tool, args=args)


TOOLS = {"m.yaml": """\
stategraph: 1
id: m
initial: a
states:
  a:
    do: {tool: x_y, args: {}}
    transitions: [{target: b}]
  b:
    do: {tool: x_z, args: {}, idempotent: true}
    transitions: [{target: done}]
  done: {type: final}
"""}


async def test_request_ids_are_not_given_twice_across_a_resume(harness):
    ids: list[str] = []
    before = RecordingBackend({"a": {"ok": 1}, "b": held(asyncio.Event())}, ids)
    first = harness.manager()
    run_id = await first.start(runnable(TOOLS), backend=before)
    await until(lambda: before.count("b") == 1, what="b in flight")
    await first.shutdown()

    second = harness.manager()
    await second.resume(run_id, backend=RecordingBackend({"a": {"ok": 1}, "b": {"ok": 2}}, ids))
    row = await settle(second, run_id)

    assert row["status"] == "succeeded", row["error"]
    assert len(ids) == 3 and len(set(ids)) == 3, ids


# ------------------------------------------------------------------ nothing starts while the process stops

async def test_nothing_starts_or_resumes_while_the_process_stops(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable(WAITING), backend=FakeBackend())
    await until(lambda: manager.live[run_id].ctx.status == "waiting", what="the wait")
    await manager.shutdown()
    assert harness.store.get_run(run_id)["status"] == "interrupted", "fixture"

    with pytest.raises(ValueError):
        await manager.start(runnable(WAITING), backend=FakeBackend())
    with pytest.raises(ValueError):
        await manager.resume(run_id, backend=FakeBackend())

    assert [row["id"] for row in harness.store.list_runs()] == [run_id]
    assert harness.store.get_run(run_id)["status"] == "interrupted"


async def test_a_plugin_started_again_runs_machines_again(env):
    await env.server.stop_plugin()
    await env.server.start_plugin()

    answer = final_of(await env.ask("Nachtzug"))

    assert answer["type"] == "final", answer


# ------------------------------------------------------------------ the end write is not swallowed

def lock_the_end_write(store: Any, monkeypatch: Any) -> list[float]:
    """The database is locked for a moment as the run ends: every write of its end fails for 0.3 s."""
    real = store.update_run
    locked: list[float] = []

    def locked_briefly(run_id: str, **fields: Any) -> int:
        if fields.get("status") in TERMINAL_STATUSES:
            locked[:] = locked or [time.monotonic() + 0.3]
            if time.monotonic() < locked[0]:
                raise sqlite3.OperationalError("database is locked")
        return real(run_id, **fields)

    monkeypatch.setattr(store, "update_run", locked_briefly)
    return locked


async def test_a_failed_end_write_is_tried_again(harness, monkeypatch):
    locked = lock_the_end_write(harness.store, monkeypatch)
    row = await harness.run(CHAIN, backend=FakeBackend({"a": "A", "b": "B", "c": "C"}))

    assert locked, "fixture: the end write met the lock"
    assert (row["status"], row["output"]) == ("succeeded", "done") and row["finished_at"], row


async def test_a_terminate_while_the_end_is_stored_does_not_cut_it_short(harness, monkeypatch):
    locked = lock_the_end_write(harness.store, monkeypatch)
    manager = harness.manager()
    run_id = await manager.start(runnable(CHAIN), backend=FakeBackend({"a": "A", "b": "B", "c": "C"}))
    await until(lambda: locked, what="the end write met the lock")

    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert (row["status"], row["output"]) == ("succeeded", "done") and row["finished_at"], row


async def test_stored_points_that_no_longer_parse_can_be_replaced(harness):
    """A run stored with a point of the wrong type (before the check existed): replacing it must not need it."""
    harness.store.create_run("r1", "m", runnable(WAITING).snapshot(), params={}, mocks={}, owner="gone:1:x",
                             lease_until=utc_at(-5), status="interrupted", session_id="sg_r1",
                             debug={"breakpoints": [{"state": "w", "condition": True}], "watchpoints": []})
    manager = harness.manager()

    manager.set_points("r1", breakpoints=["w@exit"])
    await manager.resume("r1", backend=FakeBackend())

    assert [(b.state, b.at) for b in manager.live["r1"].ctx.debugger.breakpoints] == [("w", "exit")]


# ------------------------------------------------------------------ an honest terminate without a finally

async def test_terminating_a_run_whose_definition_does_not_load_says_its_finally_did_not_run(tmp_path):
    server = sg_server(tmp_path)
    server.run_store.create_run("r1", "m", broken_snapshot(), params={}, mocks={}, owner="gone:1:x",
                                lease_until=utc_at(-5), status="interrupted", session_id="sg_r1")
    try:
        answer = await server.service.control_run("r1", "terminate")
    finally:
        await server.stop_plugin()

    assert answer["status"] == "cancelled"
    assert "finally activities did not run" in answer["error"]["message"], answer["error"]


# ------------------------------------------------------------------ a fork during a wait waits afresh

TIMED_WAIT = {"m.yaml": """\
stategraph: 1
id: m
events: {ping: {}, go: {}}
context: {pings: 0}
initial: w
states:
  w:
    timeout: 1.0
    transitions:
      - trigger: ping
        effect: ctx.pings = ctx.pings + 1
      - trigger: go
        target: ok
      - trigger: error
        target: late
  ok: {type: final, output: ok}
  late: {type: final, output: late}
"""}


async def test_a_fork_after_an_internal_transition_of_a_wait_gets_a_fresh_deadline(harness):
    manager = harness.manager()
    source = await manager.start(runnable(TIMED_WAIT))
    await until(lambda: manager.live[source].ctx.status == "waiting", what="the wait")
    manager.send_event(source, "ping")  # an internal transition: the wait goes on, its deadline with it
    assert (await settle(manager, source))["final_state"] == "late", "fixture: the source's wait expired"
    keys = {row["key"] for row in harness.store.rows(source, kinds=("trace", "event", "timer"))}
    assert {"s0:wait", "s0:event", "s1:timer"} <= keys, "fixture: step 0 is the ping, step 1 the timeout"

    fork = await manager.fork(source, at_step=1)  # after the ping, before the timeout
    await until(lambda: fork not in manager.live or manager.live[fork].ctx.status == "waiting", what="the fork")
    if fork in manager.live:
        manager.send_event(fork, "go")
    row = await settle(manager, fork)

    assert row["final_state"] == "ok", "the fork took the timeout path at once"


# ------------------------------------------------------------------ owner checks

async def test_a_user_sees_and_controls_only_their_own_runs(tmp_path, monkeypatch):
    from agent_system.config.models import AgentSystemConfig, AuthConfig

    server = sg_server(tmp_path, machines={"m.yaml": STORY, "m.py": COMPANION}, allowed_users=["alice", "bob"],
                       system_config=AgentSystemConfig(auth=AuthConfig(enabled=True)))
    monkeypatch.setattr(server, "_is_admin", lambda user: user == "root")
    try:
        run_id = (await server.service.start_run("m", params={"task": "hold on"}, user_id="alice"))["run_id"]
        await until(lambda: server.run_manager.live[run_id].ctx.status == "waiting", what="the wait")

        seen = {user: (await server.get_run({"run_id": run_id, "_user_id": user}))["status"]
                for user in ("alice", "root", "bob")}
        paused = await server.control_run({"run_id": run_id, "action": "pause", "_user_id": "bob"})
        sent = await server.send_event({"run_id": run_id, "name": "go", "_user_id": "bob"})
        assert server.run_manager.live[run_id].ctx.status == "waiting", "bob's event and pause did nothing"
        ended = await server.send_event({"run_id": run_id, "name": "go", "_user_id": "alice"})
        row = await settle(server.run_manager, run_id)
    finally:
        await server.stop_plugin()

    assert seen == {"alice": "success", "root": "success", "bob": "error"}
    assert paused["error_type"] == sent["error_type"] == "http_404", (paused, sent)
    assert ended["status"] == "success" and row["status"] == "succeeded"


# ------------------------------------------------------------------ the validated tree is the one that runs

async def test_a_run_runs_the_tree_it_validated(env, tmp_path, monkeypatch):
    companion = tmp_path / "machines" / "m.py"
    assert companion.exists(), "fixture: the companion module"
    machines = env.server.machines
    real = machines.load

    def load_then_save(*args: Any, **kwargs: Any) -> Any:
        tree = real(*args, **kwargs)
        # a save lands right after the validation read the files
        companion.write_text(COMPANION.replace('task.split("|")[0]', '"saved meanwhile"'), encoding="utf-8")
        return tree

    monkeypatch.setattr(machines, "load", load_then_save)
    answer = final_of(await env.ask("Nachtzug"))

    assert json.loads(answer["summary"])["title"] == "Nachtzug", answer


# ------------------------------------------------------------------ control answers carry the asked history

MANY = {"many.yaml": """\
stategraph: 1
id: many
python: many.py
initial: a
states:
  a:
    do: {map: "list(range(80))", each: {call: echo, args: {x: "{{ item }}"}}}
    transitions: [{target: done}]
  done: {type: final}
""", "many.py": "def echo(x):\n    return x\n"}


async def test_a_control_answer_carries_the_journal_rows_asked_for(tmp_path):
    server = sg_server(tmp_path, machines=MANY)
    try:
        run_id = (await server.service.start_run("many", mock_only=True))["run_id"]
        await settle(server.run_manager, run_id)
        rows = len(server.run_store.rows(run_id, kinds=("activity", "trace", "event", "edit", "timer")))
        assert rows > 60, "fixture: more rows than the default answer holds"

        asked = await server.service.control_run(run_id, "set_breakpoints", breakpoints=[], steps=200)
        default = await server.service.control_run(run_id, "set_breakpoints", breakpoints=[])
    finally:
        await server.stop_plugin()

    assert len(asked["journal"]) == min(rows, 200)
    assert len(default["journal"]) == 50


# ------------------------------------------------------------------ P2: agent instances continue the caller's tree

NESTED = """\
stategraph: 1
id: n
initial: a
states:
  a:
    do: {agent: writer, task: draft it}
    transitions: [{target: done}]
  done: {type: final}
"""


@pytest.mark.parametrize("budget,expected", [(2, (4, 1)), (None, (4, None)), (0, None)],
                         ids=["budget", "no_budget", "no_level_left"])
async def test_agent_instances_sit_one_level_below_the_run_s_caller(tmp_path, budget, expected):
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    server = sg_server(tmp_path, host, {"n.yaml": NESTED})
    caller = await host.sessions.create_session(user_id="ann", session_id="caller1", agent_name="coordinator")
    caller["depth"] = 3  # as the SAM that made the caller's session wrote it
    if budget is not None:
        caller["depth_budget"] = budget
    await host.sessions.save_session(caller)
    try:
        result = await server.run_machine({"machine_id": "n", "_user_id": "ann", "_session_id": "caller1"})
    finally:
        await server.stop_plugin()
        _stop_cancellation_monitor()

    if expected is None:
        assert result["run_status"] == "failed" and result["error"]["type"] == "config", result
        assert host.calls() == []
        return
    assert result["run_status"] == "succeeded", result
    [call] = host.calls()
    stored = await host.sessions.load_session("ann", call["session"])
    assert (stored["depth"], stored.get("depth_budget")) == expected


FACADE_NESTED = """\
stategraph: 1
id: m
params: {task: {type: string, required: true}}
initial: a
states:
  a:
    do: {agent: writer, task: "{{ params.task }}"}
    transitions: [{target: done}]
  done: {type: final, output: {story_id: 1}}
"""


async def test_a_machine_agent_s_instances_sit_one_level_below_its_session(env, tmp_path, monkeypatch):
    """The v4 path: a SAM made the facade's session; the machine's agents are the next level of that tree."""
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    monkeypatch.setattr(env.server, "resolve_runner", lambda: host)
    (tmp_path / "machines" / "m.yaml").write_text(FACADE_NESTED, encoding="utf-8")
    session = await host.sessions.create_session(user_id="ann", session_id="sub_story_1", agent_name="story_machine")
    session["depth"], session["depth_budget"] = 2, 3
    await host.sessions.save_session(session)
    env.agent._session_tracker.set_session_metadata("sub_story_1", {"user_id": "ann"})

    answer = final_of(await env.ask("Nachtzug", session_id="sub_story_1"))

    assert answer["type"] == "final", answer
    [call] = host.calls()
    stored = await host.sessions.load_session("ann", call["session"])
    assert (stored["depth"], stored["depth_budget"]) == (3, 2)


# ------------------------------------------------------------------ P4: the machines' documents outlive a pause

def test_the_machines_json_store_keeps_its_documents_as_long_as_a_run_may_resume(tmp_path):
    """The configured stategraph_json, as the framework resolves it: its startup sweep deletes no document of a
    run that lies interrupted for longer than json_store's default retention."""
    from agent_system.config.settings import get_tool_server_config, load_settings
    from plugins.json_store.server import JsonStoreServer

    config = load_settings()
    entry = get_tool_server_config("stategraph_json", config)
    assert entry is not None and entry.enabled, "fixture: the machines' store is configured"
    namespace = tmp_path / "json" / "stategraph_json" / "sg_run1"
    namespace.mkdir(parents=True)
    document = namespace / "draft.json"
    document.write_text("{}", encoding="utf-8")
    long_ago = time.time() - 400 * 24 * 3600
    os.utime(document, (long_ago, long_ago))

    settings = entry.model_copy(update={"config": {**(getattr(entry, "config", None) or {}),
                                                   "storage_path": str(tmp_path / "json")}})
    JsonStoreServer("stategraph_json", config, settings)

    assert document.exists()



# ================================================================== the review of the whole diff (R2-R9)

def write_machine() -> dict[str, str]:
    from plugins.stategraph.tests.test_plugin_stategraph_run_session import WRITE

    return WRITE


def ann_backend(host: AgentHost, nesting: Any = None) -> Any:
    from plugins.stategraph.tests.test_plugin_stategraph_agents import backend_for

    return backend_for(host, nesting=nesting)


# ------------------------------------------------------------------ R2: a fork is the forking user's own run

async def test_a_fork_takes_nothing_of_the_source_s_caller(tmp_path):
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    server = sg_server(tmp_path, host, {"n.yaml": NESTED})
    caller = await host.sessions.create_session(user_id="ann", session_id="caller1", agent_name="coordinator")
    caller["depth"], caller["depth_budget"] = 3, 1
    await host.sessions.save_session(caller)
    try:
        source = await server.run_machine({"machine_id": "n", "_user_id": "ann", "_session_id": "caller1"})
        assert server.run_store.get_run(source["run_id"])["nesting"], "fixture: the source hangs below ann's caller"
        fork = (await server.service.control_run(source["run_id"], "fork", user_id="root", at_step=0))["run_id"]
        row = await settle(server.run_manager, fork)
    finally:
        await server.stop_plugin()
        _stop_cancellation_monitor()

    assert row["status"] == "succeeded" and row["user_id"] == "root" and row["nesting"] is None, row
    session = await host.sessions.load_session("root", f"sg_{fork}")
    assert not (session.get("parent_session") or {}).get("session_id"), "at the top of root's list"
    instance = await host.sessions.load_session("root", host.calls()[-1]["session"])
    assert "depth_budget" not in instance, "no budget of ann's caller"


# ------------------------------------------------------------------ R3: the session tells an end only once it is stored

@pytest.mark.parametrize("how", ["taken_over", "not_stored"])
async def test_the_run_session_tells_the_end_only_once_it_is_stored(harness, tmp_path, monkeypatch, how):
    import plugins.stategraph.engine.runner as runner_module

    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    store = harness.store
    real = store.update_run

    def at_the_end(run_id: str, **fields: Any) -> int:
        if fields.get("status") in TERMINAL_STATUSES and fields.get("fence"):
            if how == "taken_over":
                real(run_id, owner="other:2:zz")  # another process took the run just before the end write
            else:
                raise sqlite3.OperationalError("database is locked")
        return real(run_id, **fields)

    monkeypatch.setattr(store, "update_run", at_the_end)
    monkeypatch.setattr(runner_module, "END_WRITE_DELAYS", ())
    manager = harness.manager()
    run_id = await manager.start(runnable(write_machine()), params={"topic": "x"}, backend_factory=ann_backend(host))
    await until(lambda: run_id not in manager.live, what="the run's task ends")  # a lost run's task is cancelled

    assert store.get_run(run_id)["status"] == "running", "fixture: the end did not reach the row"
    messages = (await host.sessions.load_session("ann", f"sg_{run_id}"))["messages"]
    assert [m["role"] for m in messages] == ["user"], [m["content"][:40] for m in messages]


# ------------------------------------------------------------------ R4: the heartbeat leaves a finished run alone

class SlowEndBackend(FakeBackend):
    """Its run_ended waits: the run is finished and its end stored, but it is still live here."""

    def __init__(self, handlers: dict[str, Any], gate: asyncio.Event):
        super().__init__(handlers)
        self.gate = gate
        self.ending = False

    async def run_ended(self, **_: Any) -> None:
        self.ending = True
        await self.gate.wait()


async def test_the_heartbeat_does_not_take_a_finished_run_s_lease_again(harness):
    gate = asyncio.Event()
    backend = SlowEndBackend({"a": "A", "b": "B", "c": "C"}, gate)
    manager = harness.manager()
    run_id = await manager.start(runnable(CHAIN), backend=backend)
    await until(lambda: backend.ending, what="the end is stored and run_ended runs")
    assert run_id in manager.live and harness.store.get_run(run_id)["status"] == "succeeded", "fixture"

    manager.renew_leases()

    assert harness.store.get_run(run_id)["lease_until"] < utc_at(0), "the lease stays released"
    gate.set()
    await settle(manager, run_id)


# ------------------------------------------------------------------ R5: while the process stops, one end write

async def test_a_stopping_process_tries_each_end_write_once(harness, monkeypatch):
    manager = harness.manager()
    run_id = await manager.start(runnable(WAITING), backend=FakeBackend())
    await until(lambda: manager.live[run_id].ctx.status == "waiting", what="the wait")
    store = harness.store
    real = store.update_run
    tries: list[str] = []

    def locked(run_id_: str, **fields: Any) -> int:
        if fields.get("status") == "interrupted":
            tries.append(run_id_)
            raise sqlite3.OperationalError("database is locked")
        return real(run_id_, **fields)

    monkeypatch.setattr(store, "update_run", locked)
    await asyncio.wait_for(manager.shutdown(), 3)

    assert tries == [run_id]


# ------------------------------------------------------------------ R6: a stored point no longer valid holds nothing up

@pytest.mark.parametrize("action", ["terminate", "resume", "fork"])
async def test_a_run_with_a_stored_point_no_longer_valid_can_be_ended_resumed_and_forked(tmp_path, action):
    server = sg_server(tmp_path)
    server.run_store.create_run("r1", "m", runnable(WAITING).snapshot(), params={},
                                mocks={"mocks": {}, "mock_only": True}, owner="gone:1:x", lease_until=utc_at(-5),
                                status="interrupted", session_id="sg_r1",
                                debug={"breakpoints": [{"state": "w", "condition": True}, "w@exit"],
                                       "watchpoints": [{"expr": 5}]})
    try:
        answer = await server.service.control_run("r1", action)
        if action == "terminate":
            assert (answer["status"], answer["error"]["type"]) == ("cancelled", "cancelled"), answer
            assert "did not run" not in answer["error"]["message"], "its finally could run"
        else:
            run_id = answer["run_id"] if action == "fork" else "r1"
            await until(lambda: server.run_manager.live[run_id].ctx.status == "waiting", what="the run goes on")
            points = server.run_manager.live[run_id].ctx.debugger.breakpoints
            assert [(p.state, p.at) for p in points] == [("w", "exit")], "only the invalid point is dropped"
    finally:
        await server.stop_plugin()


def test_stored_points_that_are_no_list_are_dropped_not_taken_apart():
    from plugins.stategraph.engine.debugger import Debugger

    debugger = Debugger.from_state({"breakpoints": "ab", "watchpoints": "ctx.x"})

    assert (debugger.breakpoints, debugger.watchpoints) == ([], [])


# ------------------------------------------------------------------ R7: steps is checked before the action acts

async def test_a_control_with_bad_steps_does_nothing(env):
    run_id = (await env.server.service.start_run("m", params={"task": "hold on"}))["run_id"]
    await until(lambda: env.server.run_manager.live[run_id].ctx.status == "waiting", what="the wait")

    with pytest.raises(ServiceError) as refused:
        await env.server.service.control_run(run_id, "terminate", steps=0)

    assert refused.value.status == 422
    assert env.server.run_manager.live[run_id].ctx.status == "waiting", "the run was not terminated"


# ------------------------------------------------------------------ R8: the chat opens a run's session read-only

@pytest.mark.parametrize("nesting,depth", [(None, 1), ({"depth": 2, "depth_budget": 3, "session": None}, 3)],
                         ids=["top", "below_a_caller"])
async def test_a_run_s_session_is_one_the_chat_opens_read_only(harness, tmp_path, nesting, depth):
    """The shell (static/js/shell/sessions.js, show) opens a session read-only when it has a ``depth`` and its agent is
    not in the agent selector; the selector lists no private agent (app.py /agents)."""
    from agent_system.config.settings import get_tool_server_config, load_settings

    config = load_settings()
    runner = get_tool_server_config("stategraph", config).runner_agent
    assert get_tool_server_config(runner, config).metadata.visibility not in ("ui", "both"), "the runner is private"
    host = AgentHost(tmp_path / "sessions", FakeAgent("writer"))
    manager = harness.manager()
    run_id = await manager.start(runnable(write_machine()), params={"topic": "x"}, nesting=nesting,
                                 backend_factory=ann_backend(host, nesting))
    await settle(manager, run_id)

    session = await host.sessions.load_session("ann", f"sg_{run_id}")
    assert session["depth"] == depth and session["agent_name"] == runner, session


# ------------------------------------------------------------------ R9: the control tool takes steps

async def test_the_control_tool_answers_with_the_journal_rows_asked_for(tmp_path):
    server = sg_server(tmp_path, machines=MANY)
    try:
        run_id = (await server.service.start_run("many", mock_only=True))["run_id"]
        await settle(server.run_manager, run_id)

        asked = await server.control_run({"run_id": run_id, "action": "set_breakpoints", "breakpoints": [],
                                          "steps": 7})
        plain_answer = await server.control_run({"run_id": run_id, "action": "set_breakpoints", "breakpoints": []})
    finally:
        await server.stop_plugin()

    assert asked["status"] == "success" and len(asked["journal"]) == 7, asked
    assert "journal" not in plain_answer
