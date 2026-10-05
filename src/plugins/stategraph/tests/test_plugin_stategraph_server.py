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


async def test_a_read_right_after_send_event_shows_what_the_event_started(server):
    result, _ = await run_tool(server, "stategraph_run_machine",
                               {"machine_id": "approval", "mock_only": True, "max_wait": 20})
    await run_tool(server, "stategraph_send_event", {"run_id": result["run_id"], "name": "approve"})

    now, _ = await run_tool(server, "stategraph_get_run", {"run_id": result["run_id"]})

    assert "inbox" not in now and (now["run_status"], now["state"]) != ("waiting", "waiting_for_ok"), now


async def test_get_run_wait_finish_waits_while_a_waiting_frame_has_not_taken_its_event_yet(server):
    result, _ = await run_tool(server, "stategraph_run_machine",
                               {"machine_id": "approval", "mock_only": True, "max_wait": 20})
    # the service's own send_event only queues it -- as the panel and a callback URL send one
    server.service.send_event(result["run_id"], "approve")

    finished, _ = await run_tool(server, "stategraph_get_run", {"run_id": result["run_id"], "wait": "finish"})

    assert finished["run_status"] == "succeeded", finished


async def test_run_manager_wait_goes_on_until_the_run_took_its_event_here_and_in_another_process(server, monkeypatch):
    result, _ = await run_tool(server, "stategraph_run_machine",
                               {"machine_id": "approval", "mock_only": True, "max_wait": 20})
    server.service.send_event(result["run_id"], "approve")

    here = await server.run_manager.wait(result["run_id"], timeout=5.0)

    assert here["status"] == "succeeded", here
    due = {"id": "x", "status": "waiting", "view": {"frames": [{"prefix": "", "accepts": ["approve"]}],
                                                     "inbox": [{"name": "approve", "frame": None}]}}
    rows = iter([due, {"id": "x", "status": "succeeded", "view": {}}])
    monkeypatch.setattr(server.run_manager.store, "get_run", lambda run_id: next(rows))
    elsewhere = await server.run_manager.wait("x", timeout=5.0)  # not live here: read from its row
    assert elsewhere["status"] == "succeeded", elsewhere


async def test_get_run_goes_on_waiting_while_another_processs_run_has_not_taken_its_event(server, monkeypatch):
    due = {"id": "x", "status": "waiting", "view": {"frames": [{"prefix": "", "accepts": ["approve"]}],
                                                     "inbox": [{"name": "approve", "frame": None}]}}
    rows = iter([due, {"id": "x", "status": "succeeded", "view": {}}])

    async def wait(run_id, timeout=None):  # run_manager.wait answering at its timeout: still due, then done
        return next(rows)

    monkeypatch.setattr(server.run_manager, "wait", wait)

    assert (await server._wait("x", 5.0, None, terminate=False))["status"] == "succeeded"


async def test_another_process_pauses_continues_and_terminates_a_run_it_does_not_hold(server, tmp_path):
    """Two servers on one runs.db are two processes: the second controls the first's run through the row."""
    from plugins.stategraph.service import ServiceError

    # its finally activity takes a moment: a terminate from elsewhere answers once the run ended, not before
    (tmp_path / "machines" / "lingering.yaml").write_text(
        APPROVAL.replace("id: approval", "id: lingering\npython: lingering.py\nfinally: {call: linger}"), encoding="utf-8")
    (tmp_path / "machines" / "lingering.py").write_text(
        "import asyncio\n\nasync def linger():\n    await asyncio.sleep(0.5)\n", encoding="utf-8")
    other = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path, allowed_users=["ops_*"]))
    try:
        result, _ = await run_tool(server, "stategraph_run_machine",
                                   {"machine_id": "lingering", "mock_only": True, "max_wait": 20})
        run_id = result["run_id"]
        owner = server.run_manager.live[run_id].ctx.debugger
        assert run_id not in other.run_manager.live, "fixture: the second holds nothing"

        with pytest.raises(ServiceError) as refused:  # its state is checked against the machine where the run is
            await other.service.control_run(run_id, "run_to", state="approved")
        assert refused.value.status == 409 and owner.run_to is None, refused.value.message
        paused = await other.service.control_run(run_id, "pause")
        assert (owner.mode, paused["debug"]["mode"]) == ("pause", "pause"), paused["debug"]
        await other.service.control_run(run_id, "continue")
        assert owner.mode == "run"
        await other.service.control_run(run_id, "step")
        assert owner.mode == "step"
        ended = await other.service.control_run(run_id, "terminate")
        assert ended["status"] == "cancelled", ended
    finally:
        await other.stop_plugin()


