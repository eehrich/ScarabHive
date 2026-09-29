"""The constructs of design §10 that writer v6 needs: sg.tool() in call activities (more follow).

Every machine runs through the real RunManager on a RunStore under tmp_path; only the boundary
behind the engine is FakeBackend. A "crash" is a real ``RunManager.shutdown()``.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from plugins.stategraph.kinds import ActivityError
from plugins.stategraph.tests.stategraph_testkit import (FASTAPI_PY314, FakeBackend, Harness, activity_rows, crash_in,
                                                         held, resume, runnable, sequence, settle, until, utc_at)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


def with_call(companion: str, *, call: str = "work", retry: str = "") -> dict[str, str]:
    """m runs one call activity ``c`` and ends with its result as output."""
    return {"m.py": companion, "m.yaml": f"""\
stategraph: 1
id: m
python: m.py
context: {{got: null}}
initial: c
states:
  c:
    do: {{call: {call}{retry}}}
    transitions:
      - target: done
        effect: ctx.got = out
      - trigger: error
        target: failed
        effect: ctx.got = [error.type, error.message]
  done: {{type: final, output: "{{{{ ctx.got }}}}"}}
  failed: {{type: final, status: failed, output: "{{{{ ctx.got }}}}"}}
"""}


async def run(harness: Harness, files: dict[str, str], backend: FakeBackend, **start) -> dict:
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend, **start)
    return await settle(manager, run_id)


# ------------------------------------------------------------------ sg.tool()

TWO_CALLS = """\
async def work(sg):
    first = await sg.tool("store_read", {"doc": "synopsis"})
    second = await sg.tool("store_write", {"doc": "synopsis", "value": first["value"] + 1})
    return {"first": first, "second": second}
"""


async def test_sg_tool_calls_are_child_activities_of_the_call(harness):
    backend = FakeBackend({"c/store_read": {"value": 41}, "c/store_write": {"status": "ok"}})
    row = await run(harness, with_call(TWO_CALLS), backend)

    assert row["output"] == {"first": {"value": 41}, "second": {"status": "ok"}}, row
    assert [(c["tool"], c["args"]) for c in backend.calls] == [
        ("store_read", {"doc": "synopsis"}), ("store_write", {"doc": "synopsis", "value": 42})]
    rows = activity_rows(harness.store, row["id"])
    assert {key: rows[key]["data"]["path"] for key in ("s0/t.0", "s0/t.1")} == {
        "s0/t.0": "c/store_read", "s0/t.1": "c/store_write"}


async def test_sg_tool_arguments_are_data_not_templates(harness):
    companion = 'async def work(sg):\n    return await sg.tool("echo", {"text": "{{ 1/0 }}"})\n'
    backend = FakeBackend({"c/echo": lambda call: call["args"]["text"]})
    row = await run(harness, with_call(companion), backend)

    assert (row["status"], row["output"]) == ("succeeded", "{{ 1/0 }}"), row


RECONCILE = """\
async def work(sg):
    brief = await sg.tool("store_read", {"doc": "brief"})
    try:
        made = await sg.tool("story_create", {"title": brief["title"]})
    except sg.Error as exc:
        if exc.type != "interrupted":
            raise
        made = await sg.tool("story_find", {"title": brief["title"]}, idempotent=True)
    return made
"""


async def test_a_resumed_call_replays_its_finished_tool_calls_and_reconciles_the_interrupted_one(harness):
    files = with_call(RECONCILE)
    gate = asyncio.Event()
    first = FakeBackend({"c/store_read": {"title": "Nachtzug"}, "c/story_create": held(gate, {"id": 1})})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("c/story_create") == 1, what="story_create in flight")
    await manager.shutdown()

    second = FakeBackend({"c/store_read": {"title": "WRONG"}, "c/story_create": {"id": 2},
                          "c/story_find": {"id": 1}})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["output"]) == ("succeeded", {"id": 1}), row
    assert second.count("c/store_read") == 0, "a finished inner call ran again instead of replaying"
    assert second.count("c/story_create") == 0, "a non-idempotent inner call in flight at the crash ran again"
    assert second.calls[0]["args"] == {"title": "Nachtzug"}, "story_find did not see the replayed read"


async def test_an_idempotent_inner_call_in_flight_at_a_crash_runs_again(harness):
    companion = 'async def work(sg):\n    return await sg.tool("store_read", {"doc": "d"}, idempotent=True)\n'
    files = with_call(companion)
    first = FakeBackend({"c/store_read": held(asyncio.Event(), "never")})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("c/store_read") == 1, what="the read in flight")
    await manager.shutdown()

    second = FakeBackend({"c/store_read": "read again"})
    row = await resume(harness, run_id, second)

    assert (row["output"], second.count("c/store_read")) == ("read again", 1), row


async def test_inner_calls_of_a_retried_call_run_again_under_the_new_attempt(harness):
    failing_once = {"n": 0}

    def write(call):
        failing_once["n"] += 1
        return ActivityError("tool_failed", "store busy") if failing_once["n"] == 1 else {"status": "ok"}

    backend = FakeBackend({"c/store_read": {"value": 1}, "c/store_write": write})
    row = await run(harness, with_call(TWO_CALLS, retry=", retry: {attempts: 2, backoff: 0}"), backend)

    assert row["status"] == "succeeded", row
    assert (backend.count("c/store_read"), backend.count("c/store_write")) == (2, 2)
    keys = sorted(k for k in activity_rows(harness.store, row["id"]) if "/t." in k)
    assert keys == ["s0/a2/t.0", "s0/a2/t.1", "s0/t.0", "s0/t.1"], keys


async def test_mocks_answer_inner_calls_by_path_in_call_order(harness):
    companion = ('async def work(sg):\n'
                 '    return [await sg.tool("echo", {"n": n}) for n in range(3)]\n')
    manager = harness.manager()
    run_id = await manager.start(runnable(with_call(companion)), mock_only=True,
                                 mocks={"c/echo": {"$visits": ["one", "two", "three"]}})
    row = await settle(manager, run_id)

    assert (row["status"], row["output"]) == ("succeeded", ["one", "two", "three"]), row


async def test_changed_arguments_of_a_replayed_inner_call_diverge(harness):
    files = with_call(TWO_CALLS)
    first = FakeBackend({"c/store_read": {"value": 1}, "c/store_write": held(asyncio.Event(), "never")})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("c/store_write") == 1, what="the write in flight")
    await manager.shutdown()
    snapshot = harness.store.get_run(run_id)["definition"]
    snapshot["files"]["m.py"] = snapshot["files"]["m.py"].replace('{"doc": "synopsis"}', '{"doc": "beats"}', 1)
    harness.store.update_run(run_id, definition=snapshot)

    second = FakeBackend({"c/store_read": {"value": 1}, "c/store_write": {"status": "ok"}})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["error"]["type"]) == ("failed", "diverged"), row
    assert second.calls == []


async def test_sg_tool_after_the_call_returned_is_refused(harness):
    companion = """\
KEPT = []


def keep(sg):
    KEPT.append(sg)
    return "kept"


async def work(sg):
    return await KEPT[0].tool("echo", {})
"""
    files = with_call(companion, call="keep")
    files["m.yaml"] = files["m.yaml"].replace("""\
    transitions:
      - target: done
        effect: ctx.got = out""", """\
    transitions:
      - target: later
  later:
    do: {call: work}
    transitions:
      - target: done
        effect: ctx.got = out""", 1)
    backend = FakeBackend({"later/echo": "must not run"})
    row = await run(harness, files, backend)

    assert row["output"][0] == "call_failed" and "after the call returned" in row["output"][1], row
    assert backend.calls == []


# ------------------------------------------------------------------ finally (§3.9)

FINALLY_MACHINE = """\
stategraph: 1
id: m
context: {n: 0}
initial: comp
finally: {tool: log_end, args: {reason: "{{ ending.reason }}", error: "{{ ending.error and ending.error['type'] }}"}}
states:
  comp:
    initial: a
    finally:
      tool: log_comp
      args: {reason: "{{ ending.reason }}", state: "{{ ending.state }}", n: "{{ ctx.n }}",
             error: "{{ ending.error and ending.error['type'] }}"}
    states:
      a:
        do: {agent: w, task: t}
        transitions:
          - target: after
            effect: ctx.n = 1
  after:
    do: {agent: w, task: after}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.n }}"}
"""


def finals(backend: FakeBackend) -> list[tuple[str, dict]]:
    return [(c["path"], c["args"]) for c in backend.calls if c["kind"] == "call_tool"]


async def test_a_state_s_finally_runs_after_the_transition_that_left_it_committed(harness):
    backend = FakeBackend({"a": "A", "after": "B", "comp/finally": "ok", "m.finally": "ok"})
    row = await run(harness, {"m.yaml": FINALLY_MACHINE}, backend)

    assert (row["status"], row["output"]) == ("succeeded", 1), row
    assert [c["path"] for c in backend.calls] == ["a", "comp/finally", "after", "m.finally"]
    assert finals(backend) == [
        ("comp/finally", {"reason": "transition", "state": "comp", "n": 1, "error": None}),
        ("m.finally", {"reason": "finished", "error": None})]


async def test_finally_runs_innermost_first_when_an_unhandled_error_ends_the_frame(harness):
    backend = FakeBackend({"a": ActivityError("agent_failed", "no"), "comp/finally": "ok", "m.finally": "ok"})
    row = await run(harness, {"m.yaml": FINALLY_MACHINE}, backend)

    assert (row["status"], row["error"]["type"]) == ("failed", "agent_failed"), row
    assert finals(backend) == [
        ("comp/finally", {"reason": "failed", "state": "comp", "n": 0, "error": "agent_failed"}),
        ("m.finally", {"reason": "failed", "error": "agent_failed"})]


async def test_finally_runs_when_the_run_is_terminated(harness):
    backend = FakeBackend({"a": held(asyncio.Event()), "comp/finally": "ok", "m.finally": "ok"})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": FINALLY_MACHINE}), backend=backend)
    await until(lambda: backend.count("a") == 1, what="a in flight")

    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert [(path, args["reason"]) for path, args in finals(backend)] == [("comp/finally", "cancelled"),
                                                                           ("m.finally", "cancelled")]


async def test_finally_does_not_run_when_the_process_stops_but_after_the_resume(harness):
    first = FakeBackend({"a": held(asyncio.Event()), "comp/finally": "ok", "m.finally": "ok"})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": FINALLY_MACHINE}), backend=first)
    await until(lambda: first.count("a") == 1, what="a in flight")
    await manager.shutdown()
    assert harness.store.get_run(run_id)["status"] == "interrupted"
    assert finals(first) == [], "the process stopped: the run resumes later, nothing was left yet"

    second = FakeBackend({"a": "A", "after": "B", "comp/finally": "ok", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "succeeded", row
    assert [path for path, _ in finals(second)] == ["comp/finally", "m.finally"]


async def test_a_failing_finally_is_traced_and_does_not_change_the_outcome(harness):
    backend = FakeBackend({"a": "A", "after": "B", "comp/finally": ActivityError("tool_failed", "forum down"),
                           "m.finally": "ok"})
    row = await run(harness, {"m.yaml": FINALLY_MACHINE}, backend)

    assert (row["status"], row["output"]) == ("succeeded", 1), row
    traces = [r for r in harness.store.rows(row["id"], kinds=("trace",)) if r["status"] == "finally_failed"]
    assert [(t["state"], t["data"]["error"]["type"]) for t in traces] == [("comp/finally", "tool_failed")]


async def test_a_terminate_interrupted_by_a_crash_is_completed_by_the_resume(harness):
    """The process dies while the finally of a terminate runs: the resume replays and ends the run cancelled."""
    files = {"m.yaml": FINALLY_MACHINE.replace("tool: log_comp", "idempotent: true\n      tool: log_comp")}
    first = FakeBackend({"a": held(asyncio.Event()), "comp/finally": held(asyncio.Event()), "m.finally": "ok"})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("a") == 1, what="a in flight")
    manager.control(run_id, "terminate")
    await until(lambda: first.count("comp/finally") == 1, what="the finally in flight")
    await manager.shutdown()
    assert harness.store.get_run(run_id)["status"] == "interrupted", "a stop mid-terminate must stay resumable"

    second = FakeBackend({"a": "must not run", "after": "must not run", "comp/finally": "ok", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "cancelled", row
    assert second.count("a") == 0 and second.count("after") == 0
    assert [(path, args["reason"]) for path, args in finals(second)] == [("comp/finally", "cancelled"),
                                                                          ("m.finally", "cancelled")]


async def test_terminate_of_an_interrupted_run_runs_its_finally(tmp_path):
    """Service path: a run no process runs is resumed into its termination instead of being marked cancelled."""
    from agent_system.config.models import AgentSystemConfig
    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config

    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    (tmp_path / "machines" / "m.yaml").write_text(FINALLY_MACHINE, encoding="utf-8")
    try:
        mocks = {"a": {"$visits": ["A"]}, "comp/finally": "ok", "m.finally": "ok"}
        started = await server.service.start_run("m", mock_only=True, mocks=mocks, pause_at_start=True)
        run_id = started["run_id"]
        await until(lambda: server.run_manager.live[run_id].ctx.status == "paused", what="the pause")
        await server.run_manager.shutdown()
        server.run_manager._stopping = False
        assert server.run_store.get_run(run_id)["status"] == "interrupted"

        await server.service.control_run(run_id, "terminate")
        row = await settle(server.run_manager, run_id)

        assert row["status"] == "cancelled", row
        paths = [r["key"] for r in server.run_store.rows(run_id, kinds=("activity",))]
        assert "end.cancelled.comp" in paths and "end.cancelled.finally" in paths, paths
    finally:
        await server.stop_plugin()


# ------------------------------------------------------------------ validation of finally

def test_finally_is_validated_like_do_and_only_on_states():
    from plugins.stategraph.tests.stategraph_testkit import found, validate

    tree = validate({"m.yaml": """\
