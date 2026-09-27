"""Regressions for the backlog of the stategraph review of 2026-09-27 (docs/backlog.md), phase 1: runtime.

Machines run through the real RunManager, service, server and facade on stores under tmp_path; only the boundary
behind the engine is faked (FakeBackend, or AgentHost's FakeAgents with real sessions on disk).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_system.config.models import AgentSystemConfig
from agent_system.core.cancellation import get_cancellation_manager

from plugins.stategraph.engine.backend import ScarabHiveBackend
from plugins.stategraph.engine.runner import RunManager
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import (AgentHost, FakeAgent, FakeBackend, Harness, runnable, settle,
                                                         tool_config, until)
from plugins.stategraph.service import ServiceError
from plugins.stategraph.tests.test_plugin_stategraph_facade import Env, final_of
from plugins.stategraph.tests.test_plugin_stategraph_server import run_tool, server  # noqa: F401 -- a fixture


@pytest.fixture
async def env(tmp_path):
    made = Env(tmp_path)
    yield made
    await made.close()

# ------------------------------------------------------------------ R1: a terminate spares running cleanup work

CLEANUP = {"m.yaml": """\
stategraph: 1
id: m
initial: a
finally: {agent: cleaner, task: clean up}
states:
  a:
    do: {agent: worker, task: go}
    transitions: [{target: done}]
  done: {type: final}
"""}


async def _cleanup_run(tmp_path, stop):
    """A run whose machine finally starts a cleanup agent watching its token; ``stop(manager, run_id)`` comes while
    it runs. What the cleanup saw, and the finally_failed traces."""
    harness = Harness(tmp_path)
    seen: dict[str, bool] = {}

    async def clean(call):
        seen["started"] = True
        for _ in range(40):  # cleanup work that watches its token, like a real agent run
            if call["token"].is_cancelled:
                seen["cut"] = True
                return "cut"
            await asyncio.sleep(0.02)
        seen["finished"] = True
        return "cleaned"

    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    host = AgentHost(tmp_path / "s", FakeAgent("worker", "ok"), FakeAgent("cleaner", clean))
    manager = RunManager(harness.store, on_cancel=server._cancel_requests)
    try:
        run_id = await manager.start(runnable(CLEANUP), backend_factory=lambda rid: ScarabHiveBackend(
            runner=host, system_config=None, session_id=f"sg_{rid}", user_id="ann"))
        await until(lambda: seen.get("started"), what="the finally agent running")
        stop(manager, run_id)
        await settle(manager, run_id)
        failed = [r for r in harness.store.rows(run_id, kinds=("trace",)) if r["status"] == "finally_failed"]
        return seen, failed
    finally:
        await manager.shutdown()
        await server.stop_plugin()
        harness.store.close()


async def test_a_terminate_does_not_cut_a_finally_agent_already_running(tmp_path):
    """§3.10: a terminate that arrives while a finally activity runs does not cut the running one -- the server's
    on_cancel cancels the run's other sub-requests, not the cleanup agent's."""
    seen, failed = await _cleanup_run(tmp_path, lambda manager, run_id: manager.control(run_id, "terminate"))

    assert (seen.get("finished"), seen.get("cut"), failed) == (True, None, []), (seen, failed)


async def test_a_cancel_of_the_caller_s_request_tree_does_not_cut_it_either(tmp_path):
    """The facade's run id is <request id>_sg...: a stop in the chat, a SAM parent's cancel, a failed job cancel the
    caller's request and so every id below it -- the cleanup agent's too, unless it is protected."""
    seen, failed = await _cleanup_run(tmp_path, lambda manager, run_id: get_cancellation_manager().cancel_request(
        run_id))

    assert (seen.get("finished"), seen.get("cut"), failed) == (True, None, []), (seen, failed)


async def test_a_protected_request_is_left_out_of_every_cascade():
    manager = get_cancellation_manager()
    ids = ("r9_001", "r9_002", "r9_002_001", "r9_0020")
    tokens = {rid: manager.create_token(rid) for rid in ids}
    manager.protect("r9_002")
    try:
        manager.cancel_sub_requests("r9")
        after_sub = {rid: t.is_cancelled for rid, t in tokens.items()}
        manager.cancel_request("r9_002")  # itself: protected too
        assert (after_sub, tokens["r9_002"].is_cancelled) == (
            {"r9_001": True, "r9_002": False, "r9_002_001": False, "r9_0020": True}, False)
        manager.unprotect("r9_002")
        manager.cancel_request("r9")
        assert all(t.is_cancelled for t in tokens.values()), "unprotected, it is cancelled like the rest"
    finally:
        manager.unprotect("r9_002")
        for rid in ids:
            manager.unregister_request(rid)


# ------------------------------------------------------------------ R2: a step belongs to the frame it was given to

PARALLEL_SUB = {"m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
initial: fan
states:
  fan:
    do:
      parallel:
        x: {machine: sub, params: {}}
        y: {machine: sub, params: {}}
    transitions: [{target: done}]
  done: {type: final}
""", "sub.yaml": """\
stategraph: 1
id: sub
initial: a
states:
  a:
    do: {tool: t1, args: {}}
    transitions: [{target: b}]
  b:
    do: {tool: t2, args: {}}
    transitions: [{target: fin}]
  fin: {type: final}
"""}