async def test_a_control_request_its_holder_does_not_take_is_withdrawn_and_refused(server, monkeypatch):
    from plugins.stategraph import service as service_module
    from plugins.stategraph.service import ServiceError
    from plugins.stategraph.tests.stategraph_testkit import seed_run

    monkeypatch.setattr(service_module, "CONTROL_WAIT", 0.3)
    seed_run(server.run_store, "held", {"m.yaml": APPROVAL.replace("id: approval", "id: m")},
             owner="elsewhere:1:x", lease=60, status="waiting")

    with pytest.raises(ServiceError) as refused:
        await server.service.control_run("held", "pause")

    assert refused.value.status == 409 and "elsewhere:1:x" in refused.value.message, refused.value.message
    assert server.run_store.get_run("held")["control"] is None, "withdrawn: a late holder must not pause it anyway"


async def test_one_control_request_at_a_time_and_a_stopped_asker_takes_its_own_back(server, monkeypatch):
    import asyncio

    from plugins.stategraph import service as service_module
    from plugins.stategraph.service import ServiceError
    from plugins.stategraph.tests.stategraph_testkit import seed_run

    monkeypatch.setattr(service_module, "CONTROL_WAIT", 1.0)
    seed_run(server.run_store, "held", {"m.yaml": APPROVAL.replace("id: approval", "id: m")},
             owner="elsewhere:1:x", lease=60, status="waiting")  # nobody polls: requests stay open
    first = asyncio.ensure_future(server.service.control_run("held", "terminate"))
    await _tick()
    assert server.run_store.get_run("held")["control"]["action"] == "terminate", "fixture: the first is open"

    with pytest.raises(ServiceError) as second:
        await server.service.control_run("held", "pause")

    assert second.value.status == 409 and "another control request" in second.value.message, second.value.message
    assert server.run_store.get_run("held")["control"]["action"] == "terminate", "the second wrote over the first"
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert server.run_store.get_run("held")["control"] is None, "a stopped asker left its request for a later resume"


def test_a_sweep_or_a_new_owner_drops_a_request_nobody_took(tmp_path):
    from plugins.stategraph.engine.journal import RunStore
    from plugins.stategraph.tests.stategraph_testkit import seed_run, utc_at

    store = RunStore(tmp_path / "runs.db")
    machine = {"m.yaml": APPROVAL.replace("id: approval", "id: m")}
    for run_id in ("swept", "taken", "mine"):
        seed_run(store, run_id, machine, owner="old:1:x", lease=60, status="waiting")
        assert store.request_control(run_id, {"action": "pause", "id": run_id}, now=utc_at(0)) == "requested"
    expire = "UPDATE runs SET lease_until = ? WHERE id = ?"

    store._db().execute(expire, (utc_at(-5), "swept"))
    store.mark_expired(now=utc_at(0))
    store._db().execute(expire, (utc_at(-5), "taken"))  # after the sweep: only the new owner can drop it
    store.take_lease("taken", "new:2:y", utc_at(60), now=utc_at(0))
    store.take_lease("mine", "old:1:x", utc_at(60), now=utc_at(0))  # its own owner again: it still takes it

    assert [store.get_run(r)["control"] for r in ("swept", "taken")] == [None, None]
    assert store.get_run("mine")["control"] == {"action": "pause", "id": "mine"}
    store.close()


def test_an_old_runs_db_gets_the_control_column_and_its_index(tmp_path):
    import sqlite3

    from plugins.stategraph.engine.journal import RunStore

    first = RunStore(tmp_path / "runs.db")
    first._db().execute("DROP INDEX runs_control")
    first._db().execute("ALTER TABLE runs DROP COLUMN control")  # as a runs.db from before it
    first.close()

    RunStore(tmp_path / "runs.db")._db()

    db = sqlite3.connect(tmp_path / "runs.db")
    assert "control" in {row[1] for row in db.execute("PRAGMA table_info(runs)")}
    assert "runs_control" in {row[1] for row in db.execute("PRAGMA index_list(runs)")}
    db.close()


def test_a_row_names_an_event_due_only_for_a_frame_that_waits_for_it():
    from plugins.stategraph.engine.runner import row_event_due

    def row(status="waiting", accepts=("approve",), inbox=({"name": "approve", "frame": None},)):
        return {"status": status, "view": {"frames": [{"prefix": "", "accepts": list(accepts)}], "inbox": list(inbox)}}

    assert row_event_due(row())
    assert row_event_due(row(inbox=[{"name": "approve", "frame": ""}]))
    assert not row_event_due(row(inbox=[{"name": "approve", "frame": "s3/m/"}])), "for another frame"
    assert not row_event_due(row(accepts=["reject"])), "an event no frame takes stays queued: no wait for it"
    assert not row_event_due(row(inbox=[])), "nothing sent"
    assert not row_event_due(row(status="paused")), "a paused run takes nothing"


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


