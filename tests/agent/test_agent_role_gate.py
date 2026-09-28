"""The run itself asks the agent's role gate (metadata.min_role) before anything of it exists.

The endpoints refuse a gated agent over HTTP, but most runs reach an agent
without an endpoint: a sub-agent, an agent called as a tool, a stategraph
activity, agent-cli woken for a session. Agent.run_events is the backstop for
all of them: the caller is the registered owner of the request id, else the
user the session names (agent-cli registers no request), else "anonymous" --
and a refused run leaves no metadata, no registered request and no LLM call
behind. A run nobody can be named for is judged as "anonymous": refused unless
anonymous access is enabled with a sufficient role.

The LLM is scripted and records every call; the user store is a tmp
UserDatabase (root: admin, bob: user), never data/users.db.
"""
from __future__ import annotations

import json

import pytest

from agent_system.auth import database
from agent_system.auth.models import UserCreate, UserRole
from agent_system.config.models import (
    AgentConfig, AgentMetadata, AgentSystemConfig, AnonymousAccessConfig, AuthConfig, ToolServerConfig,
)
from agent_system.core.request_context import register_request_user, release_request_user_tree
from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry
from test_agent_finish_reason_transport import _llm_system
from test_agent_output_cap_note import _scripted

NO_STORE_FILE = "/nonexistent/agent-gate-test/users.db"


@pytest.fixture
def store(tmp_path, monkeypatch):
    users = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", users)
    for name, role in (("root", UserRole.ADMIN), ("bob", UserRole.USER)):
        users.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse", role=role))
    return users


@pytest.fixture
def requests():
    """Register request ids to users; released after the test."""
    registered = []

    def register(request_id, user):
        register_request_user(request_id, user)
        registered.append(request_id)
        return request_id

    yield register
    for request_id in registered:
        release_request_user_tree(request_id)


def _auth(**kwargs) -> AuthConfig:
    kwargs.setdefault("database_path", NO_STORE_FILE)
    return AuthConfig(enabled=True, **kwargs)


def _agent(min_role="admin", auth=None, registry=None, name="gated_agent"):
    server_config = ToolServerConfig(type="agent", enabled=True,
                                     agent_config=AgentConfig(max_steps=2, llm_profile="normal"),
                                     metadata=AgentMetadata(min_role=min_role, visibility="tool"))
    system = AgentSystemConfig(llm_system=_llm_system(), auth=auth or _auth())
    agent = Agent(name, system, server_config, registry or ToolServerRegistry())
    agent.llm, seen = _scripted([("done", "stop")])
    return agent, seen


async def _events(agent, **kwargs):
    return [event async for event in agent.run_events("hi", **kwargs)]


async def test_a_plain_users_run_is_refused_before_anything_of_it_exists(store, requests):
    agent, seen = _agent()
    rid = requests("rq-bob", "bob")

    events = await _events(agent, request_id=rid, session_id="s-bob")

    assert [event["type"] for event in events] == ["error", "end"], events
    assert events[0]["request_id"] == rid
    assert seen == [], "the LLM was called for a refused run"
    assert agent._session_tracker.get_session_metadata("s-bob") is None, "a refused run wrote session metadata"
    assert rid not in agent._request_manager._active_requests, "a refused run stayed registered"


async def test_an_admin_runs(store, requests):
    agent, seen = _agent()
    events = await _events(agent, request_id=requests("rq-root", "root"), session_id="s-root")

    assert "final" in [event["type"] for event in events], events
    assert seen, "the admin's run never reached the LLM"


@pytest.mark.parametrize("local", [True, False])
async def test_the_local_operator_runs_the_session_it_opened_in_a_local_process_only(store, monkeypatch, local):
    """agent-cli and agent-run open their session as cli_user and register no request, and trust that name
    (auth/agent_access.local_operator_trusted); in the API process it is a name no account holds."""
    from agent_system.auth import agent_access

    monkeypatch.setattr(agent_access, "_local_operator_trusted", local)
    agent, seen = _agent()
    agent._session_tracker.set_session_metadata("s-cli", {"user_id": "cli_user", "agent_name": agent.name})

    events = await _events(agent, request_id="rq-cli", session_id="s-cli")

    assert ("final" in [event["type"] for event in events]) is local, events
    assert bool(seen) is local


