"""The stategraph tools through the framework's real path (docs/stategraph_design.md §8).

Every call goes through ``call_with_status`` -> StatusScope -> status bus, as an
agent's tool call does (the pattern of tests/plugins/test_status_end_lines.py):
exactly one END or ERROR closes the scope, and that one line must name the
result. The server is the real ``StateGraphServer`` with its machine root and
runs.db under tmp_path; machines use ``call`` activities, so no LLM is needed.

Mutation checks run (each turned the named tests red, then was restored from a copy):
- server._run_tool: ``status.end(...)`` removed (scope default "completed")      -> test_run_machine_*_line_names_the_result
- server._authorize: role check accepts any active user                         -> test_admin_refusal_when_auth_is_enabled
- server._authorize: allowed_users ignored                                        -> test_allowed_users_pass_without_a_role_lookup
- server._wait: returns only on terminal statuses                                -> test_run_machine_wait_finish_returns_when_the_run_waits (timeout)
- server._run_tool: ServiceError returned as success                             -> test_errors_have_the_error_shape
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_system.config.models import AgentSystemConfig
from agent_system.tools.status import StatusPhase, get_status_bus
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, tool_config

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

HELLO = """\
stategraph: 1
id: hello
python: hello.py
params: {name: {type: string, default: world}}
context: {greeting: null}
initial: greet
states:
  greet:
    do: {call: greet, args: {name: "{{ params.name }}"}}
    transitions:
      - target: done
        effect: ctx.greeting = out
  done: {type: final, output: {greeting: "{{ ctx.greeting }}"}}
"""
HELLO_PY = "def greet(name):\n    return f'Hello, {name}!'\n"

APPROVAL = """\
stategraph: 1
id: approval
events: {approve: {}}
initial: waiting_for_ok
states:
  waiting_for_ok:
    transitions:
      - trigger: approve
        target: approved
  approved: {type: final, output: approved}
"""

BROKEN = """\
stategraph: 1
id: hello
initial: greet
states:
  greet:
    transitions:
      - target: nowhere