async def test_get_run_refuses_steps_of_zero_as_control_run_does(server):
    """steps 0 is out of range: read as "not given", it answered 30 rows the caller did not ask for."""
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "mock_only": True})

    result, closing = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"], "steps": 0})

    assert result["status"] == "error" and "steps must be" in result["error"], result
    assert closing.phase is StatusPhase.ERROR


async def test_saving_a_machines_own_file_without_its_version_says_to_pass_it(server):
    """A save of an existing machine without expected_versions: the answer names the fix, not "not one of its files"
    -- which stays the answer for a stray file of that name (the panel's module dialog reads it)."""
    result, _ = await run_tool(server, "stategraph_save_machine", {"files": {"hello.yaml": HELLO}})

    assert result["status"] == "error" and result["error_type"] == "http_409", result
    assert "expected_versions" in result["error"] and "not one of its files" not in result["error"], result

    (server.machines.find("hello").path.parent / "stray.py").write_text("x = 1\n", encoding="utf-8")
    version = server.service.get_machine("hello")["versions"]["hello.yaml"]
    stray, _ = await run_tool(server, "stategraph_save_machine",
                              {"files": {"hello.yaml": HELLO, "stray.py": "y = 2\n"},
                               "expected_versions": {"hello.yaml": version}})
    assert stray["status"] == "error" and stray["error"].startswith("stray.py exists already"), stray


async def test_the_catalog_describes_every_field_of_every_kind(server):
    """A field without a description showed the model "" (retry) or its bare type ("string")."""
    result, _ = await run_tool(server, "stategraph_catalog", {})

    bare = {(kind["key"], name) for kind in result["kinds"] for name, text in kind["fields"].items()
            if text in ("", "string", "integer", "number", "boolean", "object", "array")}
    assert not bare, sorted(bare)


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


async def test_an_authorization_refusal_has_its_error_type(server, monkeypatch):
    _enable_auth(server, monkeypatch, {"alice": "USER"})

    refused, _ = await run_tool(server, "stategraph_run_machine",
                                {"machine_id": "hello", "mock_only": True, "_user_id": "alice"})

    assert refused["status"] == "error" and refused["error_type"] == "http_403", refused


async def test_control_run_refuses_an_argument_it_does_not_take(server):
    """A typo (stat for state) was dropped without a word: the action ran as if it had not been given."""
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "mock_only": True})

    typo, _ = await run_tool(server, "stategraph_control_run",
                             {"run_id": started["run_id"], "action": "terminate", "stat": "greet"})
    fine, _ = await run_tool(server, "stategraph_control_run",
                             {"run_id": started["run_id"], "action": "terminate", "_framework_key": 1})

    assert typo["status"] == "error" and typo["error_type"] == "http_422" and "stat" in typo["error"], typo
    assert fine["status"] == "success", fine


CALLBACK = """\
stategraph: 1
id: ask
events: {approve: {}}
context: {link: null}
initial: ask
states:
  ask:
    do: {callback: approve}
    transitions:
      - target: waiting
        effect: ctx.link = out["url"]
  waiting:
    transitions:
      - {trigger: approve, target: done}
  done: {type: final}
"""


async def test_get_run_hides_callback_tokens_from_a_reader_who_is_not_the_runs_user_or_an_admin(server, monkeypatch):
    """A run of nobody is visible to every user; its callback URL would let one who may not send_event fire it."""
    import json

    (server.machines.find("hello").path.parent / "ask.yaml").write_text(CALLBACK, encoding="utf-8")
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "ask", "mock_only": True})
    assert started["run_status"] == "waiting", started
    token = server._callback_tokens(started["run_id"])
    assert len(token) == 1
    token = token.pop()
    _enable_auth(server, monkeypatch, {"alice": "USER", "root": "ADMIN"})

    seen_by_user, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"], "_user_id": "alice"})
    seen_by_admin, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"], "_user_id": "root"})
    seen_by_nobody, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"]})

    assert seen_by_user["status"] == "success", seen_by_user
    assert token not in json.dumps(seen_by_user) and "<hidden>" in json.dumps(seen_by_user["frames"])
    assert token not in json.dumps(seen_by_nobody), "a caller without a user is not the user of a run of nobody"
    assert token in json.dumps(seen_by_admin["frames"]) and token in json.dumps(seen_by_admin["journal"])


