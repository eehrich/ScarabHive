"""The runs a chat session or answer started: kept with each run as it starts, listed by them, and offered in the
chat's "Open in" menu (has_session_data) -- saved machines and dynamic workflows alike."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from agent_system.config.models import AgentSystemConfig
from plugins.stategraph.engine.journal import RunStore
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, FakeBackend, tool_config, utc_at
from plugins.stategraph.tests.test_plugin_stategraph_workflows import (REVIEW, SERVERS, SETTINGS, fake_backends,
                                                                        settled)

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

TINY = "stategraph: 1\nid: tiny\ninitial: done\nstates:\n  done: {type: final}\n"

PARENT = """\
stategraph: 1
id: parent
python: parent.py
initial: run
states:
  run:
    do: {call: run, args: {}}
    transitions: [{target: done}]
  done: {type: final}
"""

# a step of the parent's work, not a hand-off: the run it starts is its child -- started with a session of its own,
# as an agent inside the run would pass its own
PARENT_PY = """\
async def run(sg):
    await sg.live["service"].start_run("tiny", caller_session="agent-own")
"""


# a call that resumes a run by its key: the run is the call's child from then on
KEYED = PARENT.replace("id: parent", "id: keyed").replace("parent.py", "keyed.py")
KEYED_PY = """\
async def run(sg):
    await sg.live["service"].start_run("tiny", run_key="ticket-7")