async def test_a_step_in_one_parallel_branch_is_not_taken_by_the_other(tmp_path):
    """The other branch waits at its own breakpoint: its pause must not consume the step, or the stepping branch
    runs on without a halt."""
    async def slow(call):
        await asyncio.sleep(0.05)
        return {"ok": 1}

    harness = Harness(tmp_path)
    manager = harness.manager()
    backend = FakeBackend({path: slow for path in ("fan/x/a", "fan/x/b", "fan/y/a", "fan/y/b")})
    try:
        run_id = await manager.start(runnable(PARALLEL_SUB), backend=backend, breakpoints=["a"])
        debugger = manager.live[run_id].ctx.debugger
        await until(lambda: debugger.paused is not None, what="a branch at its breakpoint")
        stepping = debugger.paused["frame"]
        manager.control(run_id, "step")
        await until(lambda: debugger.paused is not None and debugger.paused["frame"] != stepping,
                    what="the other branch at its breakpoint")
        manager.control(run_id, "continue")
        await until(lambda: debugger.paused is not None and debugger.paused["frame"] == stepping,
                    what="the stepping branch halted again")
        branch = "fan/x" if ".x/" in stepping else "fan/y"
        assert ((debugger.paused["state"], debugger.paused["hook"], debugger.paused["reason"]),
                [c["path"] for c in backend.calls if c["path"].startswith(branch)]) == (
            ("a", "exit", "step"), [f"{branch}/a"])
        manager.control(run_id, "continue")
        assert (await settle(manager, run_id))["status"] == "succeeded"
    finally:
        await harness.close()


async def test_a_step_given_to_the_other_branch_leaves_the_first_one_s_step_pending(tmp_path):
    """Both branches stand at the same breakpoint: x gets a step, then y does while x is still in its activity.
    Each halts at its own next hook -- the second step does not replace the first."""
    async def slow(call):
        await asyncio.sleep(0.3)
        return {"ok": 1}

    harness = Harness(tmp_path)
    manager = harness.manager()
    backend = FakeBackend({path: slow for path in ("fan/x/a", "fan/x/b", "fan/y/a", "fan/y/b")})
    try:
        run_id = await manager.start(runnable(PARALLEL_SUB), backend=backend, breakpoints=["a"])
        debugger = manager.live[run_id].ctx.debugger
        await until(lambda: debugger.paused is not None, what="a branch at its breakpoint")
        first = debugger.paused["frame"]
        manager.control(run_id, "step")
        await until(lambda: debugger.paused is not None and debugger.paused["frame"] != first,
                    what="the other branch at its breakpoint")
        second = debugger.paused["frame"]
        manager.control(run_id, "step")
        halts = []
        for _ in range(2):
            await until(lambda: debugger.paused is not None and debugger.paused["reason"] == "step",
                        what="a stepping branch halted")
            halts.append((debugger.paused["frame"], debugger.paused["state"], debugger.paused["hook"]))
            manager.control(run_id, "continue")
            await asyncio.sleep(0.05)
        assert sorted(halts) == sorted([(first, "a", "exit"), (second, "a", "exit")]), halts
        assert (await settle(manager, run_id))["status"] == "succeeded"
    finally:
        await harness.close()