CUT = """\
stategraph: 1
id: cut
python: cut.py
events: {approve: {}}
context: {note: null, links: {}}
initial: ask
states:
  ask:
    do: {callback: approve}
    transitions:
      - target: waiting
        effect: |
          ctx.note = pad(out["url"])
          ctx.links = {out["url"]: "sent"}
  waiting:
    transitions:
      - {trigger: approve, target: done}
  done: {type: final}
"""
# the token starts 10 characters before the 2,000 at which get_run cuts a text
CUT_PY = "def pad(url):\n    at = url.index('token=') + 6\n    return 'x' * (1990 - at) + url + 'tail'\n"


async def test_a_token_across_the_cut_and_one_in_a_key_are_hidden_whole(server, monkeypatch):
    """Hidden after the cut, a token cut in two kept its first part (42 of 43 characters were measured); a ctx keyed
    by the URL showed it whole in the key."""
    import json

    folder = server.machines.find("hello").path.parent
    (folder / "cut.yaml").write_text(CUT, encoding="utf-8")
    (folder / "cut.py").write_text(CUT_PY, encoding="utf-8")
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "cut", "mock_only": True})
    assert started["run_status"] == "waiting", started
    token = server._callback_tokens(started["run_id"]).pop()
    _enable_auth(server, monkeypatch, {"alice": "USER"})

    seen, _ = await run_tool(server, "stategraph_get_run", {"run_id": started["run_id"], "_user_id": "alice"})

    text = json.dumps(seen)
    assert token[:8] not in text and token[-8:] not in text, "a part of the token is visible"
    assert "characters)" in json.dumps(seen["frames"]), "the note was not cut: the test does not reach the cut"


async def test_control_run_takes_the_request_id_the_framework_adds(server):
    """An agent's programmatic dispatch adds request_id and requestId without the "_" (tools/base.py reads them)."""
    started, _ = await run_tool(server, "stategraph_run_machine", {"machine_id": "hello", "mock_only": True})

    result, _ = await run_tool(server, "stategraph_control_run", {"run_id": started["run_id"], "action": "terminate",
                                                                  "request_id": "r1", "requestId": "r1"})

    assert result["status"] == "success", result


async def test_allowed_users_pass_without_a_role_lookup(server, monkeypatch):
    looked_up = _enable_auth(server, monkeypatch, {})
    result, _ = await run_tool(server, "stategraph_validate_machine", {"yaml": HELLO, "_user_id": "ops_anna"})
    assert result["status"] == "success", result
    assert looked_up == [], "an allowed_users match must not need the user database"


async def _tick():
    import asyncio

    await asyncio.sleep(0.02)


async def test_an_edit_renders_the_file_off_the_event_loop(server, monkeypatch):
    """A batch renders the file once per edit it holds -- seconds on a large machine: the API must go on meanwhile."""
    import asyncio
    import time

    from plugins.stategraph.model import yamledit

    real = yamledit.apply_op

    def slow(text, op):
        time.sleep(0.3)
        return real(text, op)

    monkeypatch.setattr(yamledit, "apply_op", slow)
    ticks = []

    async def ticker():
        while True:
            ticks.append(1)
            await asyncio.sleep(0.02)

    version = server.service.get_machine("hello")["versions"]["hello.yaml"]
    beat = asyncio.create_task(ticker())
    try:
        answer = await server.service.edit_machine("hello", {"op": "add_state", "name": "later"}, version)
    finally:
        beat.cancel()

    assert any(state["name"] == "later" for state in answer["graph"]["states"])
    assert len(ticks) >= 5, f"the event loop stood still during the edit ({len(ticks)} ticks in 0.3 s)"


async def test_an_edit_onto_drafts_writes_nothing_and_answers_the_draft(server, tmp_path):
    """Without auto-save the panel's edits go onto its drafts: the file stays, the answer is the draft and its graph."""
    file = tmp_path / "machines" / "hello.yaml"
    saved = file.read_bytes()

    first = await server.service.edit_machine("hello", {"op": "add_state", "name": "later"}, None, drafts={})
    second = await server.service.edit_machine("hello", {"op": "add_state", "name": "after"}, None,
                                               drafts={"hello.yaml": first["draft"]})

    assert file.read_bytes() == saved, "a draft edit wrote the file"
    names = {state["name"] for state in second["graph"]["states"]}
    assert {"later", "after"} <= names, "the second edit did not go onto the first one's draft"
    assert "after:" in second["draft"] and "later:" in second["draft"]
    assert set(second) == {"machine_id", "problems", "graph", "draft"}, "no saved files or versions to go stale"


async def test_an_edit_onto_a_draft_that_does_not_parse_says_so(server):
    from plugins.stategraph.service import ServiceError

    with pytest.raises(ServiceError) as refused:
        await server.service.edit_machine("hello", {"op": "add_state", "name": "later"}, None,
                                          drafts={"hello.yaml": "stategraph: 1\nstates: [\n"})

    assert refused.value.status == 422 and "YAML tab" in str(refused.value)