"""


@pytest.fixture
async def server(tmp_path):
    made = StateGraphServer("stategraph", AgentSystemConfig(plugins={"servers": SERVERS}),
                            tool_config(tmp_path, workflows=dict(SETTINGS)))
    for name, text in (("tiny.yaml", TINY), ("parent.yaml", PARENT), ("parent.py", PARENT_PY),
                       ("keyed.yaml", KEYED), ("keyed.py", KEYED_PY)):
        (tmp_path / "machines" / name).write_text(text, encoding="utf-8")
    yield made
    await made.stop_plugin()


def ids(runs):
    return sorted(run["id"] for run in runs)


async def test_a_run_started_from_a_chat_is_listed_under_its_session_and_its_answer(server):
    """run_machine as the chat calls it: the session and request come as runtime params."""
    answer = await server.run_machine({"machine_id": "tiny", "wait": "finish", "_user_id": "ann",
                                       "_session_id": "chat-1", "_request_id": "req-1"})
    assert answer["status"] == "success", answer
    run_id = answer["run_id"]
    await server.run_machine({"machine_id": "tiny", "wait": "finish", "_user_id": "ann",
                              "_session_id": "chat-2", "_request_id": "req-10"})

    assert ids(server.service.list_runs(session="chat-1")) == [run_id]
    assert ids(server.service.list_runs(request="req-1")) == [run_id], "req-10 is no call under req-1"
    assert len(server.service.list_runs()) == 2, "fixture: the other session's run exists"


async def test_a_workflow_and_a_call_under_an_answer_are_listed_with_it(server):
    fake_backends(server, {"find": "a bug", "verify/0": "stands"})
    workflow = await server.service.start_workflow(REVIEW, user_id="ann", caller="lead", caller_session="chat-1",
                                                   caller_request="req-1", wake=False)
    await settled(server, workflow["run_id"])
    # a machine agent's run carries its request id as prefix: <request>_sg...; its calls <run>_NNN
    under = await server.service.start_run("tiny", user_id="ann", caller_session="chat-1",
                                           caller_request="req-1_sgabc123_001")
    await settled(server, under["run_id"])

    assert ids(server.service.list_runs(request="req-1")) == sorted([workflow["run_id"], under["run_id"]])
    assert ids(server.service.list_runs(session="chat-1")) == sorted([workflow["run_id"], under["run_id"]])


async def test_a_run_a_call_starts_and_a_fork_stay_under_the_session_that_started_theirs(server):
    parent = await server.service.start_run("parent", user_id="ann", caller_session="chat-1",
                                            caller_request="req-1", live={"service": server.service})
    await settled(server, parent["run_id"])
    [child] = [r for r in server.service.list_runs(machine_id="tiny")]
    assert child["caller_run"] == parent["run_id"], "fixture: the call's run is no child of the parent"

    # its row is what counts here: how far the fork gets without the parent's live service does not
    fork_id = await server.run_manager.fork(parent["run_id"], at_step=0, backend=FakeBackend({}), user_id="ann")
    await settled(server, fork_id)

    listed = ids(server.service.list_runs(session="chat-1"))
    assert parent["run_id"] in listed and child["id"] in listed, listed
    assert fork_id in listed, listed
    assert server.service.list_runs(session="agent-own") == [], "listed under the agent's session, not the chat's"
    # the parent's id is no <request>_...: its child's request <parent>_NNN is not under req-1, the child is
    assert child["id"] in ids(server.service.list_runs(request="req-1"))


async def test_the_open_in_menu_offers_the_panel_only_for_a_session_with_a_run_the_viewer_may_see(server,
                                                                                                    monkeypatch):
    await server.run_machine({"machine_id": "tiny", "wait": "finish", "_user_id": "ann", "_session_id": "chat-1"})

    assert server.has_session_data("chat-1", "ann") is True
    assert server.has_session_data("chat-2", "ann") is False
    monkeypatch.setattr(server, "_auth_enabled", lambda: True)
    monkeypatch.setattr(server, "_is_admin", lambda user: user == "root")
    assert server.has_session_data("chat-1", "bob") is False, "another user's run offered the panel"
    assert server.has_session_data("chat-1", "root") is True
    assert server.has_session_data("chat-1", "ann") is True


def test_the_runs_of_a_store_from_before_get_their_workflows_caller(tmp_path):
    """A runs.db without the column: a dynamic workflow kept its caller in its terms -- those runs are listed too."""
    path = tmp_path / "runs.db"
    fresh = RunStore(path)
    fresh.list_runs()  # the tables are made on the first use
    fresh.close()
    db = sqlite3.connect(path)
    db.execute("DROP INDEX runs_session")
    db.execute("ALTER TABLE runs DROP COLUMN caller_session")
    db.execute("INSERT INTO runs (id, machine_id, status, created_at, updated_at, definition, workflow)"
               " VALUES ('old', 'wf', 'succeeded', '2026-01-01', '2026-01-01', '{}', ?)",
               (json.dumps({"caller": "lead", "session": "chat-1", "request": "req-1"}),))
    db.commit()
    db.close()

    store = RunStore(path)
    try:
        assert ids(store.list_runs(session="chat-1")) == ["old"]
        assert ids(store.list_runs(request="req-1")) == ["old"]
    finally:
        store.close()


async def test_the_panels_run_list_takes_the_session_and_the_answer(server):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace

    from agent_system.plugins.web_adapter import PluginWebRegistry

    answer = await server.run_machine({"machine_id": "tiny", "wait": "finish", "_session_id": "chat-1",
                                       "_request_id": "req-1"})
    await server.run_machine({"machine_id": "tiny", "wait": "finish", "_session_id": "chat-2"})
    app = FastAPI()
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=False))
    registry = PluginWebRegistry()
    registry.register_web_plugin("stategraph", server)
    registry.apply_to_app(app)

    def listed(query):
        return ids(TestClient(app).get(f"/plugins/stategraph/api/runs?{query}").json())

    assert await asyncio.to_thread(listed, "session_id=chat-1") == [answer["run_id"]]
    assert await asyncio.to_thread(listed, "request_id=req-1") == [answer["run_id"]]
    assert len(await asyncio.to_thread(listed, "")) == 2


async def test_a_run_resumed_by_its_key_from_a_chat_is_listed_under_that_chat(server):
    definition = server.service.store.load("tiny").snapshot()
    server.run_store.create_run("kept", "tiny", definition, params={}, mocks={}, owner="gone:1:x",
                                lease_until=utc_at(-1), status="interrupted", run_key="ticket-7")
    server.run_store.update_run("kept", caller_run="old", caller_request="old_009")

    await server.run_machine({"machine_id": "tiny", "run_key": "ticket-7", "wait": "finish",
                              "_session_id": "chat-1", "_request_id": "req-1"})

    assert ids(server.service.list_runs(request="req-1")) == ["kept"]
    assert ids(server.service.list_runs(session="chat-1")) == ["kept"]


async def test_a_run_a_call_resumes_by_its_key_moves_under_the_session_of_the_calls_run(server):
    definition = server.service.store.load("tiny").snapshot()
    server.run_store.create_run("kept", "tiny", definition, params={}, mocks={}, owner="gone:1:x",
                                lease_until=utc_at(-1), status="interrupted", run_key="ticket-7")
    server.run_store.update_run("kept", caller_session="chat-A", caller_request="req-A")

    parent = await server.service.start_run("keyed", user_id="ann", caller_session="chat-B", caller_request="req-B",
                                            live={"service": server.service})
    await settled(server, parent["run_id"])

    kept = server.run_store.get_run("kept")
    assert kept["caller_run"] == parent["run_id"], f"fixture: the call did not resume it: {kept['caller_run']}"
    assert "kept" in ids(server.service.list_runs(session="chat-B")), "the call's child is missing from its session"
    assert server.service.list_runs(session="chat-A") == [], "listed under the chat it no longer belongs to"