@pytest.mark.parametrize("session_user, request_owner, runs", [
    (None, "root", True),          # a session nobody holds yet: the registered request owner is judged
    (None, "bob", False),
    ("bob", None, False),          # nothing registered (agent-cli, a wake): the session's user
    ("root", None, True),
    ("bob", "root", False),        # another user's session: refused whoever asks (Agent._foreign_session)
    ("anonymous", "root", False),  # "anonymous" is a stored user like any other
])
async def test_the_run_is_judged_as_its_request_owner_else_its_session_user(store, requests, session_user,
                                                                            request_owner, runs):
    """The request owner is written by framework code only; a session id can come from where the caller chose it.
    Where nothing is registered -- agent-cli woken for a session -- the session's user answers. Where both
    are there they must be the same user."""
    agent, seen = _agent()
    if session_user:
        agent._session_tracker.set_session_metadata("s-1", {"user_id": session_user, "agent_name": agent.name})
    rid = requests("rq-1", request_owner) if request_owner else "rq-unregistered"

    events = await _events(agent, request_id=rid, session_id="s-1")

    assert ("final" in [event["type"] for event in events]) is runs, events
    assert bool(seen) is runs


@pytest.mark.parametrize("anonymous, runs", [
    (AnonymousAccessConfig(enabled=False), False),
    (AnonymousAccessConfig(enabled=True, role="admin"), True),
])
async def test_a_run_nobody_can_be_named_for_is_anonymous(store, anonymous, runs):
    agent, seen = _agent(auth=_auth(anonymous_access=anonymous))

    events = await _events(agent, request_id="rq-nobody", session_id="s-nobody")

    assert ("final" in [event["type"] for event in events]) is runs, events
    assert bool(seen) is runs


async def test_an_ungated_agent_asks_nobody(monkeypatch):
    class Recording:
        asked = []

        def get_user_by_username(self, username):
            self.asked.append(username)

    monkeypatch.setattr(database, "_db", Recording())
    agent, seen = _agent(min_role=None)

    events = await _events(agent, request_id="rq-free", session_id="s-free")

    assert "final" in [event["type"] for event in events], events
    assert Recording.asked == []


async def _call_as_a_tool(agent, user, tool=None, **model_arguments):
    """The production dispatch of an agent-as-tool: ToolExecutionManager injects the caller's
    user and registers the call's request id under it, then calls the agent's tool."""
    registry = agent.registry
    registry.register(agent.name, agent)
    tool = tool or agent.name
    items = [item async for item in ToolExecutionManager(registry).execute_tools_streaming(
        tool_calls=[{"id": "c1", "function": {"name": tool, "arguments": json.dumps({"task": "do it",
                                                                                   **model_arguments})}}],
        tool_name_mapping={tool: agent.name}, available_tools=[agent.name], step=1,
        request_id=f"parent-{user}", session_id=f"parent-s-{user}", user_id=user)]
    release_request_user_tree(f"parent-{user}")
    [complete] = [item for item in items if item["type"] == "complete"]
    [message] = complete["messages"]
    return json.loads(message.content)


async def test_an_agent_called_as_a_tool_is_refused_with_a_tool_error(store):
    agent, seen = _agent()

    answer = await _call_as_a_tool(agent, "bob")

    assert answer["status"] == "error", answer
    assert "gated_agent" in answer["error"], answer
    assert seen == [], "the LLM was called for a refused tool call"


async def test_an_admin_calls_the_agent_as_a_tool(store):
    agent, seen = _agent()

    answer = await _call_as_a_tool(agent, "root")

    assert answer["status"] == "success", answer
    assert seen


async def test_a_reload_moves_the_gate(store, requests):
    agent, seen = _agent(min_role=None)
    gated = ToolServerConfig(type="agent", enabled=True, agent_config=agent.agent_config,
                             metadata=AgentMetadata(min_role="admin"))

    changes = agent.reload_config(gated)
    refused = await _events(agent, request_id=requests("rq-a", "bob"), session_id="s-a")
    agent.reload_config(ToolServerConfig(type="agent", enabled=True, agent_config=agent.agent_config))
    ran = await _events(agent, request_id=requests("rq-b", "bob"), session_id="s-b")

    assert changes["min_role"] == {"old": None, "new": "admin"}, changes
    assert [event["type"] for event in refused] == ["error", "end"], refused
    assert "final" in [event["type"] for event in ran], ran


def _basic_agent(name="gated_basic"):
    """The agent type nearly every shipped agent is: its tool is <name>_execute_task, not Agent.call."""
    from plugins.basic_agent.server import BasicAgent

    server_config = ToolServerConfig(type="basic_agent", enabled=True,
                                     agent_config=AgentConfig(max_steps=2, llm_profile="normal"),
                                     metadata=AgentMetadata(min_role="admin", visibility="tool"))
    agent = BasicAgent(name, AgentSystemConfig(llm_system=_llm_system(), auth=_auth()), server_config,
                       ToolServerRegistry())
    agent.llm, seen = _scripted([("done", "stop")])
    return agent, seen


