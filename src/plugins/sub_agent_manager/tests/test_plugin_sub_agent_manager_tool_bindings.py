"""A sub-agent's tool bindings: given on create (`_tool_bindings`, framework-set), kept with the instance and handed
to every run of it -- a continue that names none runs with them, one that names others is refused.

Real are the server's create/continue, the manager and the session store on disk; the agent is a stand-in that
records what its runs were given.
"""
from unittest.mock import AsyncMock, Mock

import pytest

from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
from agent_system.servers.agent.components.session_tracking import SessionTracker
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from agent_system.tools.base import ToolServerRegistry
from plugins.sub_agent_manager.manager import SubAgentManager
from plugins.sub_agent_manager.server import SubAgentManagerServer

BINDINGS = {"file_ops/file_ops_read_file": {"filePath": "/work/notes/a.md"}}
USER, PARENT = "testuser", "coord_1"


@pytest.fixture
async def setup(tmp_path):
    sessions = SessionManager(storage_path=str(tmp_path))
    service = SessionService(session_manager=sessions)
    service.save_session = AsyncMock()         # the agent is a stand-in: nothing of a real run to save
    await sessions.create_session(user_id=USER, session_id=PARENT, title="c", agent_name="coord",
                                  llm_profile="normal")
    runs = []

    async def run_events(*args, **kwargs):
        runs.append(kwargs)
        yield {"type": "start"}
        yield {"type": "final", "summary": "done"}

    agent = Mock()
    agent.name = "worker"
    agent.agent_config = AgentConfig(llm_profile="normal")
    agent._session_service = None
    agent._session_tracker = Mock(wraps=SessionTracker())
    agent._session_tracker.set_session_metadata = Mock()
    agent.run_events = run_events
    registry = ToolServerRegistry()
    registry.register("worker", agent)
    manager = SubAgentManager(service, registry)
    server = SubAgentManagerServer("sam", Mock(spec=AgentSystemConfig),
                                   ToolServerConfig(type="sub_agent_manager", enabled=True, allowed_agents=["worker"]))
    server._extract_registry = Mock(return_value=registry)
    server._extract_session_service = Mock(return_value=service)
    server._get_manager = Mock(return_value=manager)
    return server, sessions, runs


def call(server, **params):
    return server.manage_sub_agent({**params, "_session_id": PARENT, "_user_id": USER, "_request_id": "r1"})


async def test_the_bindings_of_a_create_hold_every_run_of_the_instance(setup):
    server, sessions, runs = setup
    created = await call(server, operation="create", agent_type="worker", task="write", _tool_bindings=BINDINGS)
    assert created["status"] == "completed", created
    stored = await sessions.load_session(USER, created["instance_id"])
    assert stored["metadata"]["tool_bindings"] == BINDINGS
    # A continue that names none runs with the instance's; one that names the same, too.
    for again in ({}, {"_tool_bindings": BINDINGS}):
        result = await call(server, operation="continue", instance_id=created["instance_id"], message="more", **again)
        assert result["status"] == "completed", result
    assert [run["tool_bindings"] for run in runs] == [BINDINGS] * 3


async def test_a_continue_naming_other_bindings_is_refused_before_it_runs(setup):
    server, _, runs = setup
    created = await call(server, operation="create", agent_type="worker", task="write", _tool_bindings=BINDINGS)
    other = {"file_ops/file_ops_read_file": {"filePath": "/work/notes/b.md"}}
    refused = await call(server, operation="continue", instance_id=created["instance_id"], message="x",
                         _tool_bindings=other)
    assert refused["error_type"] == "tool_bindings_mismatch" and len(runs) == 1
    # An instance created without bindings does not take any later either.
    plain = await call(server, operation="create", agent_type="worker", task="read")
    refused = await call(server, operation="continue", instance_id=plain["instance_id"], message="x",
                         _tool_bindings=BINDINGS)
    assert refused["error_type"] == "tool_bindings_mismatch" and len(runs) == 2
    # An unbound run is called as before -- without the parameter, which an agent of its own kind may lack.
    assert "tool_bindings" not in runs[-1]


async def test_malformed_bindings_create_no_instance(setup):
    server, sessions, runs = setup
    result = await call(server, operation="create", agent_type="worker", task="write",
                        _tool_bindings={"file_ops/*": {"filePath": ["/a", "/b"]}})
    assert result["error_type"] == "invalid_tool_bindings" and runs == []
    parent = await sessions.load_session(USER, PARENT)
    assert not (parent.get("metadata") or {}).get("sub_agents")


async def test_a_background_create_runs_with_its_bindings(setup):
    server, _, runs = setup
    started = await call(server, operation="create", agent_type="worker", task="write", blocking=False,
                         _tool_bindings=BINDINGS)
    await server._async_jobs[started["instance_id"]]["task_handle"]
    assert runs and runs[-1]["tool_bindings"] == BINDINGS
