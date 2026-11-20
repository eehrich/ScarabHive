"""Who may run machines with auth on: the core's role gate decides, so agent-cli's local operator (cli_user, whom no
account holds) may -- as it may run an agent with ``min_role: admin`` -- and in the API process the name is nobody."""
from __future__ import annotations

import pytest

from agent_system.auth import database
from agent_system.auth.agent_access import local_operator_trusted
from agent_system.config.models import AgentSystemConfig
from plugins.stategraph.server import StateGraphServer
from plugins.stategraph.tests.stategraph_testkit import FASTAPI_PY314, tool_config

pytestmark = pytest.mark.filterwarnings(FASTAPI_PY314)

TINY = "stategraph: 1\nid: tiny\ninitial: done\nstates:\n  done: {type: final}\n"


@pytest.fixture
async def server(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "_db", None)  # the store is the configured file: none, so no account holds a name
    system = AgentSystemConfig(auth={"enabled": True, "database_path": str(tmp_path / "users.db")})
    made = StateGraphServer("stategraph", system, tool_config(tmp_path))
    (tmp_path / "machines" / "tiny.yaml").write_text(TINY, encoding="utf-8")
    yield made
    await made.stop_plugin()


async def test_agent_clis_local_operator_runs_a_machine_and_in_the_api_the_name_is_nobody(server):
    assert server._auth_enabled(), "fixture: auth is off, every caller may"

    refused = await server.run_machine({"machine_id": "tiny", "wait": "finish", "_user_id": "cli_user"})
    assert refused["status"] == "error" and "only admins" in str(refused), refused

    with local_operator_trusted():  # agent-cli's and agent-run's entry points
        answer = await server.run_machine({"machine_id": "tiny", "wait": "finish", "_user_id": "cli_user"})
        stranger = await server.run_machine({"machine_id": "tiny", "wait": "finish", "_user_id": "mallory"})
    assert answer["status"] == "success", answer
    assert stranger["status"] == "error", "a name no account holds passed as the operator"