stategraph: 1
id: m
initial: a
finally: {tool: t, args: {x: "{{ ending.nope }}"}}
states:
  a:
    finally: {agnt: w}
    transitions: [{target: c}]
  c:
    type: choice
    finally: {tool: t}
    transitions: [{target: done, guard: else}]
  done: {type: final}
"""})
    messages = [(p.code, p.path, p.message) for p in tree.problems if p.level == "error"]
    assert any(code == "SG004" and path == "finally.args.x" and "nope" in msg for code, path, msg in messages), messages
    assert any(code == "SG005" and path.startswith("states.a.finally") for code, path, _ in messages), messages
    assert any(code == "SG003" and path == "states.c.finally" for code, path, _ in messages), messages
    assert not found(validate({"m.yaml": FINALLY_MACHINE}), "SG004")


def test_ending_is_bound_only_in_finally_activities():
    from plugins.stategraph.tests.stategraph_testkit import validate

    tree = validate({"m.yaml": """\
stategraph: 1
id: m
initial: a
states:
  a:
    do: {tool: t, args: {x: "{{ ending.reason }}"}}
    transitions:
      - target: done
        effect: |
          for ending in [1]:
              ctx.n = ending
  done: {type: final}
"""})
    errors = [(p.path, p.message) for p in tree.problems if p.level == "error"]
    assert [path for path, message in errors if "'ending' is not bound" in message] == ["states.a.do.args.x"], errors


# ------------------------------------------------------------------ resources (§2.9)

RESOURCES_MACHINE = """\
stategraph: 1
id: m
context: {seen: null, copied: null}
resources:
  store:
    open: {tool: ns_open, args: {origin: "{{ run.origin }}"}}
    fork: {tool: ns_copy, args: {source: "{{ fork_source }}"}}
    close: {tool: ns_close, args: {ns: "{{ resources.store }}", reason: "{{ ending.reason }}"}}
  forum:
    open: {tool: forum_open, args: {ns: "{{ resources.store }}"}}
    close: {tool: forum_close, args: {group: "{{ resources.forum }}"}}
vars: {json_namespace: "{{ resources.store }}"}
initial: a
states:
  a:
    do: {agent: w, task: "write into {{ resources.store }}"}
    transitions:
      - target: b
        effect: |
          ctx.seen = out
          ctx.copied = resources.store
  b:
    do: {agent: w, task: "check {{ resources.store }}"}
    transitions: [{target: c}]
  c:
    do: {agent: w, task: "finish {{ resources.store }}"}
    transitions: [{target: done}]
  done: {type: final, output: "{{ ctx.seen }}"}
"""


def resource_backend(namespace: str = "ns_source", **more) -> FakeBackend:
    return FakeBackend({"resources/store/open": namespace, "resources/store/fork": "ns_forked",
                        "resources/forum/open": "group_1", "resources/forum/close": "ok",
                        "resources/store/close": "ok", "a": "A", "b": "B", "c": "C", **more})


async def test_resources_open_in_order_are_in_scope_and_close_in_reverse(harness):
    backend = resource_backend()
    row = await run(harness, {"m.yaml": RESOURCES_MACHINE}, backend)

    assert (row["status"], row["output"]) == ("succeeded", "A"), row
    assert [c["path"] for c in backend.calls] == ["resources/store/open", "resources/forum/open", "a", "b", "c",
                                                  "resources/forum/close", "resources/store/close"]
    by_path = {c["path"]: c for c in backend.calls}
    assert by_path["resources/forum/open"]["args"] == {"ns": "ns_source"}
    assert (by_path["a"]["task"], by_path["a"]["vars"]) == ("write into ns_source", {"json_namespace": "ns_source"})
    assert by_path["resources/store/close"]["args"] == {"ns": "ns_source", "reason": "finished"}
    assert by_path["resources/forum/close"]["args"] == {"group": "group_1"}


async def test_a_resumed_run_does_not_open_its_resources_again(harness):
    first = resource_backend(b=held(asyncio.Event()))
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": RESOURCES_MACHINE}), backend=first)
    await until(lambda: first.count("b") == 1, what="b in flight")
    await manager.shutdown()
    assert first.count("resources/store/close") == 0, "a stopping process does not close"

    second = resource_backend(namespace="ns_other")
    row = await resume(harness, run_id, second)

    assert row["status"] == "succeeded", row
    assert second.count("resources/store/open") == 0 and second.count("resources/forum/open") == 0
    assert second.calls[0]["task"] == "check ns_source", "the resumed run must use the journaled resource value"


async def test_a_fork_forks_its_resources_and_replays_its_prefix(harness):
    manager = harness.manager()
    source_id = await manager.start(runnable({"m.yaml": RESOURCES_MACHINE}), backend=resource_backend())
    await settle(manager, source_id)

    forked = resource_backend()
    fork_id = await manager.fork(source_id, at_step=2, backend=forked)
    row = await settle(manager, fork_id)

    assert row["status"] == "succeeded", row
    by_path = {c["path"]: c for c in forked.calls}
    assert by_path["resources/store/fork"]["args"] == {"source": "ns_source"}
    assert by_path["resources/forum/open"]["args"] == {"ns": "ns_forked"}, "no fork hook: opened afresh"
    assert forked.count("a") + forked.count("b") == 0, (
        "the prefix replays although its tasks -- and ctx.copied in its step hashes -- name the source's namespace")
    assert by_path["c"]["task"] == "finish ns_forked"
    assert by_path["resources/store/close"]["args"]["ns"] == "ns_forked"


async def test_a_resource_that_fails_to_open_fails_the_run_and_closes_the_ones_opened(harness):
    backend = resource_backend(**{"resources/forum/open": ActivityError("tool_failed", "forum down")})
    row = await run(harness, {"m.yaml": RESOURCES_MACHINE}, backend)

    assert (row["status"], row["error"]["type"]) == ("failed", "tool_failed"), row
    assert "resources.forum" in row["error"]["message"]
    assert [c["path"] for c in backend.calls] == ["resources/store/open", "resources/forum/open",
                                                  "resources/store/close"]


def test_resources_are_validated():
    from plugins.stategraph.tests.stategraph_testkit import validate

    uses = """\
initial: a
states:
  a:
    do: {tool: t, args: {x: "{{ resources.%s }}"}}
    transitions: [{target: done}]
  done: {type: final}
"""
    declared = validate({"m.yaml": "stategraph: 1\nid: m\nresources:\n  store:\n"
                                   "    open: {tool: t, args: {x: \"{{ fork_source }}\"}}\n" + uses % "stor"})
    errors = [(p.path, p.message) for p in declared.problems if p.level == "error"]
    assert any(path == "resources.store.open.args.x" and "'fork_source' is not bound" in msg for path, msg in errors)
    assert any(path == "states.a.do.args.x" and "resources has no 'stor'" in msg for path, msg in errors), errors

    undeclared = validate({"m.yaml": "stategraph: 1\nid: m\n" + uses % "store"})
    assert any("'resources' is not bound" in p.message for p in undeclared.problems), "no resources declared"


async def test_a_transition_rolled_back_does_not_run_the_finally_of_the_state_it_did_not_leave(harness):
    files = {"m.yaml": FINALLY_MACHINE.replace("            effect: ctx.n = 1", "            effect: ctx.n = 1 / 0")}
    backend = FakeBackend({"a": "A", "comp/finally": "ok", "m.finally": "ok"})
    row = await run(harness, files, backend)

    assert (row["status"], row["error"]["type"]) == ("failed", "action_failed"), row
    assert [(path, args["reason"]) for path, args in finals(backend)] == [("comp/finally", "failed"),
                                                                           ("m.finally", "failed")]


PURE_PATH = """\
stategraph: 1
id: m
initial: x
finally: {tool: log_end, args: {reason: "{{ ending.reason }}"}}
states:
  x:
    transitions: [{target: done}]
  done: {type: final}
"""


async def test_a_terminate_carried_into_a_resume_also_stops_a_path_without_activities(harness):
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": PURE_PATH}), backend=FakeBackend(), pause_at_start=True)
    await until(lambda: manager.live[run_id].ctx.status == "paused", what="the pause")
    await manager.shutdown()

    backend = FakeBackend({"m.finally": "ok"})
    resumed = harness.manager()
    await resumed.resume(run_id, backend=backend, cancel=True)
    row = await settle(resumed, run_id)

    assert row["status"] == "cancelled", row
    assert finals(backend) == [("m.finally", {"reason": "cancelled"})]


# ------------------------------------------------------------------ the run's token (§5.8)

async def test_a_cancel_that_reaches_the_run_s_token_terminates_the_run_with_its_finally(harness):
    """A caller above the run is cancelled (its request id prefixes the run id): the run ends terminated."""
    from agent_system.core.cancellation import get_cancellation_manager

    backends: list[FakeBackend] = []

    def with_token(run_id: str) -> FakeBackend:
        backend = FakeBackend({"a": held(asyncio.Event()), "comp/finally": "ok", "m.finally": "ok"})
        backend.token = get_cancellation_manager().create_token(run_id)
        backends.append(backend)
        return backend

    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": FINALLY_MACHINE}), backend_factory=with_token,
                                 run_id="book7_sub_x1_sg1")
    await until(lambda: backends and backends[0].count("a") == 1, what="a in flight")

    get_cancellation_manager().cancel_request("book7")
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert [(path, args["reason"]) for path, args in finals(backends[0])] == [("comp/finally", "cancelled"),
                                                                              ("m.finally", "cancelled")]
    assert not [r for r in activity_rows(harness.store, run_id).values() if r["status"] == "error"], \
        "no activity may fail as agent_failed: the run was terminated"


# ------------------------------------------------------------------ vars as one template (the v6 machine passes ctx.vars)

VARS_MACHINE = """\
stategraph: 1
id: m
context: {vars: {brief: Nachtzug, genre_thriller: true, genre_horror: false}, bad: [1]}
vars: {phase: synopsis}
initial: a
states:
  a:
    do: {agent: w, task: t, vars: "{{ {**ctx.vars, 'aufgabe': 'idee'} }}"}
    transitions:
      - target: done
      - trigger: error
        target: failed
        effect: ctx.bad = [error.type, error.message]
  done: {type: final}
  failed: {type: final, status: failed, output: "{{ ctx.bad }}"}