"""

MAX_LINE = 140


@pytest.fixture
async def server(tmp_path):
    srv = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path, allowed_users=["ops_*"]))
    machines = tmp_path / "machines"
    (machines / "hello.yaml").write_text(HELLO, encoding="utf-8")
    (machines / "hello.py").write_text(HELLO_PY, encoding="utf-8")
    (machines / "approval.yaml").write_text(APPROVAL, encoding="utf-8")
    yield srv
    await srv.stop_plugin()


async def run_tool(server, action: str, params: dict):
    """One real tool call; returns the result and the ONE event that closed the scope."""
    bus = get_status_bus()
    method = action[len(server.name) + 1:]
    queue = await bus.subscribe(server=f"{server.name}.{method}()")
    try:
        result = await server.call_with_status(action, params)
    finally:
        bus.unsubscribe(queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    closing = [e for e in events if e.phase in (StatusPhase.END, StatusPhase.ERROR)]
    assert len(closing) == 1, f"{action}: expected one closing event, got {[(e.phase, e.message) for e in events]}"
    assert closing[0].message and len(closing[0].message) <= MAX_LINE, closing[0].message
    return result, closing[0]


# ------------------------------------------------------------------ run_machine

async def test_run_machine_to_the_end_line_names_the_result(server):
    result, closing = await run_tool(server, "stategraph_run_machine",
                                     {"machine_id": "hello", "params": {"name": "Ada"}, "mock_only": True})

    assert result["status"] == "success", result
    assert result["run_status"] == "succeeded"
    assert result["output"] == {"greeting": "Hello, Ada!"}
    assert closing.phase is StatusPhase.END
    for needle in (result["run_id"], "succeeded", "done"):
        assert needle in closing.message, f"{closing.message!r} does not name {needle!r}"


async def test_run_machine_wait_finish_returns_when_the_run_waits(server):
    import time

    started = time.monotonic()
    result, closing = await run_tool(server, "stategraph_run_machine",
                                     {"machine_id": "approval", "mock_only": True, "max_wait": 20})
    assert time.monotonic() - started < 5, "wait=finish sat out max_wait instead of returning on the wait"

    assert result["run_status"] == "waiting", result
    assert result["state"] == "waiting_for_ok"
    assert result["accepts"] == [{"frame": "", "events": ["approve"]}]
    assert "waiting" in closing.message and "waiting_for_ok" in closing.message

    sent, sent_line = await run_tool(server, "stategraph_send_event", {"run_id": result["run_id"], "name": "approve"})
    assert sent["accepted"] is True and sent["frame"] == ""
    finished, _ = await run_tool(server, "stategraph_get_run", {"run_id": result["run_id"]})
    for _ in range(100):
        if finished["run_status"] != "running" and finished["run_status"] != "waiting":
            break
        finished = await server.get_run({"run_id": result["run_id"]})
        await _tick()
    assert finished["run_status"] == "succeeded", finished


async def test_send_event_line_does_not_call_an_accepted_event_queued(server):
    result, _ = await run_tool(server, "stategraph_run_machine",
                               {"machine_id": "approval", "mock_only": True, "max_wait": 20})
    sent, line = await run_tool(server, "stategraph_send_event", {"run_id": result["run_id"], "name": "approve"})
    assert sent["queued"] is False
    assert "queued" not in line.message, line.message


async def test_run_machine_wait_finish_returns_on_a_pause(server):
    result, closing = await run_tool(server, "stategraph_run_machine",
                                     {"machine_id": "hello", "mock_only": True, "breakpoints": ["greet@exit"],
                                      "max_wait": 20})
    assert result["run_status"] == "paused", result
    assert result["paused"]["state"] == "greet" and result["paused"]["hook"] == "exit"
    assert "paused" in closing.message

    value, value_line = await run_tool(server, "stategraph_control_run",
                                       {"run_id": result["run_id"], "action": "evaluate", "expr": "out"})
    assert value["value"] == "Hello, world!"
    assert "Hello, world!" in value_line.message
    resumed, resumed_line = await run_tool(server, "stategraph_control_run",
                                           {"run_id": result["run_id"], "action": "continue"})
    assert resumed["action"] == "continue" and result["run_id"] in resumed_line.message


# ------------------------------------------------------------------ machines

async def test_validate_machine_reports_problems_as_a_result(server):
    result, closing = await run_tool(server, "stategraph_validate_machine", {"yaml": BROKEN})
    assert result["status"] == "success"
    assert result["valid"] is False
    assert [p["code"] for p in result["problems"] if p["level"] == "error"] == ["SG002"]
    assert closing.phase is StatusPhase.END
    assert "1 error(s)" in closing.message and "hello" in closing.message


async def test_list_and_get_machine_lines_carry_counts(server):
    listed, line = await run_tool(server, "stategraph_list_machines", {})
    assert sorted(m["id"] for m in listed["machines"]) == ["approval", "hello"]
    assert "2 machine(s)" in line.message
    got, got_line = await run_tool(server, "stategraph_get_machine", {"machine_id": "hello"})
    assert sorted(got["files"]) == ["hello.py", "hello.yaml"]
    assert "hello" in got_line.message and "2 file(s)" in got_line.message


# ------------------------------------------------------------------ errors

async def test_errors_have_the_error_shape(server):
    result, closing = await run_tool(server, "stategraph_get_run", {"run_id": "no_such_run"})
    assert result["status"] == "error"
    assert "no_such_run" in result["error"]
    assert result["error_type"] == "http_404"
    assert closing.phase is StatusPhase.ERROR and "no_such_run" in closing.message

    refused, refused_line = await run_tool(server, "stategraph_save_machine",
                                           {"files": {"hello.yaml": BROKEN}, "machine_id": "hello"})
    assert refused["status"] == "error" and "not saved" in refused["error"]
    assert refused_line.phase is StatusPhase.ERROR
    assert (server.machines.find("hello").path.read_text(encoding="utf-8")) == HELLO, "a refused save wrote the file"


# ------------------------------------------------------------------ authorization (§8.3)

def _enable_auth(server, monkeypatch, role_of: dict):
    from agent_system.auth.models import UserRole

    server.system_config.auth.enabled = True
    looked_up: list[str] = []

    def lookup(username):
        looked_up.append(username)
        role = role_of.get(username)
        return None if role is None else SimpleNamespace(is_active=True, role=getattr(UserRole, role))

    monkeypatch.setattr("agent_system.auth.database.get_user_by_username", lookup)
    return looked_up


async def test_admin_refusal_when_auth_is_enabled(server, monkeypatch):
    _enable_auth(server, monkeypatch, {"alice": "USER", "root": "ADMIN"})

    refused, line = await run_tool(server, "stategraph_run_machine",
                                   {"machine_id": "hello", "mock_only": True, "_user_id": "alice"})
    assert refused["status"] == "error" and "only admins" in refused["error"], refused
    assert line.phase is StatusPhase.ERROR and "alice" in line.message
    assert server.run_store.list_runs() == [], "a refused call started a run"

    anonymous, _ = await run_tool(server, "stategraph_validate_machine", {"yaml": HELLO})
    assert anonymous["status"] == "error", "no _user_id must not pass when auth is on"

    allowed, _ = await run_tool(server, "stategraph_run_machine",
                                {"machine_id": "hello", "mock_only": True, "_user_id": "root"})
    assert allowed["status"] == "success" and allowed["run_status"] == "succeeded", allowed

    read_only, _ = await run_tool(server, "stategraph_list_machines", {"_user_id": "alice"})
    assert read_only["status"] == "success", "read-only tools stay open"


async def test_allowed_users_pass_without_a_role_lookup(server, monkeypatch):
    looked_up = _enable_auth(server, monkeypatch, {})
    result, _ = await run_tool(server, "stategraph_validate_machine", {"yaml": HELLO, "_user_id": "ops_anna"})
    assert result["status"] == "success", result
    assert looked_up == [], "an allowed_users match must not need the user database"


async def _tick():
    import asyncio

    await asyncio.sleep(0.02)