async def test_one_pause_answered_by_a_watchpoint_does_not_halt_another_frame_again():
    """The viewer clicks pause once; frame x halts at a watchpoint first. That answers the pause: frame y's next hook
    runs on."""
    from types import SimpleNamespace

    from plugins.stategraph.engine.debugger import Debugger

    run = SimpleNamespace(ending=False, stopping=False, refresh_status=lambda: None, trace=lambda *a, **k: None)

    def frame(prefix):
        return SimpleNamespace(prefix=prefix, machine=SimpleNamespace(id="sub"), step=1, leaf=None)

    node = SimpleNamespace(name="a")
    debugger = Debugger()
    debugger.command("pause")
    watch = asyncio.ensure_future(debugger.pause(run, frame("s0/b.x/m/"), node, "watch", "watchpoint w changed", None))
    await asyncio.sleep(0.01)
    hook = asyncio.ensure_future(debugger.at_hook(run, frame("s0/b.y/m/"), node, "enter", None))
    await asyncio.sleep(0.01)
    debugger.command("continue")
    await asyncio.sleep(0.01)

    try:
        assert (hook.done(), debugger.paused) == (True, None), debugger.paused
    finally:
        debugger.release()
        await asyncio.gather(watch, hook)


# ------------------------------------------------------------------ R3: a run key is one request

@pytest.mark.parametrize("change", ["params", "mocks", "mock_only"])
async def test_a_run_key_reused_for_another_request_is_refused(env, change):
    """A mock test's outcome must not answer a live request that reuses its key, nor one with other params."""
    service = env.server.service
    same = {"params": {"task": "x"}, "mocks": {"design": {"$error": {"type": "tool_failed", "message": "m"}}},
            "mock_only": False}
    first = await service.start_run("m", run_key="k", **same)
    await settle(env.server.run_manager, first["run_id"])

    other = dict(same, **{change: {"params": {"task": "y"}, "mocks": {}, "mock_only": True}[change]})
    with pytest.raises(ServiceError) as refused:
        await service.start_run("m", run_key="k", **other)

    assert (refused.value.status, change in refused.value.message, len(env.runs())) == (409, True, 1), (
        refused.value.message)
    assert await service.start_run("m", run_key="k", **same) == {"run_id": first["run_id"], "ended": "failed"}


# ------------------------------------------------------------------ R4: tool arguments are checked, and said why

async def test_a_json_string_for_an_object_argument_is_taken(server):
    result, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "params": '{"name": "Ann"}'})

    assert (result["status"], result["output"]) == ("success", {"greeting": "Hello, Ann!"}), result


@pytest.mark.parametrize("arguments,says", [
    ({"params": "name=Ann"}, "params must be an object"),
    ({"params": ["Ann"]}, "params must be an object"),
    ({"mocks": "greet: x"}, "mocks must be an object"),
    ({"mock_only": "false"}, "mock_only must be true or false"),
    ({"max_wait": "abc"}, "max_wait must be a number"),
    ({"max_wait": 99999}, "max_wait must be a number"),
    ({"wait": "later"}, "wait must be one of"),
])
async def test_a_wrong_argument_is_refused_with_what_to_send_and_starts_nothing(server, arguments, says):
    result, closing = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", **arguments})

    assert (result["status"], says in result.get("error", ""), closing.phase.value) == ("error", True, "error"), result
    assert server.run_store.list_runs() == [], "a refused start left a run row behind"


@pytest.mark.parametrize("files", ["hello.yaml: x", ["hello.yaml"]])
async def test_files_that_are_no_object_are_refused(server, files):
    for tool in ("stategraph_validate_machine", "stategraph_save_machine"):
        result, _ = await run_tool(server, tool, {"files": files})
        assert (result["status"], "files must be an object" in result.get("error", "")) == ("error", True), (
            tool, result)