"""


async def test_vars_as_one_template_passes_the_whole_object(harness):
    backend = FakeBackend({"a": "A"})
    row = await run(harness, {"m.yaml": VARS_MACHINE}, backend)

    assert row["status"] == "succeeded", row
    assert backend.calls[0]["vars"] == {"phase": "synopsis", "brief": "Nachtzug", "genre_thriller": True,
                                        "genre_horror": False, "aufgabe": "idee"}


async def test_vars_that_render_to_no_object_are_template_failed(harness):
    files = {"m.yaml": VARS_MACHINE.replace("{**ctx.vars, 'aufgabe': 'idee'}", "ctx.bad")}
    backend = FakeBackend({"a": "A"})
    row = await run(harness, files, backend)

    assert row["output"][0] == "template_failed" and "object of names" in row["output"][1], row
    assert backend.calls == []


def test_vars_must_be_a_map_or_exactly_one_template():
    from plugins.stategraph.tests.stategraph_testkit import validate

    tree = validate({"m.yaml": VARS_MACHINE.replace("vars: {phase: synopsis}", 'vars: "phase {{ 1 }}"')})
    assert [(p.code, p.path) for p in tree.problems if p.level == "error"] == [("SG005", "vars")], tree.problems


# ================================================================== review of the constructs (findings A1-A12, B1)

TOOL_FINALLY = FINALLY_MACHINE.replace("        do: {agent: w, task: t}", "        do: {tool: make_row}").replace(
    "tool: log_comp", "idempotent: true\n      tool: log_comp")


async def test_a_journaled_terminate_over_an_in_flight_tool_ends_cancelled_not_failed(harness):
    """A1: the resume carries the terminate out BEFORE the tool in flight would end as interrupted."""
    first = FakeBackend({"a": held(asyncio.Event()), "comp/finally": held(asyncio.Event()), "m.finally": "ok"})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": TOOL_FINALLY}), backend=first)
    await until(lambda: first.count("a") == 1, what="the tool in flight")
    manager.control(run_id, "terminate")
    await until(lambda: first.count("comp/finally") == 1, what="the finally in flight")
    await manager.shutdown()

    second = FakeBackend({"a": "must not run", "comp/finally": "ok", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "cancelled", row
    assert [(path, args["reason"]) for path, args in finals(second)] == [("comp/finally", "cancelled"),
                                                                          ("m.finally", "cancelled")]


BRANCHES = {"m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
initial: p
states:
  p:
    do:
      parallel:
        x: {machine: sub}
        y: {machine: sub}
    transitions: [{target: done}]
  done: {type: final}
""", "sub.yaml": """\
stategraph: 1
id: sub
finally: {tool: tidy}
initial: w
states:
  w:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_a_terminate_lets_the_finally_of_parallel_branches_finish(harness):
    """A2: the branches get ONE cancel -- a second would cut their finally activities."""
    def tidy(seconds):
        async def handler(call):
            await asyncio.sleep(seconds)
            return "tidied"
        return handler

    # x ends first and wakes the join while y still tidies: the join must not cancel y a second time
    backend = FakeBackend({"p/x/w": held(asyncio.Event()), "p/y/w": held(asyncio.Event()),
                           "p/x/sub.finally": tidy(0.05), "p/y/sub.finally": tidy(0.4)})
    manager = harness.manager()
    run_id = await manager.start(runnable(BRANCHES), backend=backend)
    await until(lambda: backend.count("p/x/w") == 1 and backend.count("p/y/w") == 1, what="both branches")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    tidied = [r for r in activity_rows(harness.store, run_id).values() if r["data"]["path"].endswith("sub.finally")]
    assert sorted(r["status"] for r in tidied) == ["done", "done"], tidied
    assert not [path for path in backend.cancelled if path.endswith("finally")]


async def test_terminate_twice_does_not_cut_the_running_finally(harness):
    """B1: a second terminate (the facade's bridge after the token watcher, a double click) is a no-op."""
    gate = asyncio.Event()
    backend = FakeBackend({"a": held(asyncio.Event()), "comp/finally": held(gate, "ok"), "m.finally": "ok"})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": FINALLY_MACHINE}), backend=backend)
    await until(lambda: backend.count("a") == 1, what="a in flight")
    manager.control(run_id, "terminate")
    await until(lambda: backend.count("comp/finally") == 1, what="the finally in flight")
    manager.control(run_id, "terminate")
    gate.set()
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert [path for path, _ in finals(backend)] == ["comp/finally", "m.finally"]


FORK_OUTPUTS = {"m.yaml": """\
stategraph: 1
id: m
context: {made: null}
resources:
  store:
    open: {tool: ns_open}
    fork: {tool: ns_copy, args: {source: "{{ fork_source }}"}}
initial: a
states:
  a:
    do: {tool: make_doc, args: {ns: "{{ resources.store }}"}}
    transitions:
      - target: b
        effect: ctx.made = out
  b:
    do: {tool: use_doc, args: {doc: "{{ ctx.made }}"}}
    transitions: [{target: c}]
  c:
    do: {tool: finish, args: {ns: "{{ resources.store }}"}}
    transitions: [{target: done}]
  done: {type: final}
"""}


async def test_a_fork_replays_outputs_that_hold_the_source_s_resource_value(harness):
    """A3: a recorded output names the source's namespace; the fork's hashes take it as the same token."""
    def world(namespace):
        return FakeBackend({"resources/store/open": namespace, "resources/store/fork": "ns_forked",
                            "a": lambda call: f"{call['args']['ns']}/synopsis", "b": "used", "c": "done"})

    manager = harness.manager()
    source = await manager.start(runnable(FORK_OUTPUTS), backend=world("ns_source"))
    await settle(manager, source)
    forked = world("ns_other")
    fork_id = await manager.fork(source, at_step=2, backend=forked)
    row = await settle(manager, fork_id)

    assert row["status"] == "succeeded", row["error"]
    assert forked.count("a") + forked.count("b") == 0 and forked.count("c") == 1


SUB_FINALLY = {"m.yaml": FINALLY_MACHINE.replace(
    'finally: {tool: log_end, args: {reason: "{{ ending.reason }}", error: "{{ ending.error and ending.error[\'type\'] }}"}}',
    "imports: {cleanup: ./cleanup.yaml}\nfinally: {machine: cleanup}\nresources:\n  store:\n    open: {tool: ns_open}\n"
    "    close: {tool: ns_close}"), "cleanup.yaml": """\
stategraph: 1
id: cleanup
initial: tidy
states:
  tidy:
    do: {tool: tidy}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_a_finally_that_is_a_submachine_runs_through_a_terminate(harness):
    """A4: the finally's own frame and activities are finalizers too; the resources close after it."""
    backend = FakeBackend({"resources/store/open": "ns_000001", "a": held(asyncio.Event()), "comp/finally": "ok",
                           "m.finally/tidy": "tidied", "resources/store/close": "closed"})
    manager = harness.manager()
    run_id = await manager.start(runnable(SUB_FINALLY), backend=backend)
    await until(lambda: backend.count("a") == 1, what="a in flight")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert [c["path"] for c in backend.calls if c["kind"] == "call_tool"][-3:] == [
        "comp/finally", "m.finally/tidy", "resources/store/close"]


async def test_a_terminate_during_the_end_of_a_finished_run_keeps_its_outcome_and_closes(harness):
    """A5: the root had finished; the running finally ends, the close still runs, the run succeeded."""
    gate = asyncio.Event()
    files = {"m.yaml": FINALLY_MACHINE.replace("initial: comp", "resources:\n  store:\n    open: {tool: ns_open}\n"
                                               "    close: {tool: ns_close}\ninitial: comp")}
    backend = FakeBackend({"resources/store/open": "ns_000001", "a": "A", "after": "B", "comp/finally": "ok",
                           "m.finally": held(gate, "ok"), "resources/store/close": "closed"})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend)
    await until(lambda: backend.count("m.finally") == 1, what="the machine finally of the finished run")
    manager.control(run_id, "terminate")
    await asyncio.sleep(0.05)
    gate.set()
    row = await settle(manager, run_id)

    assert (row["status"], row["output"]) == ("succeeded", 1), row
    assert backend.count("resources/store/close") == 1


async def test_a_finally_that_starts_after_the_terminate_is_bounded(harness, monkeypatch):
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 0.2)
    backend = FakeBackend({"a": held(asyncio.Event()), "comp/finally": "ok", "m.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": FINALLY_MACHINE}), backend=backend)
    await until(lambda: backend.count("a") == 1, what="a in flight")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id, timeout=3)

    assert row["status"] == "cancelled", row
    traces = [r for r in harness.store.rows(run_id, kinds=("trace",)) if r["status"] == "finally_failed"]
    assert [(t["state"], t["data"]["error"]["type"]) for t in traces] == [("m.finally", "timeout")], traces


async def test_a_pending_finally_gets_the_cancel_bound(harness, monkeypatch):
    """A6: a transition's finally still running when the terminate comes is bounded like the others."""
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 0.2)
    backend = FakeBackend({"a": "A", "after": held(asyncio.Event()), "comp/finally": held(asyncio.Event()),
                           "m.finally": "ok"})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": FINALLY_MACHINE}), backend=backend)
    await until(lambda: backend.count("comp/finally") == 1, what="the pending finally in flight")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id, timeout=3)

    assert row["status"] == "cancelled", row
    traces = [r for r in harness.store.rows(run_id, kinds=("trace",)) if r["status"] == "finally_failed"]
    assert [t["data"]["error"]["type"] for t in traces] == ["timeout"], traces


async def test_terminate_of_an_interrupted_run_is_not_held_by_a_breakpoint(tmp_path):
    """A7: the resume into the termination pauses at no breakpoint."""
    from agent_system.config.models import AgentSystemConfig
    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config

    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    (tmp_path / "machines" / "m.yaml").write_text(FINALLY_MACHINE, encoding="utf-8")
    try:
        mocks = {"a": "A", "after": "B", "comp/finally": "ok", "m.finally": "ok"}
        started = await server.service.start_run("m", mock_only=True, mocks=mocks, breakpoints=["after"])
        run_id = started["run_id"]
        await until(lambda: server.run_manager.live[run_id].ctx.status == "paused", what="the breakpoint")
        await server.run_manager.shutdown()
        server.run_manager._stopping = False

        await server.service.control_run(run_id, "terminate")
        row = server.run_store.get_run(run_id)

        assert row["status"] == "cancelled", row
    finally:
        await server.stop_plugin()


RESOURCE_BY_MACHINE = {"m.yaml": """\
stategraph: 1
id: m
imports: {opener: ./opener.yaml}
resources:
  store:
    open: {machine: opener}
initial: a
states:
  a:
    do: {tool: work, args: {ns: "{{ resources.store }}"}}
    transitions: [{target: b}]
  b:
    do: {tool: more, args: {ns: "{{ resources.store }}"}}
    transitions: [{target: done}]
  done: {type: final}
""", "opener.yaml": """\
stategraph: 1
id: opener
initial: make
states:
  make:
    do: {tool: ns_make}
    transitions: [{target: e}]
  e: {type: final, output: "{{ 'ns_' + run.id }}"}
"""}


async def test_a_fork_opens_a_resource_made_by_a_submachine_afresh(harness):
    """A8: the resource's own frame rows are not copied into the fork (they would diverge)."""
    manager = harness.manager()
    source = await manager.start(runnable(RESOURCE_BY_MACHINE),
                                 backend=FakeBackend({"resources/store/open/make": "x", "a": "A", "b": "B"}))
    await settle(manager, source)
    forked = FakeBackend({"resources/store/open/make": "x", "a": "A", "b": "B"})
    fork_id = await manager.fork(source, at_step=1, backend=forked)
    row = await settle(manager, fork_id)

    assert row["status"] == "succeeded", row["error"]
    assert (forked.count("resources/store/open/make"), forked.count("a"), forked.count("b")) == (1, 0, 1)


async def test_an_object_resource_s_strings_are_tokens_too(harness):
    """A9: a template that uses one field of an object resource replays in a fork with another object."""
    files = {"m.yaml": FORK_OUTPUTS["m.yaml"].replace('args: {ns: "{{ resources.store }}"}}',
                                                      'args: {target: "{{ resources.store[\'ns\'] }}"}}')}

    def world(namespace):
        return FakeBackend({"resources/store/open": {"ns": namespace}, "resources/store/fork": {"ns": "ns_forked"},
                            "a": "doc_1", "b": "used", "c": "done"})

    manager = harness.manager()
    source = await manager.start(runnable(files), backend=world("ns_source"))
    await settle(manager, source)
    forked = world("ns_other")
    fork_id = await manager.fork(source, at_step=2, backend=forked)
    row = await settle(manager, fork_id)

    assert row["status"] == "succeeded", row["error"]
    assert forked.count("a") == 0 and forked.calls[-1]["args"] == {"target": "ns_forked"}


async def test_terminate_in_a_process_that_lost_the_run_is_refused(harness):
    """A10: the refused, journaled terminate is not reported as done."""
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": FINALLY_MACHINE}),
                                 backend=FakeBackend({"a": held(asyncio.Event())}))
    await until(lambda: run_id in manager.live and manager.live[run_id].ctx.status == "running", what="running")
    harness.store.update_run(run_id, owner="host-b:2", lease_until=utc_at(60))  # B took the run meanwhile

    with pytest.raises(ValueError, match="another process owns"):
        manager.control(run_id, "terminate")


def test_resources_nested_access_and_open_order_are_checked():
    """A11: resources.x.y is plain data below the top level; an open sees only the resources before it."""
    from plugins.stategraph.tests.stategraph_testkit import validate

    tree = validate({"m.yaml": """\
stategraph: 1
id: m
resources:
  forum:
    open: {tool: f, args: {ns: "{{ resources.store }}"}}
  store:
    open: {tool: s}
initial: a
states:
  a:
    do: {tool: t, args: {x: "{{ resources.store.ns }}"}}
    transitions: [{target: done}]
  done: {type: final}
"""})
    messages = [(p.path, p.message) for p in tree.problems if p.level == "error"]
    assert any(path == "resources.forum.open.args.ns" and "not open yet" in msg for path, msg in messages), messages
    assert any(path == "states.a.do.args.x" and "plain data" in msg for path, msg in messages), messages


