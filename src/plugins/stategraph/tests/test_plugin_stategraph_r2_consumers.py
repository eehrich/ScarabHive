"""Second pre-commit review, consumers lens: what the diff's contracts do to their callers (tracker, scarab, writer),
through the real StateGraphServer and RunStore under tmp_path. Each test names the finding it holds."""

from __future__ import annotations

import asyncio

import pytest

from agent_system.config.models import AgentSystemConfig
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, tool_config, until, utc_at

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

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

# a hand-off, as tracker's intake hands a ticket to its workflow: started from the call, not a step of it
PARENT_PY = """\
import asyncio
import os


async def run(sg):
    await sg.live["service"].start_run("child", run_key=sg.live.get("key"), detached=True)
    while not os.path.exists(os.environ["SG_R2C_GATE"]):
        await asyncio.sleep(0.01)
"""

CHILD = """\
stategraph: 1
id: child
python: child.py
initial: work
states:
  work:
    do: {call: work, args: {}}
    transitions: [{target: done}]
  done: {type: final}
"""

CHILD_PY = """\
import asyncio
import os


async def work():
    while not os.path.exists(os.environ["SG_R2C_GATE"]):
        await asyncio.sleep(0.01)
    return 1
"""


@pytest.fixture
async def server(tmp_path, monkeypatch):
    gate = tmp_path / "gate"
    monkeypatch.setenv("SG_R2C_GATE", str(gate))
    made = StateGraphServer("stategraph", AgentSystemConfig(), tool_config(tmp_path))
    for name, text in (("parent.yaml", PARENT), ("parent.py", PARENT_PY), ("child.yaml", CHILD),
                       ("child.py", CHILD_PY)):
        (tmp_path / "machines" / name).write_text(text, encoding="utf-8")
    yield made
    gate.touch()
    await made.stop_plugin()


def _runs_of(server, machine: str) -> list[dict]:
    return [r for r in server.run_store.list_runs(machine, limit=10) if r["machine_id"] == machine]


async def test_cons1_a_detached_run_is_nobodys_child_and_outlives_the_callers_terminate(server):
    parent = (await server.service.start_run("parent", live={"service": server.service}))["run_id"]
    await until(lambda: _runs_of(server, "child"), what="the hand-off started")
    [child] = _runs_of(server, "child")
    await until(lambda: child["id"] in server.run_manager.live, what="the child runs here")

    assert server.run_store.children(parent) == []
    assert server.run_store.get_run(child["id"])["caller_run"] is None
    assert "started_by" not in server.service.get_run(child["id"])

    await server.service.control_run(parent, "terminate")
    await until(lambda: server.run_store.get_run(parent)["status"] == "cancelled", what="the caller ended")
    await asyncio.sleep(0.3)  # an end_children would have terminated it by now
    assert server.run_store.get_run(child["id"])["status"] == "running"


async def test_cons1_a_detached_resume_by_key_drops_the_old_callers_link(server):
    definition = server.service.store.load("child").snapshot()
    server.run_store.create_run("kept", "child", definition, params={}, mocks={}, owner="gone:1:x",
                                lease_until=utc_at(-1), status="interrupted", run_key="ticket-7")
    server.run_store.update_run("kept", caller_run="old", caller_request="old_009")

    await server.service.start_run("parent", live={"service": server.service, "key": "ticket-7"})
    await until(lambda: "kept" in server.run_manager.live, what="the caller resumed it by its key")
    assert server.run_store.get_run("kept")["caller_run"] is None
    assert server.run_store.children("old") == []