# ------------------------------------------------------------------ R5: debug names are checked against the machine

@pytest.mark.parametrize("breakpoints,says", [
    (["gret"], "no state 'gret'"),
    ([{"state": "greet", "machine": "helo"}], "no machine 'helo'"),
    (["done"], "never stops at enter"),  # a top-level final: the run ends as it enters it
])
async def test_a_breakpoint_naming_no_state_is_refused_at_the_start(server, breakpoints, says):
    result, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "breakpoints": breakpoints})

    assert (result["status"], says in result.get("error", ""), server.run_store.list_runs()) == ("error", True, []), (
        result)


async def test_set_breakpoints_and_run_to_naming_no_state_are_refused(server):
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "approval", "wait": "background"})
    run_id = started["run_id"]
    await until(lambda: (server.run_store.get_run(run_id) or {}).get("status") == "waiting", what="the wait state")

    for arguments, says in (({"action": "set_breakpoints", "breakpoints": ["aproved"]}, "no state 'aproved'"),
                            ({"action": "run_to", "state": "aproved"}, "no state 'aproved'"),
                            ({"action": "set_breakpoints", "breakpoints": ["approved"]}, "never stops at enter"),
                            ({"action": "run_to", "state": "approved"}, "never stops at enter")):
        result, _ = await run_tool(server, "stategraph_control_run", {"run_id": run_id, **arguments})
        assert (result["status"], result.get("error_type"), says in result.get("error", "")) == (
            "error", "http_422", True), (arguments, result)
    kept, _ = await run_tool(server, "stategraph_control_run", {"run_id": run_id, "action": "set_breakpoints",
                                                                "breakpoints": ["waiting_for_ok@exit"]})
    assert kept["status"] == "success", kept


async def test_a_mock_no_activity_used_is_named_in_the_answer(server):
    result, _ = await run_tool(server, "stategraph_run_machine", {
        "machine_id": "hello", "mocks": {"gret": "Hi"}})

    assert (result["output"], result.get("mocks_unused")) == ({"greeting": "Hello, world!"}, ["gret"]), result


# ------------------------------------------------------------------ R6, R7: the facade's session -> run map

FLAKY = """\
import os
from plugins.stategraph.kinds import ActivityError
MARK = os.environ["SG_BACKLOG_MARK"]
def design(task):
    if not os.path.exists(MARK):
        open(MARK, "w").close()
        raise ActivityError("agent_failed", "provider hiccup")
    return {"story_id": 812, "title": task}
def note_end(reason):
    return reason
"""


async def test_a_known_session_id_does_not_hand_another_user_the_run(env, monkeypatch):
    from agent_system.config.models import AgentSystemConfig

    monkeypatch.setattr(StateGraphServer, "_is_admin", staticmethod(lambda user_id: False))
    env.server.system_config = AgentSystemConfig(auth={"enabled": True})
    env.agent._session_tracker.set_session_metadata("alice_s", {"user_id": "alice"})
    first = final_of(await env.ask("Nachtzug", request_id="r-alice", session_id="alice_s"))
    assert first["type"] == "final", first
    other = env.fresh_agent()
    other._session_tracker.set_session_metadata("alice_s", {"user_id": "bob"})

    answer = final_of([e async for e in other.run_events("gib her", request_id="r-bob", session_id="alice_s")])

    assert (answer["type"], "another user's run" in answer.get("message", "")) == ("error", True), answer


async def test_the_same_request_in_the_same_session_runs_anew_after_a_transient_failure(env, tmp_path, monkeypatch):
    monkeypatch.setenv("SG_BACKLOG_MARK", str(tmp_path / "mark"))
    (tmp_path / "machines" / "m.py").write_text(FLAKY, encoding="utf-8")
    first = final_of(await env.ask("Nachtzug", request_id="job-1", session_id="s1"))
    assert first["type"] == "error" and "agent_failed" in first["message"], first

    again = final_of(await env.ask("Nachtzug", request_id="job-1", session_id="s1"))

    assert (again["type"], again.get("story_id"), len(env.runs())) == ("final", 812, 2), again