async def test_a_state_s_finally_and_its_submachine_s_finally_have_their_own_paths(harness):
    """A12: mocks by path must not answer both."""
    files = {"m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
initial: a
states:
  a:
    finally: {tool: outer}
    do: {machine: sub}
    transitions: [{target: done}]
  done: {type: final}
""", "sub.yaml": BRANCHES["sub.yaml"]}
    backend = FakeBackend({"a/w": "W", "a/sub.finally": "inner", "a/finally": "outer"})
    row = await run(harness, files, backend)

    assert row["status"] == "succeeded", row
    assert [c["path"] for c in backend.calls if c["kind"] == "call_tool"] == ["a/sub.finally", "a/finally"]


# ------------------------------------------------------------------ how a frame ends, across crashes (§3.10)

SELF_RETRY = {"m.yaml": """\
stategraph: 1
id: m
context: {tries: 0}
initial: x
finally: {tool: log_end}
states:
  x:
    do: {agent: w, task: t}
    finally: {tool: x_fin, args: {reason: "{{ ending.reason }}"}}
    transitions:
      - trigger: error
        target: x
        guard: ctx.tries < 3
        effect: ctx.tries += 1
      - target: done
  done: {type: final}
"""}


async def test_a_state_left_and_ended_within_one_step_runs_its_finally_under_two_keys(harness):
    """C1: the retry left x at step 1 (its finally ran), the terminate at step 1 ends x again -- its own key."""
    tries = {"n": 0}

    async def x(call):
        tries["n"] += 1
        if tries["n"] == 1:
            return ActivityError("agent_failed", "the first try fails")
        await asyncio.Event().wait()

    backend = FakeBackend({"x": x, "x/finally": "ok", "m.finally": "ok"})
    manager = harness.manager()
    run_id = await manager.start(runnable(SELF_RETRY), backend=backend)
    await until(lambda: tries["n"] == 2, what="the retry of x in flight")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert [(path, args.get("reason")) for path, args in finals(backend)] == [
        ("x/finally", "transition"), ("x/finally", "cancelled"), ("m.finally", None)]
    assert {"s1.fin.x", "end.cancelled.x", "end.cancelled.finally"} <= set(activity_rows(harness.store, run_id))


LEFT_THEN_RETRIED = {"m.yaml": """\
stategraph: 1
id: m
initial: x
states:
  x:
    do: {agent: w, task: x}
    finally: {tool: x_fin}
    transitions: [{target: y}]
  y:
    do: {agent: w, task: y, retry: {attempts: 3}}
    transitions: [{target: done}]
  done: {type: final}
"""}


async def test_an_interrupted_finally_of_a_transition_leaves_the_next_activity_s_retry_alone(harness):
    """C2: s1.fin.x sits beside the step's activity s1, not below it -- else its interrupted error would
    count as one of s1's children and forbid s1's retry (§5.5)."""
    run_id, _ = await crash_in(harness, LEFT_THEN_RETRIED, "x/finally", {"x": "X"})
    row = await resume(harness, run_id, FakeBackend({"y": sequence(ActivityError("agent_failed", "flaky"), "Y")}))

    assert row["status"] == "succeeded", row
    rows = activity_rows(harness.store, run_id)
    assert rows["s1.fin.x"]["data"]["error"]["type"] == "interrupted"
    assert rows["s1"]["data"]["meta"]["attempts"] == 2


ENDING_BRANCHES = {"m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
finally: {tool: log_end}
initial: p
states:
  p:
    do:
      parallel:
        x: {machine: sub}
        y: {machine: sub}
    transitions:
      - target: done
      - {trigger: error, target: broken}
  done: {type: final}
  broken: {type: final, status: failed}
""", "sub.yaml": """\
stategraph: 1
id: sub
resources:
  group:
    open: {tool: group_open}
    close: {tool: group_close}
initial: prepare
states:
  prepare:
    do: {tool: prepare}
    transitions: [{target: w}]
  w:
    do: {agent: w, task: t}
    finally: {tool: tidy, idempotent: true}
    transitions: [{target: e}]
  e: {type: final}
"""}

BRANCH_ANSWERS = {"p/x/resources/group/open": "group_x1", "p/y/resources/group/open": "group_y1",
                  "p/x/prepare": "P", "p/y/prepare": "P", "p/x/w/finally": "tidied",
                  "p/x/resources/group/close": "closed", "m.finally": "ok"}