@pytest.mark.parametrize("kind", ["agent", "basic_agent"])
async def test_a_model_cannot_borrow_an_admins_session_to_pass_the_gate(store, kind):
    """The model names a session of this agent that belongs to root (a SAM sub-session root ran).
    The call stays bob's: refused, and root's session is neither run nor touched."""
    agent, seen = _agent() if kind == "agent" else _basic_agent()
    tool = agent.name if kind == "agent" else f"{agent.name}_execute_task"
    agent._session_tracker.set_session_metadata("sub-root", {"user_id": "root", "agent_name": agent.name})

    answer = await _call_as_a_tool(agent, "bob", tool=tool, session_id="sub-root")

    assert answer["status"] == "error", answer
    assert seen == [], "the gated agent ran for bob"
    assert agent._session_tracker.get_session_messages("sub-root") in (None, []), "root's session was written"


async def test_a_basic_agent_called_as_a_tool_is_refused_and_runs_for_an_admin(store):
    agent, seen = _basic_agent()

    refused = await _call_as_a_tool(agent, "bob", tool=f"{agent.name}_execute_task")
    assert refused["status"] == "error" and seen == [], refused
    ran = await _call_as_a_tool(agent, "root", tool=f"{agent.name}_execute_task")
    assert ran["status"] == "success" and seen, ran


@pytest.mark.parametrize("kind", ["agent", "basic_agent"])
async def test_a_model_named_session_is_not_the_one_the_call_runs_on(store, kind):
    """Gate or no gate: a plain ``session_id`` in the model's arguments is not the session the call runs on.
    Tool execution strips only ``_*`` and request ids; honoured, it let a model continue any session this
    agent holds -- here root's, with root's history in bob's context."""
    from agent_system.llm.models import ChatMessage

    agent, seen = _agent(min_role=None) if kind == "agent" else _basic_agent()
    agent.min_role = None
    tool = agent.name if kind == "agent" else f"{agent.name}_execute_task"
    agent._session_tracker.set_session_metadata("sub-root", {"user_id": "root", "agent_name": agent.name})
    agent._session_tracker.set_session_messages("sub-root", [ChatMessage(role="user", content="root's secret plan")])

    answer = await _call_as_a_tool(agent, "bob", tool=tool, session_id="sub-root")

    assert answer["status"] == "success", answer
    assert seen, "fixture: the ungated call did not run"
    assert not any("root's secret plan" in str(getattr(message, "content", message))
                   for call in seen for message in call), "root's session history reached bob's run"
    assert [m.content for m in agent._session_tracker.get_session_messages("sub-root")] == ["root's secret plan"]


async def test_every_tool_of_a_gated_agent_is_gated_not_only_its_runs(store):
    """``<agent>_list_available_tools`` goes straight to its method, past Agent.call and run_events;
    it answered what GET /agents/{name}/tools refuses."""
    agent, seen = _basic_agent()
    tool = f"{agent.name}_list_available_tools"

    refused = await _call_as_a_tool(agent, "bob", tool=tool)
    listed = await _call_as_a_tool(agent, "root", tool=tool)

    assert isinstance(refused, dict) and refused.get("status") == "error", f"bob got the listing: {refused}"
    assert agent.name in refused["error"], refused
    assert isinstance(listed, list), listed  # the listing itself


async def test_a_tool_call_nobody_can_be_named_for_is_refused(store):
    """Straight to the dispatcher without a request id or an injected user: unidentified, so refused."""
    agent, seen = _basic_agent()

    answer = await agent.call(f"{agent.name}_list_available_tools", {})

    assert answer.get("status") == "error", answer


@pytest.mark.parametrize("kind, owner, injected, runs", [
    ("agent", "bob", "root", False),
    ("agent", "root", "bob", True),
    # bob-root on a gated basic_agent measures nothing here: the mixin's gate refuses bob before
    # BasicAgent.execute_task is reached. The ungated one in test_agent_run_identity.py measures it.
    ("basic_agent", "root", "bob", True),
])
async def test_the_registered_owner_wins_over_an_injected_user(store, requests, kind, owner, injected, runs):
    """A call whose request id names its owner runs as that owner, whatever ``_user_id`` comes with it: the
    own id Agent.call registers for an injected user is only for a call whose request names nobody."""
    agent, seen = _agent() if kind == "agent" else _basic_agent()
    tool = agent.name if kind == "agent" else f"{agent.name}_execute_task"
    rid = requests(f"rq-{owner}", owner)

    answer = await agent.call(tool, {"task": "do it", "_request_id": rid, "_user_id": injected})

    assert (answer["status"] == "success") is runs, answer
    assert bool(seen) is runs, "the gate judged the injected user, not the request's owner"