# ------------------------------------------------------------------ R8: a machine agent's config is checked early

async def test_machine_agents_that_cannot_run_are_named_after_the_start(env, caplog):
    from agent_system.config.models import AgentSystemConfig

    def agent(**keys):
        return {"type": "stategraph_machine", "enabled": True, "agent_config": {"llm_profile": "normal"}, **keys}

    env.server.system_config = AgentSystemConfig(plugins={"servers": {
        "fine": agent(machine="m"),
        "typo": agent(machine="mm"),
        "wrong_param": agent(machine="m", task_param="request"),
        "json_input": agent(machine="m", input="json"),
        "elsewhere": agent(machine="mm", stategraph="other_instance"),
        "off": {**agent(machine="mm"), "enabled": False},
    }})

    problems = env.server.facade_problems()
    await env.server._report_facades()

    assert sorted(problems) == ["typo", "wrong_param"], problems
    assert "no machine 'mm'" in problems["typo"][0]
    assert problems["wrong_param"] == ["task_param 'request' is no param of m (it has: task)",
                                       "m requires task: neither in params nor the task_param"]
    assert [r.getMessage()[:40] for r in caplog.records if r.levelname == "ERROR"] == [
        "stategraph: machine agent typo cannot ru", "stategraph: machine agent wrong_param ca"]


@pytest.mark.parametrize("arguments,says", [
    ({"params": ["Ann"]}, "params must be an object"),
    ({"mocks": "greet: x"}, "mocks must be an object"),
    ({"mock_only": "false"}, "mock_only must be true or false"),
])
async def test_the_service_refuses_arguments_of_the_wrong_type_for_every_caller(server, arguments, says):
    """REST and the facade reach start_run without the tool's argument checks."""
    with pytest.raises(ServiceError) as refused:
        await server.service.start_run("hello", **arguments)

    assert (refused.value.status, says in refused.value.message, server.run_store.list_runs()) == (422, True, [])


async def test_the_service_refuses_files_that_are_no_object(server):
    with pytest.raises(ServiceError) as refused:
        server.service.validate(files=["hello.yaml"])

    assert refused.value.status == 422 and "files must be an object" in refused.value.message


async def test_a_fork_onto_the_current_file_drops_a_breakpoint_its_state_lost(server, tmp_path):
    """The source's own breakpoints come along; one naming a state the current file renamed is dropped, not a
    reason to refuse the fork."""
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "breakpoints": ["greet@exit"]})
    run_id = started["run_id"]
    assert started["run_status"] == "paused", started
    await run_tool(server, "stategraph_control_run", {"run_id": run_id, "action": "continue"})
    await settle(server.run_manager, run_id)
    hello = tmp_path / "machines" / "hello.yaml"
    text = hello.read_text(encoding="utf-8")
    hello.write_text(text.replace("initial: greet", "initial: hi").replace("  greet:" + chr(10), "  hi:" + chr(10))
                     .replace("done", "finished"), encoding="utf-8")

    forked, _ = await run_tool(server, "stategraph_control_run", {"run_id": run_id, "action": "fork", "at_step": 0,
                                                                  "definition": "current"})

    assert forked["status"] == "success", forked
    row = await settle(server.run_manager, forked["run_id"])
    assert (row["status"], row["final_state"], (row.get("debug") or {}).get("breakpoints")) == (
        "succeeded", "finished", []), row


async def test_expected_versions_sent_as_a_json_string_are_taken(server):
    machine, _ = await run_tool(server, "stategraph_get_machine", {"machine_id": "hello"})
    text = machine["files"]["hello.yaml"] + "# saved again\n"

    saved, _ = await run_tool(server, "stategraph_save_machine", {
        "machine_id": "hello", "files": {"hello.yaml": text}, "expected_versions": json.dumps(machine["versions"])})

    assert saved["status"] == "success", saved