async def test_a_resumed_terminate_lets_the_branches_finish_their_ending(harness):
    """C3: the terminate journaled before the crash is carried out at a leaf, never at the parallel: each branch
    replays to where it stood (w, one step in) and completes the finally and close the crash cut short."""
    first = FakeBackend({**BRANCH_ANSWERS, "p/x/w": held(asyncio.Event()), "p/y/w": held(asyncio.Event()),
                         "p/y/w/finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(ENDING_BRANCHES), backend=first)
    await until(lambda: first.count("p/x/w") == 1 and first.count("p/y/w") == 1, what="both branches in w")
    manager.control(run_id, "terminate")
    await until(lambda: first.count("p/y/w/finally") == 1 and first.count("p/x/resources/group/close") == 1,
                what="x ended, y tidies")
    await manager.shutdown()

    second = FakeBackend({"p/y/w/finally": "tidied", "p/y/resources/group/close": "closed", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "cancelled", row
    assert [c["path"] for c in second.calls] == ["p/y/w/finally", "p/y/resources/group/close", "m.finally"]


@pytest.mark.parametrize("idempotent", [True, False])
async def test_the_branches_a_fail_fast_join_cancelled_finish_their_ending_after_a_crash(harness, idempotent):
    """The join replays x's recorded failure without running anything live -- but y, which it had cancelled,
    was still in its finally: y replays into its end and completes it, and its agent does not run again. A
    breakpoint on w (set while w ran) does not hold that replay, and y -- also when it may not start again --
    gets no outcome of its own: the join's cancel ended it."""
    files = ENDING_BRANCHES if idempotent else {**ENDING_BRANCHES, "m.yaml": ENDING_BRANCHES["m.yaml"].replace(
        "y: {machine: sub}", "y: {machine: sub, idempotent: false}")}
    gate = asyncio.Event()

    async def x_fails_later(call):
        await gate.wait()
        return ActivityError("agent_failed", "x broke")

    first = FakeBackend({**BRANCH_ANSWERS, "p/x/w": x_fails_later, "p/y/w": held(asyncio.Event()),
                         "p/y/w/finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("p/x/w") == 1 and first.count("p/y/w") == 1, what="both branches in w")
    manager.set_points(run_id, breakpoints=[{"state": "w", "machine": "sub"}])
    gate.set()
    await until(lambda: first.count("p/y/w/finally") == 1, what="y, cancelled by the join, tidies")
    await manager.shutdown()

    second = FakeBackend({"p/y/w/finally": "tidied", "p/y/resources/group/close": "closed", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["final_state"]) == ("failed", "broken"), row
    assert [c["path"] for c in second.calls] == ["p/y/w/finally", "p/y/resources/group/close", "m.finally"]
    rows = activity_rows(harness.store, run_id)
    assert (rows["s0/b.y/m/s1"]["status"], rows["s0/b.y"]["status"]) == ("started", "started")  # no new outcomes


async def test_a_terminate_that_came_while_a_fail_fast_join_ended_is_carried_out_at_the_join(harness):
    """Then the terminate came too, and the crash: the join replays x's failure and y's ending, and ends
    terminated -- not with the failure as a new outcome the machine would go on to handle."""
    gate = asyncio.Event()

    async def x_fails_later(call):
        await gate.wait()
        return ActivityError("agent_failed", "x broke")

    first = FakeBackend({**BRANCH_ANSWERS, "p/x/w": x_fails_later, "p/y/w": held(asyncio.Event()),
                         "p/y/w/finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(ENDING_BRANCHES), backend=first)
    await until(lambda: first.count("p/x/w") == 1 and first.count("p/y/w") == 1, what="both branches in w")
    gate.set()
    await until(lambda: first.count("p/y/w/finally") == 1, what="y, cancelled by the join, tidies")
    manager.control(run_id, "terminate")
    await asyncio.sleep(0.05)
    await manager.shutdown()

    second = FakeBackend({"p/y/w/finally": "tidied", "p/y/resources/group/close": "closed", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "cancelled", row
    assert activity_rows(harness.store, run_id)["s0"]["status"] == "started"  # no outcome of the join
    assert [c["path"] for c in second.calls] == ["p/y/w/finally", "p/y/resources/group/close", "m.finally"]


LAST_STEP_FINALLY = {"m.yaml": """\
stategraph: 1
id: m
resources:
  store:
    open: {tool: ns_open}
    close: {tool: ns_close, idempotent: true}
initial: a
finally: {tool: log_end, args: {reason: "{{ ending.reason }}"}, idempotent: true}
states:
  a:
    do: {agent: w, task: t}
    finally: {tool: a_fin}
    transitions: [{target: done}]
  done: {type: final}
"""}


def reasons(*backends: FakeBackend) -> list:
    return [c["args"].get("reason") for backend in backends for c in backend.calls if c["path"] == "m.finally"]


async def test_a_terminate_during_the_finally_of_the_last_transition_ends_the_same_after_a_crash(harness):
    """C4: the terminate came while the transition into the final state ran a finally; the resume replays
    straight into the final state -- no live step to carry the terminate out at -- and still ends cancelled."""
    gate = asyncio.Event()
    first = FakeBackend({"resources/store/open": "ns_000001", "a": "A", "a/finally": held(gate, "ok"),
                         "m.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(LAST_STEP_FINALLY), backend=first)
    await until(lambda: first.count("a/finally") == 1, what="the finally of the transition into done")
    manager.control(run_id, "terminate")
    await asyncio.sleep(0.05)
    gate.set()
    await until(lambda: first.count("m.finally") == 1, what="the machine's finally")
    await manager.shutdown()

    second = FakeBackend({"m.finally": "ok", "resources/store/close": "closed"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "cancelled", row
    assert reasons(first, second) == ["cancelled", "cancelled"]


async def test_a_terminate_that_came_after_the_run_finished_is_absorbed_after_a_crash_too(harness):
    """C4b: the root had finished when the terminate came (its finally ran): the journaled end says so, and the
    resume keeps the outcome instead of taking the pending terminate for one that came first."""
    first = FakeBackend({"resources/store/open": "ns_000001", "a": "A", "a/finally": "ok",
                         "m.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(LAST_STEP_FINALLY), backend=first)
    await until(lambda: first.count("m.finally") == 1, what="the finally of the finished run")
    manager.control(run_id, "terminate")
    await asyncio.sleep(0.05)
    await manager.shutdown()

    second = FakeBackend({"m.finally": "ok", "resources/store/close": "closed"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "succeeded", row
    assert reasons(first, second) == ["finished", "finished"]
    assert second.count("resources/store/close") == 1


async def test_a_terminate_does_not_cut_the_finally_of_a_branch_a_fail_fast_join_cancelled(harness):
    """C5: y had the join's cancel already; the run's terminate reaches it once more -- no reason to stop."""
    gate = asyncio.Event()

    async def x_fails_later(call):
        await gate.wait()
        return ActivityError("agent_failed", "x broke")

    async def tidy_slowly(call):
        await asyncio.sleep(0.3)
        return "tidied"

    backend = FakeBackend({"p/x/w": x_fails_later, "p/y/w": held(asyncio.Event()), "p/x/sub.finally": "ok",
                           "p/y/sub.finally": tidy_slowly})
    manager = harness.manager()
    run_id = await manager.start(runnable(BRANCHES), backend=backend)
    await until(lambda: backend.count("p/x/w") == 1 and backend.count("p/y/w") == 1, what="both branches")
    gate.set()
    await until(lambda: backend.count("p/y/sub.finally") == 1, what="y, cancelled by the join, tidies")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert activity_rows(harness.store, run_id)["s0/b.y/m/end.cancelled.finally"]["status"] == "done"
    assert "p/y/sub.finally" not in backend.cancelled


async def test_a_stop_does_not_wait_for_the_finally_activities_and_the_resume_completes_them(harness, monkeypatch):
    """C6: a stop is not a terminate: the finished run's finally stops at once, the run is interrupted, and the
    resume runs the finally and the close."""
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 1.0)  # were the stop waited for: a bounded wait
    first = FakeBackend({"resources/store/open": "ns_000001", "a": "A", "a/finally": "ok",
                         "m.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(LAST_STEP_FINALLY), backend=first)
    await until(lambda: first.count("m.finally") == 1, what="the finally of the finished run")
    began = time.monotonic()
    await manager.shutdown()

    assert time.monotonic() - began < 0.5
    assert harness.store.get_run(run_id)["status"] == "interrupted"
    assert first.count("resources/store/close") == 0
    second = FakeBackend({"m.finally": "ok", "resources/store/close": "closed"})
    row = await resume(harness, run_id, second)
    assert row["status"] == "succeeded", row
    assert [c["path"] for c in second.calls] == ["m.finally", "resources/store/close"]


NESTED_CLEANUP = {"m.yaml": """\
stategraph: 1
id: m
imports: {cleanup: ./cleanup.yaml}
initial: a
finally: {machine: cleanup}
states:
  a:
    do: {agent: w, task: t}
    transitions: [{target: done}]
  done: {type: final}
""", "cleanup.yaml": """\
stategraph: 1
id: cleanup
finally: {tool: cleanup_fin}
initial: tidy
states:
  tidy:
    do: {tool: tidy}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_a_stop_during_a_finally_submachine_ends_it_before_the_run_ends(harness):
    """C7: the stop reaches the finally's own frame -- it ends without its own finally, and none of its work
    outlives the run (a tool that tidies up when cancelled is waited for)."""
    async def slow_to_stop(call):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            raise

    backend = FakeBackend({"a": held(asyncio.Event()), "m.finally/tidy": slow_to_stop,
                           "m.finally/cleanup.finally": "fin"})
    manager = harness.manager()
    run_id = await manager.start(runnable(NESTED_CLEANUP), backend=backend)
    await until(lambda: backend.count("a") == 1, what="a in flight")
    manager.control(run_id, "terminate")
    await until(lambda: backend.count("m.finally/tidy") == 1, what="the cleanup's tidy")
    await manager.shutdown()

    assert harness.store.get_run(run_id)["status"] == "interrupted"
    assert not [t for t in asyncio.all_tasks() if "ActivityRun.execute" in repr(t.get_coro())]
    rows = activity_rows(harness.store, run_id)
    await asyncio.sleep(0.3)
    assert activity_rows(harness.store, run_id) == rows
    assert backend.count("m.finally/cleanup.finally") == 0


async def test_a_timeout_after_a_terminate_neither_fails_the_run_nor_cuts_its_finally(harness):
    """C8: limits.timeout passes while the terminate's finally runs: the watchdog stays out."""
    files = {"m.yaml": """\
stategraph: 1
id: m
limits: {timeout: 0.5s}
initial: a
finally: {tool: log_end}
states:
  a:
    do: {agent: w, task: t}
    transitions: [{target: done}]
  done: {type: final}
"""}

    async def slow_log(call):
        await asyncio.sleep(1.2)
        return "logged"

    backend = FakeBackend({"a": held(asyncio.Event()), "m.finally": slow_log})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend)
    await until(lambda: backend.count("a") == 1, what="a in flight")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert (row["status"], row["error"]["type"]) == ("cancelled", "cancelled"), row
    assert activity_rows(harness.store, run_id)["end.cancelled.finally"]["status"] == "done"


async def test_a_run_that_timed_out_still_ends_timed_out_when_terminated_after_a_crash(harness):
    """The timeout is journaled like a terminate; a terminate of the interrupted run then adds nothing."""
    files = {"m.yaml": """\
stategraph: 1
id: m
limits: {timeout: 0.3s}
initial: a
finally: {tool: log_end, idempotent: true}
states:
  a:
    do: {agent: w, task: t}
    transitions: [{target: done}]
  done: {type: final}
"""}
    first = FakeBackend({"a": held(asyncio.Event()), "m.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("m.finally") == 1, timeout=3, what="the timeout's finally")
    await manager.shutdown()

    second = FakeBackend({"m.finally": "logged"})
    manager = harness.manager()
    await manager.resume(run_id, backend=second, cancel=True)
    row = await settle(manager, run_id)

    assert (row["status"], row["error"]["type"]) == ("failed", "timed_out"), row
    assert second.count("m.finally") == 1 and second.count("a") == 0


BREAK_IN_CLEANUP = {"m.yaml": """\
stategraph: 1
id: m
imports: {cleanup: ./cleanup.yaml}
initial: a
finally: {machine: cleanup}
states:
  a:
    do: {agent: w, task: t}
    transitions: [{target: done}]
  done: {type: final}
""", "cleanup.yaml": NESTED_CLEANUP["cleanup.yaml"].replace("finally: {tool: cleanup_fin}\n", "")}


async def test_a_breakpoint_in_a_finally_submachine_does_not_hold_a_terminate(harness):
    """C9: while the run ends, nothing pauses it -- not a breakpoint on a state of its finally's machine."""
    backend = FakeBackend({"a": held(asyncio.Event()), "m.finally/tidy": "tidied"})
    manager = harness.manager()
    run_id = await manager.start(runnable(BREAK_IN_CLEANUP), backend=backend, breakpoints=["tidy"])
    await until(lambda: backend.count("a") == 1, what="a in flight")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id, timeout=2)

    assert row["status"] == "cancelled", row
    assert backend.count("m.finally/tidy") == 1


async def test_a_terminate_lets_go_of_a_pause_inside_a_finally(harness):
    """The run finished; its finally's machine paused at a breakpoint (someone debugs the finally); a terminate
    releases it, the finally ends, and the outcome stands."""
    backend = FakeBackend({"a": "A", "m.finally/tidy": "tidied"})
    manager = harness.manager()
    run_id = await manager.start(runnable(BREAK_IN_CLEANUP), backend=backend,
                                 breakpoints=[{"state": "tidy", "machine": "cleanup"}])
    await until(lambda: manager.live[run_id].ctx.debugger.paused is not None, what="the pause in the finally")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id, timeout=2)

    assert row["status"] == "succeeded", row
    assert backend.count("m.finally/tidy") == 1


async def test_a_finally_that_cancels_itself_fails_alone_and_the_ending_goes_on(harness):
    """An agent call inside a finally whose caller refuses with CancelledError: that finally failed, the rest
    of the ending still runs."""
    async def refused(call):
        raise asyncio.CancelledError("run cancelled")

    backend = FakeBackend({"a": held(asyncio.Event()), "comp/finally": refused, "m.finally": "ok"})
    manager = harness.manager()
    run_id = await manager.start(runnable({"m.yaml": FINALLY_MACHINE}), backend=backend)
    await until(lambda: backend.count("a") == 1, what="a in flight")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert backend.count("m.finally") == 1
    traces = [r for r in harness.store.rows(run_id, kinds=("trace",)) if r["status"] == "finally_failed"]
    assert [(t["state"], t["data"]["error"]["type"]) for t in traces] == [("comp/finally", "cancelled")]


DEADLINE = {"m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
initial: s
states:
  s:
    do: {machine: sub, timeout: 0.3s}
    transitions:
      - target: done
      - {trigger: error, target: late}
  done: {type: final}
  late: {type: final, status: failed}
""", "sub.yaml": """\
stategraph: 1
id: sub
finally: {tool: tidy, args: {reason: "{{ ending.reason }}"}}
initial: w
states:
  w:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_a_frame_an_activity_timeout_ended_decides_its_end_anew_after_a_crash(harness):
    """An activity's own timeout is a local cancel and not journaled: the resumed submachine runs on and ends
    another way -- its finally runs under that ending's key instead of meeting the other ending's row."""
    first = FakeBackend({"s/w": held(asyncio.Event()), "s/sub.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(DEADLINE), backend=first)
    await until(lambda: first.count("s/sub.finally") == 1, what="the timeout ended the submachine")
    await manager.shutdown()

    second = FakeBackend({"s/w": "W", "s/sub.finally": "tidied"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["error"]) == ("succeeded", None), row
    assert [c["args"]["reason"] for c in first.calls + second.calls if c["path"] == "s/sub.finally"] == [
        "cancelled", "finished"]


async def test_a_terminate_cancels_the_run_s_sub_requests_but_not_its_own_token(tmp_path):
    """A cancelled run token would have the platform force-cancel every <run>_... task after its cleanup
    timeout -- also the agents its finally activities start after the terminate."""
    from agent_system.config.models import AgentSystemConfig
    from agent_system.core.cancellation import get_cancellation_manager
    from plugins.stategraph.server import StateGraphServer
    from plugins.stategraph.tests.stategraph_testkit import tool_config

    server = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    manager = get_cancellation_manager()
    own, sub = manager.create_token("run7"), manager.create_token("run7_001")
    try:
        server._cancel_requests("run7")
        assert (sub.is_cancelled, own.is_cancelled) == (True, False)
    finally:
        manager.unregister_request("run7")
        manager.unregister_request("run7_001")
        await server.stop_plugin()


# ------------------------------------------------------------------ where an ending frame stops (§3.10)

FAIL_FAST_IN_TRANSITION = {"m.yaml": """\
stategraph: 1
id: m
imports: {subx: ./subx.yaml, suby: ./suby.yaml}
initial: p
states:
  p:
    do:
      parallel:
        x: {machine: subx}
        y: {machine: suby}
    transitions:
      - target: done
      - {trigger: error, target: broken}
  done: {type: final}
  broken: {type: final, status: failed}
""", "subx.yaml": """\
stategraph: 1
id: subx
initial: w
states:
  w:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
""", "suby.yaml": """\
stategraph: 1
id: suby
resources:
  group:
    open: {tool: group_open}
    close: {tool: group_close}
finally: {tool: suby_fin, args: {reason: "{{ ending.reason }}"}}
initial: a
states:
  a:
    do: {agent: w, task: t}
    finally: {tool: a_fin, idempotent: true}
    transitions: [{target: b}]
  b:
    do: {agent: w, task: t2}
    transitions: [{target: e}]
  e: {type: final}
"""}


@pytest.mark.parametrize("target", ["b", "e"])
async def test_a_branch_the_join_cancelled_in_a_transition_s_finally_ends_after_a_crash(harness, target):
    """y took the join's cancel inside a transition's finally: no end was journaled yet when the process stopped.
    The resume still replays y into its end -- the finally it was in, its machine's finally, the close -- and y
    ends as that cancel ended it, also where the transition went into y's final state. A breakpoint on the state
    y stood in does not hold it there."""
    files = {**FAIL_FAST_IN_TRANSITION, "suby.yaml": FAIL_FAST_IN_TRANSITION["suby.yaml"].replace(
        "transitions: [{target: b}]", f"transitions: [{{target: {target}}}]")}
    gate = asyncio.Event()

    async def x_fails_later(call):
        await gate.wait()
        return ActivityError("agent_failed", "x broke")

    first = FakeBackend({"p/x/w": x_fails_later, "p/y/resources/group/open": "grp_y", "p/y/a": "A",
                         "p/y/a/finally": held(asyncio.Event()), "p/y/b": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first, breakpoints=[{"state": "b", "machine": "suby"}])
    await until(lambda: first.count("p/y/a/finally") == 1, what="y in its transition's finally")
    gate.set()
    await asyncio.sleep(0.05)
    await manager.shutdown()

    second = FakeBackend({"p/y/a/finally": "ok", "p/y/suby.finally": "fin", "p/y/resources/group/close": "closed"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["final_state"]) == ("failed", "broken"), row
    assert [(c["path"], c["args"].get("reason")) for c in second.calls] == [
        ("p/y/a/finally", None), ("p/y/suby.finally", "cancelled"), ("p/y/resources/group/close", None)]


EXIT_PAUSE = {"m.yaml": """\
stategraph: 1
id: m
context: {entered_t: false}
initial: s
finally: {tool: m_fin, args: {reason: "{{ ending.reason }}", t: "{{ ctx.entered_t }}"}, idempotent: true}
states:
  s:
    do: {agent: w, task: s}
    finally: {tool: s_fin, args: {reason: "{{ ending.reason }}"}, idempotent: true}
    transitions: [{target: t}]
  t:
    entry: ctx.entered_t = True
    do: {agent: w, task: t}
    finally: {tool: t_fin, args: {reason: "{{ ending.reason }}"}, idempotent: true}
    transitions: [{target: done}]
  done: {type: final}
"""}


def endings(backend: FakeBackend) -> list[tuple]:
    return [(c["path"], c["args"].get("reason"), c["args"].get("t")) for c in backend.calls if c["kind"] == "call_tool"]


async def test_a_terminate_at_an_exit_pause_ends_the_frame_where_it_stood_after_a_crash(harness):
    """s's outcome is journaled, its transition was not taken: the frame stood at the exit hook. The resume
    carries the terminate out before the transition -- s ends cancelled, and t is never entered."""
    first = FakeBackend({"s": "S", "s/finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(EXIT_PAUSE), backend=first, breakpoints=["s@exit"])
    await until(lambda: manager.live[run_id].ctx.debugger.paused is not None, what="the pause at s's exit")
    manager.control(run_id, "terminate")
    await until(lambda: first.count("s/finally") == 1, what="s's finally")
    await manager.shutdown()

    second = FakeBackend({"s/finally": "ok", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "cancelled", row
    assert endings(second) == [("s/finally", "cancelled", None), ("m.finally", "cancelled", False)]


async def test_a_run_stopped_at_an_exit_pause_and_then_terminated_ends_in_the_state_it_stood_in(harness):
    first = FakeBackend({"s": "S"})
    manager = harness.manager()
    run_id = await manager.start(runnable(EXIT_PAUSE), backend=first, breakpoints=["s@exit"])
    await until(lambda: manager.live[run_id].ctx.debugger.paused is not None, what="the pause at s's exit")
    await manager.shutdown()

    second = FakeBackend({"s/finally": "ok", "m.finally": "ok"})
    manager = harness.manager()
    await manager.resume(run_id, backend=second, cancel=True)
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert endings(second) == [("s/finally", "cancelled", None), ("m.finally", "cancelled", False)]


FINISHED_BELOW = {"m.yaml": """\
stategraph: 1
id: m
imports: {ma: ./ma.yaml, mb: ./mb.yaml}
initial: p
states:
  p:
    do:
      parallel:
        a: {machine: ma}
        b: {machine: mb}
    transitions: [{target: done}]
  done: {type: final}
""", "ma.yaml": """\
stategraph: 1
id: ma
imports: {y: ./y.yaml}
initial: s1
finally: {tool: ma_fin, args: {reason: "{{ ending.reason }}"}, idempotent: true}
states:
  s1:
    do: {machine: y}
    finally: {tool: s1_fin, args: {reason: "{{ ending.reason }}"}, idempotent: true}
    transitions: [{target: s2}]
  s2:
    do: {agent: w, task: s2}
    finally: {tool: s2_fin, args: {reason: "{{ ending.reason }}"}, idempotent: true}
    transitions: [{target: e}]
  e: {type: final}
""", "y.yaml": """\
stategraph: 1
id: y
initial: w
finally: {tool: y_fin, idempotent: true}
states:
  w:
    do: {tool: quick}
    transitions: [{target: e}]
  e: {type: final}
""", "mb.yaml": """\
stategraph: 1
id: mb
initial: w
finally: {tool: mb_fin, idempotent: true}
states:
  w:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
"""}


def later(seconds: float, value: str = "ok"):
    async def handler(call):
        await asyncio.sleep(seconds)
        return value

    return handler


@pytest.mark.parametrize("b_first", [False, True], ids=["a-first", "b-first"])
async def test_a_frame_that_had_finished_passes_on_a_terminate_a_sibling_carried_out(harness, b_first):
    """y had finished and ran its finally when the run was terminated. After the crash b carries the terminate
    out first; y, done with its finally, must not hand ma an outcome that takes ma's transition -- and ma,
    reached after b had carried it out, still lets y complete its finally: ma ends in s1, where it stood."""
    files = FINISHED_BELOW
    if b_first:
        files = {**files, "m.yaml": files["m.yaml"].replace("        a: {machine: ma}\n        b: {machine: mb}\n",
                                                             "        b: {machine: mb}\n        a: {machine: ma}\n")}
    first = FakeBackend({"p/a/s1/w": "Q", "p/a/s1/y.finally": held(asyncio.Event()), "p/b/w": held(asyncio.Event()),
                         "p/b/mb.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("p/a/s1/y.finally") == 1 and first.count("p/b/w") == 1, what="y ends, b in w")
    manager.control(run_id, "terminate")
    await until(lambda: first.count("p/b/mb.finally") == 1, what="b's finally")
    await manager.shutdown()

    second = FakeBackend({"p/a/s1/y.finally": later(0.05), "p/b/mb.finally": later(0.4), "p/a/s1/finally": "ok",
                          "p/a/ma.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "cancelled", row
    assert sorted((c["path"], c["args"].get("reason")) for c in second.calls) == [
        ("p/a/ma.finally", "cancelled"), ("p/a/s1/finally", "cancelled"), ("p/a/s1/y.finally", None),
        ("p/b/mb.finally", None)]
    assert "s0/b.a/m/s1:step" not in {r["key"] for r in harness.store.rows(run_id, kinds=("trace",))}
    assert activity_rows(harness.store, run_id)["s0/b.a/m/s0"]["status"] == "started"  # y's activity: no outcome


async def test_a_resumed_terminate_bounds_a_finally_in_another_branch_from_then_on(harness, monkeypatch):
    """y's finally runs again after the crash; b carries the pending terminate out, then ends slowly. y's finally
    gets the bound from that moment on, as if the terminate had reached it -- not only once b is done."""
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 0.3)
    files = {**FINISHED_BELOW, "mb.yaml": FINISHED_BELOW["mb.yaml"].replace(
        "finally: {tool: mb_fin, idempotent: true}", "finally: {tool: mb_fin, idempotent: true, timeout: 5s}")}
    first = FakeBackend({"p/a/s1/w": "Q", "p/a/s1/y.finally": held(asyncio.Event()), "p/b/w": held(asyncio.Event()),
                         "p/b/mb.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("p/a/s1/y.finally") == 1 and first.count("p/b/w") == 1, what="y ends, b in w")
    manager.control(run_id, "terminate")
    await until(lambda: first.count("p/b/mb.finally") == 1, what="b's finally")
    await manager.shutdown()

    cut: dict[str, float] = {}

    async def endless(call):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cut["at"] = time.monotonic()
            raise

    second = FakeBackend({"p/a/s1/y.finally": endless, "p/b/mb.finally": later(1.5), "p/a/s1/finally": "ok",
                          "p/a/ma.finally": "ok"})
    began = time.monotonic()
    row = await resume(harness, run_id, second)

    assert row["status"] == "cancelled", row
    assert cut["at"] - began < 1.2, cut["at"] - began  # 0.3 s from b's carrying it out, not from b's end (1.5 s)


FORKED_FINALLY = {"m.yaml": """\
stategraph: 1
id: m
imports: {cleanup: ./cleanup.yaml}
initial: a
finally: {machine: cleanup, params: {reason: "{{ ending.reason }}"}}
states:
  a:
    do: {agent: w, task: a}
    transitions: [{target: b}]
  b:
    do: {agent: w, task: b}
    transitions: [{target: done}]
  done: {type: final}
""", "cleanup.yaml": """\
stategraph: 1
id: cleanup
params: {reason: {type: string}}
initial: tidy
states:
  tidy:
    do: {tool: tidy, args: {reason: "{{ params.reason }}"}}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_a_fork_runs_its_own_ending_not_the_source_s(harness):
    """The rows of the source's root finally (a submachine, so they carry steps of their own frame) are not
    copied into a fork: the fork, failing the same way, runs its own finally instead of replaying the source's."""
    manager = harness.manager()
    source = await manager.start(runnable(FORKED_FINALLY), backend=FakeBackend({
        "a": "A", "b": ActivityError("agent_failed", "b broke"), "m.finally/tidy": "tidied"}))
    await settle(manager, source)
    fork_backend = FakeBackend({"b": ActivityError("agent_failed", "b broke again"), "m.finally/tidy": "tidied"})
    row = await settle(manager, await manager.fork(source, at_step=1, backend=fork_backend))

    assert (row["status"], row["error"]["type"]) == ("failed", "agent_failed"), row
    assert [c["path"] for c in fork_backend.calls] == ["b", "m.finally/tidy"]


NOT_AGAIN = {"m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
initial: s
states:
  s:
    do: {machine: sub, idempotent: false}
    transitions:
      - target: done
      - {trigger: error, target: handle}
  handle:
    do: {agent: w, task: h}
    transitions: [{target: done}]
  done: {type: final}
""", "sub.yaml": """\
stategraph: 1
id: sub
resources:
  group:
    open: {tool: group_open}
    close: {tool: group_close, idempotent: true}
initial: w
states:
  w:
    do: {agent: w, task: t}
    finally: {tool: w_fin, idempotent: true}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_a_non_idempotent_submachine_is_interrupted_only_after_its_frame_ended(harness):
    """It is not started again -- but the frame it had opened replays into its end first (w's finally, the
    group's close), then it raises interrupted for the error path to handle."""
    run_id, _ = await crash_in(harness, NOT_AGAIN, "s/w", {"s/resources/group/open": "grp_000001"})
    second = FakeBackend({"s/w/finally": "ok", "s/resources/group/close": "closed", "handle": "H"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["final_state"]) == ("succeeded", "done"), row
    assert [c["path"] for c in second.calls] == ["s/w/finally", "s/resources/group/close", "handle"]
    assert activity_rows(harness.store, run_id)["s0"]["data"]["error"]["type"] == "interrupted"


async def test_a_terminate_carried_into_a_resume_ends_a_non_idempotent_submachine_s_frame(harness):
    """In flight at a terminate, then the crash: it goes on so its frame replays into its end, and ends
    terminated -- not interrupted, which the machine's error path would go on to handle."""
    first = FakeBackend({"s/resources/group/open": "grp_000001", "s/w": held(asyncio.Event()),
                         "s/w/finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(NOT_AGAIN), backend=first)
    await until(lambda: first.count("s/w") == 1, what="w in flight")
    manager.control(run_id, "terminate")
    await until(lambda: first.count("s/w/finally") == 1, what="w's finally")
    await manager.shutdown()

    second = FakeBackend({"s/w/finally": "ok", "s/resources/group/close": "closed"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "cancelled", row
    assert [c["path"] for c in second.calls] == ["s/w/finally", "s/resources/group/close"]
    assert activity_rows(harness.store, run_id)["s0"]["status"] == "started"


RETRIED_WITH_ENDING = {"m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
initial: s
states:
  s:
    do: {machine: sub, retry: {attempts: 3}}
    transitions:
      - target: done
      - {trigger: error, target: broken}
  done: {type: final}
  broken: {type: final, status: failed}
""", "sub.yaml": """\
stategraph: 1
id: sub
initial: w
finally: {tool: note_end, args: {reason: "{{ ending.reason }}"}}
states:
  w:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_an_interrupted_finally_of_a_failed_attempt_leaves_the_retry_alone(harness):
    """The crash hit attempt 1's ending, a finally that is not idempotent: it is not run again -- but it
    belongs to attempt 1's frame, and attempt 2 runs its own frame and its own finally. The retry goes on."""
    run_id, _ = await crash_in(harness, RETRIED_WITH_ENDING, "s/sub.finally",
                               {"s/w": ActivityError("agent_failed", "flaky")})
    second = FakeBackend({"s/w": "W", "s/sub.finally": "noted"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["final_state"]) == ("succeeded", "done"), row
    assert activity_rows(harness.store, run_id)["s0/m/end.failed.finally"]["data"]["error"]["type"] == "interrupted"
    assert [c["path"] for c in second.calls] == ["s/w", "s/sub.finally"]


PAUSE_IN_LINE = {"m.yaml": """\
stategraph: 1
id: m
imports: {subx: ./subx.yaml, suby: ./suby.yaml}
initial: p
states:
  p:
    do:
      parallel:
        x: {machine: subx}
        y: {machine: suby}
    transitions: [{target: done}]
  done: {type: final}
""", "subx.yaml": FAIL_FAST_IN_TRANSITION["subx.yaml"], "suby.yaml": """\
stategraph: 1
id: suby
imports: {cleanup: ./cleanup.yaml}
initial: q
finally: {machine: cleanup}
states:
  q:
    do: {tool: quick}
    transitions: [{target: e}]
  e: {type: final}
""", "cleanup.yaml": """\
stategraph: 1
id: cleanup
initial: tidy
states:
  tidy:
    do: {tool: tidy}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_a_pause_waiting_its_turn_does_not_hold_a_terminate(harness, monkeypatch):
    """x holds the one pause; y's finally reaches a breakpoint and waits its turn. The terminate lets go of x's
    pause -- and y's, still in line, must not pause the ending run once its turn comes."""
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 1.5)
    gate = asyncio.Event()
    backend = FakeBackend({"p/x/w": "W", "p/y/q": held(gate, "Q"), "p/y/suby.finally/tidy": "tidied"})
    manager = harness.manager()
    run_id = await manager.start(runnable(PAUSE_IN_LINE), backend=backend, breakpoints=[
        {"state": "w", "machine": "subx"}, {"state": "tidy", "machine": "cleanup"}])
    ctx = manager.live[run_id].ctx
    await until(lambda: ctx.debugger.paused is not None, what="x paused at w")
    gate.set()  # y finishes; its finally's machine reaches tidy and waits for the pause
    await asyncio.sleep(0.2)
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert backend.count("p/y/suby.finally/tidy") == 1
    assert not [r for r in harness.store.rows(run_id, kinds=("trace",)) if r["status"] == "finally_failed"]


DIVERGING_BRANCH = {"m.yaml": """\
stategraph: 1
id: m
imports: {subx: ./subx.yaml, suby: ./suby.yaml}
initial: p
finally: {tool: m_fin}
states:
  p:
    do:
      parallel:
        x: {machine: subx}
        y: {machine: suby}
    transitions: [{target: done}]
  done: {type: final}
""", "subx.yaml": """\
stategraph: 1
id: subx
python: subx.py
initial: a
states:
  a:
    do: {tool: stamp, args: {nonce: "{{ nonce() }}"}}
    transitions: [{target: b}]
  b:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
""", "subx.py": "import uuid\n\n\ndef nonce():\n    return uuid.uuid4().hex\n", "suby.yaml": """\
stategraph: 1
id: suby
initial: w
finally: {tool: y_fin}
resources:
  group:
    open: {tool: group_open}
    close: {tool: group_close}
states:
  w:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_a_divergence_in_one_branch_runs_no_finally_in_the_others(harness):
    """The run's state is not trusted once a replay diverges: the join cancels y, and y ends without its
    finally and close -- as the diverging branch and the root do."""
    first = FakeBackend({"p/x/a": "stamped", "p/x/b": held(asyncio.Event()), "p/y/w": held(asyncio.Event()),
                         "p/y/resources/group/open": "grp_000001"})
    manager = harness.manager()
    run_id = await manager.start(runnable(DIVERGING_BRANCH), backend=first)
    await until(lambda: first.count("p/x/b") == 1 and first.count("p/y/w") == 1, what="both branches in flight")
    await manager.shutdown()

    second = FakeBackend({"p/y/w": held(asyncio.Event()), "p/y/suby.finally": "ran",
                          "p/y/resources/group/close": "closed", "m.finally": "ran"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["error"]["type"]) == ("failed", "diverged"), row
    assert not {"p/y/suby.finally", "p/y/resources/group/close", "m.finally"} & {c["path"] for c in second.calls}


SWALLOWED_IN_JOIN = {"m.yaml": """\
stategraph: 1
id: m
imports: {suby: ./suby.yaml}
initial: p
finally: {tool: m_fin}
states:
  p:
    do:
      parallel:
        x: {agent: w, task: x}
        y: {machine: suby}
    transitions:
      - target: done
      - {trigger: error, target: handle}
  handle:
    do: {agent: w, task: h}
    transitions: [{target: done}]
  done: {type: final}
""", "suby.yaml": """\
stategraph: 1
id: suby
python: suby.py
finally: {tool: y_fin, idempotent: true}
initial: a
states:
  a:
    do: {tool: stamp, args: {nonce: "{{ nonce() }}"}}
    transitions: [{target: w}]
  w:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
""", "suby.py": "import uuid\n\n\ndef nonce():\n    return uuid.uuid4().hex\n"}


async def test_a_divergence_in_a_branch_replayed_into_its_end_ends_the_run(harness):
    """The join replays x's recorded failure after y's ending, and y's replay diverges: that ends the run
    diverged -- the join's recorded failure must not carry the run on into its error path."""
    gate = asyncio.Event()

    async def x_fails_later(call):
        await gate.wait()
        return ActivityError("agent_failed", "x broke")

    first = FakeBackend({"p/x": x_fails_later, "p/y/a": "stamped", "p/y/w": held(asyncio.Event()),
                         "p/y/suby.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(SWALLOWED_IN_JOIN), backend=first)
    await until(lambda: first.count("p/y/w") == 1, what="y in w")
    gate.set()
    await until(lambda: first.count("p/y/suby.finally") == 1, what="y's finally after the join cancelled it")
    await manager.shutdown()

    second = FakeBackend({"p/y/suby.finally": "ok", "handle": "H", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["error"]["type"]) == ("failed", "diverged"), row
    assert second.calls == []


async def test_a_divergence_a_call_catches_still_ends_the_run(harness):
    """A call's ``except Exception`` around sg.tool() takes in the divergence of a replayed tool call: the run
    ends diverged all the same, and nothing after it runs live."""
    files = {"m.yaml": """\
stategraph: 1
id: m
python: m.py
initial: a
finally: {tool: m_fin}
states:
  a:
    do: {call: work, idempotent: true}
    transitions: [{target: b}]
  b:
    do: {agent: w, task: b}
    transitions: [{target: done}]
  done: {type: final}
""", "m.py": """import uuid


async def work(sg):
    try:
        await sg.tool("stamp", {"nonce": uuid.uuid4().hex}, idempotent=True)
        await sg.tool("slow", {}, idempotent=True)
    except Exception:
        return "gave up"
    return "ok"
"""}
    run_id, _ = await crash_in(harness, files, "a/slow", {"a/stamp": "S"})
    second = FakeBackend({"a/stamp": "S", "a/slow": "ok", "b": "B", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["error"]["type"]) == ("failed", "diverged"), row
    assert second.calls == []


SWALLOWED_AT_THE_END = {"m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
initial: p
states:
  p:
    do:
      parallel:
        x: {machine: sub}
        y: {tool: quick}
    transitions: [{target: done}]
  done: {type: final}
""", "sub.yaml": """\
stategraph: 1
id: sub
python: sub.py
initial: a
states:
  a:
    do: {agent: w, task: a}
    finally: {tool: a_fin, idempotent: true}
    transitions: [{target: b}]
  b:
    do: {tool: stamp, args: {nonce: "{{ nonce() }}"}}
    transitions: [{target: c}]
  c:
    do: {agent: w, task: c}
    transitions: [{target: e}]
  e: {type: final}
""", "sub.py": "import uuid\n\n\ndef nonce():\n    return uuid.uuid4().hex\n"}


async def test_a_divergence_in_a_branch_ended_after_a_terminate_ends_the_run_diverged(harness):
    """The terminate reaches the resumed join before its branches began; x, replayed into its end afterwards,
    diverges. The join raises the terminate's cancel -- the run still ends diverged, not cancelled."""
    run_id, _ = await crash_in(harness, SWALLOWED_AT_THE_END, "p/x/c", {"p/x/a": "A", "p/x/a/finally": "ok",
                                                                        "p/x/b": "S", "p/y": "Q"})
    second = FakeBackend({})
    manager = harness.manager()
    await manager.resume(run_id, backend=second)
    asyncio.get_running_loop().call_soon(manager.control, run_id, "terminate")  # after the run's first step
    row = await settle(manager, run_id)

    assert (row["status"], row["error"]["type"]) == ("failed", "diverged"), row


def endless():
    async def handler(call):
        await asyncio.Event().wait()

    return handler


async def test_a_terminate_of_an_interrupted_run_bounds_the_finally_it_runs_again(harness, monkeypatch):
    """The terminate is journaled before the resume and carried out only where the journal ends; the finally that
    was in flight at the crash runs again before that -- within the bound, not unbounded."""
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 0.3)
    files = {"m.yaml": """\
stategraph: 1
id: m
initial: a
states:
  a:
    do: {agent: w, task: t}
    finally: {tool: a_fin, idempotent: true}
    transitions: [{target: b}]
  b:
    do: {agent: w, task: t}
    transitions: [{target: done}]
  done: {type: final}
"""}
    run_id, _ = await crash_in(harness, files, "a/finally", {"a": "A"})
    manager = harness.manager()
    await manager.resume(run_id, backend=FakeBackend({"a/finally": endless()}), cancel=True)
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert activity_rows(harness.store, run_id)["s1.fin.a"]["data"]["error"]["type"] == "timeout"


async def test_a_branch_replayed_into_an_end_of_its_own_gets_the_bound_for_its_ending(harness, monkeypatch):
    """y had finished and was closing its group when the join's cancel reached it; after the crash the close runs
    again -- within the bound the live run gave it, not unbounded."""
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 0.3)
    files = {**ENDING_BRANCHES, "sub.yaml": ENDING_BRANCHES["sub.yaml"].replace(
        "close: {tool: group_close}", "close: {tool: group_close, idempotent: true}")}
    gate = asyncio.Event()

    async def x_fails_later(call):
        await gate.wait()
        return ActivityError("agent_failed", "x broke")

    first = FakeBackend({**BRANCH_ANSWERS, "p/x/w": x_fails_later, "p/y/w": "W",
                         "p/y/w/finally": "tidied", "p/y/resources/group/close": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("p/y/resources/group/close") == 1, what="y finished, closing its group")
    gate.set()
    await asyncio.sleep(0.05)
    await manager.shutdown()

    row = await resume(harness, run_id, FakeBackend({"p/y/resources/group/close": endless(), "m.finally": "ok"}))

    assert (row["status"], row["final_state"]) == ("failed", "broken"), row
    assert activity_rows(harness.store, run_id)["s0/b.y/m/end.finished.close.group"]["data"]["error"]["type"] == \
        "timeout"


async def test_nothing_runs_live_after_a_divergence_a_call_caught(harness):
    """The call catches the divergence of its first tool call and goes on: its next tool call must not reach the
    backend -- the run ends diverged before anything runs live."""
    files = {"m.yaml": """\
stategraph: 1
id: m
python: m.py
initial: a
states:
  a:
    do: {call: work, idempotent: true}
    transitions: [{target: done}]
  done: {type: final}
""", "m.py": """import uuid


async def work(sg):
    try:
        await sg.tool("stamp", {"nonce": uuid.uuid4().hex}, idempotent=True)
    except Exception:
        pass
    await sg.tool("side_effect", {}, idempotent=True)
    await sg.tool("slow", {}, idempotent=True)
    return "ok"
"""}
    run_id, _ = await crash_in(harness, files, "a/slow", {"a/stamp": "S", "a/side_effect": "E"})
    second = FakeBackend({"a/stamp": "S", "a/side_effect": "E", "a/slow": "ok"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["error"]["type"]) == ("failed", "diverged"), row
    assert second.calls == []
    assert activity_rows(harness.store, run_id)["s0/t.2"]["status"] == "started"


async def test_a_non_idempotent_join_whose_branch_diverges_is_not_interrupted(harness):
    """In flight at the crash and not idempotent: its branches replay into their ends first, and y's replay
    diverges. That ends the run -- the composite does not end interrupted, and its error path does not run."""
    files = {**SWALLOWED_IN_JOIN, "m.yaml": SWALLOWED_IN_JOIN["m.yaml"].replace(
        "        y: {machine: suby}\n", "        y: {machine: suby}\n      idempotent: false\n")}
    gate = asyncio.Event()

    async def x_fails_later(call):
        await gate.wait()
        return ActivityError("agent_failed", "x broke")

    first = FakeBackend({"p/x": x_fails_later, "p/y/a": "stamped", "p/y/w": held(asyncio.Event()),
                         "p/y/suby.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("p/y/w") == 1, what="y in w")
    gate.set()
    await until(lambda: first.count("p/y/suby.finally") == 1, what="y's finally after the join cancelled it")
    await manager.shutdown()

    second = FakeBackend({"p/y/suby.finally": "ok", "handle": "H", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["error"]["type"]) == ("failed", "diverged"), row
    assert second.calls == []
    assert activity_rows(harness.store, run_id)["s0"]["status"] == "started"


async def test_a_frame_a_non_idempotent_composite_ends_completes_an_end_of_its_own_unbounded(harness, monkeypatch):
    """The submachine had finished and was in its (slow) finally at the crash. The composite is not started
    again, so its frame replays into that end -- nothing had cancelled it, so its finally is not cut by the
    cancel bound, as it was not in the live run."""
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 0.3)
    files = {"m.yaml": NOT_AGAIN["m.yaml"], "sub.yaml": """\
stategraph: 1
id: sub
finally: {tool: sub_fin, idempotent: true}
initial: w
states:
  w:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
"""}
    run_id, _ = await crash_in(harness, files, "s/sub.finally", {"s/w": "W"})
    row = await resume(harness, run_id, FakeBackend({"s/sub.finally": later(0.8), "handle": "H"}))

    assert (row["status"], row["final_state"]) == ("succeeded", "done"), row
    assert activity_rows(harness.store, run_id)["s0/m/end.finished.finally"]["status"] == "done"


NESTED_OWN_END = {"m.yaml": """\
stategraph: 1
id: m
imports: {suby: ./suby.yaml}
initial: p
states:
  p:
    do:
      parallel:
        x: {agent: w, task: x}
        y: {machine: suby}
    transitions:
      - target: done
      - {trigger: error, target: broken}
  done: {type: final}
  broken: {type: final, status: failed}
""", "suby.yaml": """\
stategraph: 1
id: suby
imports: {subz: ./subz.yaml}
initial: a
states:
  a:
    do: {tool: quick, idempotent: true}
    finally: {tool: a_fin, idempotent: true}
    transitions: [{target: b}]
  b:
    do: {machine: subz}
    transitions: [{target: e}]
  e: {type: final}
""", "subz.yaml": """\
stategraph: 1
id: subz
finally: {tool: z_fin, idempotent: true}
initial: w
states:
  w:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_a_cancel_that_reaches_a_replay_bounds_the_endings_below_it(harness, monkeypatch):
    """After the crash x fails at once, and the join's cancel reaches y while y replays a's finally. y replays on
    into z, which had finished and was in its finally: that finally gets the bound, as in the live run, where
    the cancel reached it there."""
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 0.3)
    first = FakeBackend({"p/x": held(asyncio.Event()), "p/y/a": "Q", "p/y/a/finally": "ok", "p/y/b/w": "W",
                         "p/y/b/subz.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(NESTED_OWN_END), backend=first)
    await until(lambda: first.count("p/y/b/subz.finally") == 1 and first.count("p/x") == 1, what="x, z's finally")
    await manager.shutdown()

    second = FakeBackend({"p/x": ActivityError("agent_failed", "x broke"), "p/y/b/subz.finally": later(0.8)})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["final_state"]) == ("failed", "broken"), row
    traces = [r for r in harness.store.rows(run_id, kinds=("trace",)) if r["status"] == "finally_failed"]
    assert [(t["state"], t["data"]["error"]["type"]) for t in traces] == [("p/y/b/subz.finally", "timeout")]


ABORT_IN_A_BRANCH = {"m.yaml": """\
stategraph: 1
id: m
imports: {subx: ./subx.yaml, suby: ./suby.yaml}
initial: p
states:
  p:
    do:
      parallel:
        x: {machine: subx}
        y: {machine: suby}
    transitions:
      - target: done
      - {trigger: error, target: broken}
  done: {type: final}
  broken: {type: final, status: failed}
""", "subx.yaml": """\
stategraph: 1
id: subx
limits: {max_steps: 2}
initial: a
states:
  a:
    do: {tool: tick}
    transitions:
      - {target: a, guard: "True"}
      - target: e
  e: {type: final}
""", "suby.yaml": """\
stategraph: 1
id: suby
finally: {tool: y_fin}
initial: w
states:
  w:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_a_step_limit_in_a_branch_keeps_its_outcome_when_a_terminate_comes_while_the_rest_ends(harness):
    """x's abort made the join cancel y; the terminate comes while y's finally runs: the join still raises the
    abort -- the run ends failed (step_limit), not cancelled."""
    gate, tick_gate = asyncio.Event(), asyncio.Event()

    async def tick(call):
        await tick_gate.wait()
        return "T"

    backend = FakeBackend({"p/x/a": tick, "p/y/w": held(asyncio.Event()), "p/y/suby.finally": held(gate, "ok")})
    manager = harness.manager()
    run_id = await manager.start(runnable(ABORT_IN_A_BRANCH), backend=backend)
    await until(lambda: backend.count("p/y/w") == 1, what="y in w")
    tick_gate.set()
    await until(lambda: backend.count("p/y/suby.finally") == 1, what="y's finally after x's abort")
    manager.control(run_id, "terminate")
    await asyncio.sleep(0.05)
    gate.set()
    row = await settle(manager, run_id)

    assert (row["status"], row["error"]["type"]) == ("failed", "step_limit"), row


async def test_a_finally_the_cancel_bound_cut_is_not_run_again_after_a_crash(harness, monkeypatch):
    """The bound's cut is that finally's outcome (a timeout) and journaled as such: the resume does not spend
    another bound on it."""
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 0.3)
    files = {"m.yaml": """\
stategraph: 1
id: m
initial: a
finally: {tool: m_fin, idempotent: true}
states:
  a:
    do: {agent: w, task: t}
    finally: {tool: a_fin, idempotent: true}
    transitions: [{target: done}]
  done: {type: final}
"""}
    first = FakeBackend({"a": held(asyncio.Event()), "a/finally": held(asyncio.Event()),
                         "m.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=first)
    await until(lambda: first.count("a") == 1, what="a in flight")
    manager.control(run_id, "terminate")
    await until(lambda: first.count("m.finally") == 1, what="a's finally cut, the machine's runs")
    await manager.shutdown()

    second = FakeBackend({"a/finally": "ran again", "m.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert row["status"] == "cancelled", row
    assert [c["path"] for c in second.calls] == ["m.finally"]
    assert activity_rows(harness.store, run_id)["end.cancelled.a"]["data"]["error"]["type"] == "timeout"


STEP_LIMIT_BELOW = {"m.yaml": """\
stategraph: 1
id: m
imports: {sub: ./sub.yaml}
finally: {tool: m_fin}
initial: s
states:
  s:
    do: {machine: sub}
    transitions: [{target: done}]
  done: {type: final}
""", "sub.yaml": """\
stategraph: 1
id: sub
limits: {max_steps: 2}
finally: {tool: sub_fin}
initial: a
states:
  a:
    do: {tool: tick}
    transitions:
      - {target: a, guard: "True"}
      - target: e
  e: {type: final}
"""}


async def test_a_step_limit_keeps_its_outcome_when_a_terminate_comes_during_its_finally(harness, monkeypatch):
    """The abort came first: a terminate that reaches its finally bounds the ending -- also the root's finally,
    which no cancel reaches -- but does not turn the run's outcome (step_limit, uncatchable) into cancelled."""
    from plugins.stategraph.engine import interpreter

    monkeypatch.setattr(interpreter, "FINALLY_CANCEL_TIMEOUT", 0.3)
    gate = asyncio.Event()
    backend = FakeBackend({"s/a": "T", "s/sub.finally": held(gate, "ok"), "m.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(STEP_LIMIT_BELOW), backend=backend)
    await until(lambda: backend.count("s/sub.finally") == 1, what="the aborted submachine's finally")
    manager.control(run_id, "terminate")
    await asyncio.sleep(0.05)
    gate.set()
    row = await settle(manager, run_id)

    assert (row["status"], row["error"]["type"]) == ("failed", "step_limit"), row
    traces = [r for r in harness.store.rows(run_id, kinds=("trace",)) if r["status"] == "finally_failed"]
    assert [(t["state"], t["data"]["error"]["type"]) for t in traces] == [("m.finally", "timeout")]


BRANCH_CLEANUP = {"m.yaml": FAIL_FAST_IN_TRANSITION["m.yaml"], "subx.yaml": FAIL_FAST_IN_TRANSITION["subx.yaml"],
                  "suby.yaml": """\
stategraph: 1
id: suby
imports: {cleanup: ./cleanup.yaml}
finally: {machine: cleanup}
initial: w
states:
  w:
    do: {agent: w, task: t}
    transitions: [{target: e}]
  e: {type: final}
""", "cleanup.yaml": """\
stategraph: 1
id: cleanup
imports: {inner: ./inner.yaml}
initial: tidy
states:
  tidy:
    do: {machine: inner, timeout: 0.3s}
    transitions:
      - target: e
      - {trigger: error, target: e}
  e: {type: final}
""", "inner.yaml": """\
stategraph: 1
id: inner
finally: {tool: inner_fin, args: {reason: "{{ ending.reason }}"}, idempotent: true}
initial: sweep
states:
  sweep:
    do: {tool: sweep, idempotent: true}
    transitions: [{target: e}]
  e: {type: final}
"""}


async def test_the_finally_of_a_branch_replayed_into_its_end_decides_its_own_end_anew(harness):
    """y is replayed into its end after the crash (the join had cancelled it) -- its finally is not: that runs
    live, and a frame inside it that a local cancel (a timeout) had ended decides its end anew."""
    gate = asyncio.Event()

    async def x_fails_later(call):
        await gate.wait()
        return ActivityError("agent_failed", "x broke")

    first = FakeBackend({"p/x/w": x_fails_later, "p/y/w": held(asyncio.Event()),
                         "p/y/suby.finally/tidy/sweep": held(asyncio.Event()),
                         "p/y/suby.finally/tidy/inner.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(BRANCH_CLEANUP), backend=first)
    await until(lambda: first.count("p/y/w") == 1, what="y in w")
    gate.set()
    await until(lambda: first.count("p/y/suby.finally/tidy/inner.finally") == 1, what="the timeout ended inner")
    await manager.shutdown()

    second = FakeBackend({"p/y/suby.finally/tidy/sweep": "swept", "p/y/suby.finally/tidy/inner.finally": "ok"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["final_state"]) == ("failed", "broken"), row
    assert [(c["path"], c["args"].get("reason")) for c in second.calls] == [
        ("p/y/suby.finally/tidy/sweep", None), ("p/y/suby.finally/tidy/inner.finally", "finished")]
    assert not [r for r in harness.store.rows(run_id, kinds=("trace",)) if r["status"] == "finally_failed"]


async def test_a_terminate_before_a_resumed_join_s_branches_began_still_ends_them(harness):
    """The terminate reaches the join as it hands its branches to the loop, before any began: each branch the
    crashed run had started still replays into its end, so tidy and close run for both."""
    first = FakeBackend({**BRANCH_ANSWERS, "p/x/w": held(asyncio.Event()), "p/y/w": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(ENDING_BRANCHES), backend=first)
    await until(lambda: first.count("p/x/w") == 1 and first.count("p/y/w") == 1, what="both branches in w")
    await manager.shutdown()

    second = FakeBackend({"p/x/w/finally": "tidied", "p/y/w/finally": "tidied", "p/x/resources/group/close": "closed",
                          "p/y/resources/group/close": "closed", "m.finally": "ok"})
    manager = harness.manager()
    await manager.resume(run_id, backend=second)
    asyncio.get_running_loop().call_soon(manager.control, run_id, "terminate")  # after the run's first step
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert sorted(c["path"] for c in second.calls) == [
        "m.finally", "p/x/resources/group/close", "p/x/w/finally", "p/y/resources/group/close", "p/y/w/finally"]


THREE_STEPS = {"m.yaml": """\
stategraph: 1
id: m
initial: a
finally: {tool: m_fin, idempotent: true}
states:
  a:
    do: {agent: w, task: a}
    finally: {tool: a_fin, args: {reason: "{{ ending.reason }}"}, idempotent: true}
    transitions: [{target: b}]
  b:
    do: {agent: w, task: b}
    finally: {tool: b_fin, args: {reason: "{{ ending.reason }}"}, idempotent: true}
    transitions: [{target: c}]
  c:
    do: {agent: w, task: c}
    finally: {tool: c_fin, args: {reason: "{{ ending.reason }}"}, idempotent: true}
    transitions: [{target: done}]
  done: {type: final}
"""}


async def test_a_terminate_that_reaches_a_replay_ends_the_frame_where_its_journal_ends(harness):
    """A replay takes no time: the terminate that reaches the resumed frame while it replays a's finally ends it
    where the crashed run stood (in c) -- not in b, whose finally had already run."""
    run_id, _ = await crash_in(harness, THREE_STEPS, "c", {"a": "A", "a/finally": "ok", "b": "B", "b/finally": "ok"})
    second = FakeBackend({"c/finally": "ok", "m.finally": "ok"})
    manager = harness.manager()
    await manager.resume(run_id, backend=second)
    asyncio.get_running_loop().call_soon(manager.control, run_id, "terminate")  # while a's finally replays
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert [(c["path"], c["args"].get("reason")) for c in second.calls] == [
        ("c/finally", "cancelled"), ("m.finally", None)]


EDITED_AT_ENTER = {"m.yaml": """\
stategraph: 1
id: m
context: {flag: false}
initial: s
finally: {tool: m_fin, args: {flag: "{{ ctx.flag }}"}, idempotent: true}
states:
  s:
    do: {agent: w, task: s}
    finally: {tool: s_fin, idempotent: true}
    transitions: [{target: t}]
  t:
    do: {agent: w, task: t}
    transitions: [{target: done}]
  done: {type: final}
"""}


async def test_a_terminate_that_reaches_a_replay_keeps_an_edit_made_where_the_frame_stood(harness):
    """The crashed run paused at t's enter hook, and someone set ctx.flag there. The terminate reaches the resumed
    frame while it replays s's finally: the frame replays on to t's enter hook -- the edit applies -- and ends."""
    first = FakeBackend({"s": "S", "s/finally": "ok"})
    manager = harness.manager()
    run_id = await manager.start(runnable(EDITED_AT_ENTER), backend=first, breakpoints=["t"])
    await until(lambda: manager.live[run_id].ctx.debugger.paused is not None, what="the pause at t's enter")
    manager.assign(run_id, "ctx.flag", "True")
    await manager.shutdown()

    second = FakeBackend({"m.finally": "ok"})
    manager = harness.manager()
    await manager.resume(run_id, backend=second)
    asyncio.get_running_loop().call_soon(manager.control, run_id, "terminate")  # while s's finally replays
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert [(c["path"], c["args"]) for c in second.calls] == [("m.finally", {"flag": True})]


async def test_a_terminate_during_the_end_of_a_failed_run_keeps_its_outcome_after_a_crash(harness):
    """The run had failed (an unhandled error) and ran its finally when the terminate came: the resume replays
    into that end -- the failing step is not taken for one past the journal -- and the outcome stands."""
    first = FakeBackend({"resources/store/open": "ns_000001", "a": ActivityError("agent_failed", "no"),
                         "a/finally": "ok", "m.finally": held(asyncio.Event())})
    manager = harness.manager()
    run_id = await manager.start(runnable(LAST_STEP_FINALLY), backend=first)
    await until(lambda: first.count("m.finally") == 1, what="the finally of the failed run")
    manager.control(run_id, "terminate")
    await asyncio.sleep(0.05)
    await manager.shutdown()

    second = FakeBackend({"m.finally": "ok", "resources/store/close": "closed"})
    row = await resume(harness, run_id, second)

    assert (row["status"], row["error"]["type"]) == ("failed", "agent_failed"), row
    assert reasons(first, second) == ["failed", "failed"]


async def test_a_leaf_that_fails_under_a_terminate_ends_cancelled_without_that_outcome(harness):
    """The backend turns the terminate's cancel into a failure: that failure is not a's outcome -- journaled, it
    would replay as one and take the error transition."""
    files = {"m.yaml": """\
stategraph: 1
id: m
initial: a
states:
  a:
    do: {agent: w, task: t}
    transitions:
      - target: done
      - {trigger: error, target: handle}
  handle:
    do: {agent: w, task: h}
    transitions: [{target: done}]
  done: {type: final}
"""}

    async def gives_up(call):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return ActivityError("agent_failed", "cancelled underneath")

    backend = FakeBackend({"a": gives_up, "handle": "H"})
    manager = harness.manager()
    run_id = await manager.start(runnable(files), backend=backend)
    await until(lambda: backend.count("a") == 1, what="a in flight")
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert activity_rows(harness.store, run_id)["s0"]["status"] == "started"


async def test_a_terminate_before_the_run_began_ends_it_with_its_finally(harness):
    """The terminate comes before the run's task ran a line: the run carries it out when it begins -- the task
    is not cancelled before its first line, which would leave the run 'running' for good. The frame had
    entered a when it ends there, so a's finally runs too (as for a terminate at a's enter hook)."""
    backend = FakeBackend({"a/finally": "ok", "m.finally": "ok"})
    manager = harness.manager()
    run_id = await manager.start(runnable(THREE_STEPS), backend=backend)
    manager.control(run_id, "terminate")
    row = await settle(manager, run_id)

    assert row["status"] == "cancelled", row
    assert [(c["path"], c["args"].get("reason")) for c in backend.calls] == [
        ("a/finally", "cancelled"), ("m.finally", None)]
    assert run_id not in manager.live
