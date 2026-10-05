"""A run acts for the user it was started for -- not for whoever an earlier run under the same session id was.

An agent called as a tool runs under its CALLER's session id, and its tracker
keeps the metadata an earlier run under that id wrote: run_events writes it only
where none is. Tool execution read the user from there, so a second user's run
that reached the same id continued the first user's conversation AS the first
user -- that user's identity on its tools, its tool request ids and its
sub-agents (whose role gate then judged the wrong user).

Now: a run whose registered request owner (the framework registers it: the API,
tool execution, the SAM, the stategraph backend) is not the session's stored
user is refused -- an admin's too, and a session held for "anonymous" too, as
POST /run refuses another user's session whether auth is on or off. The tools
run for the owner, the session's stored user only where nothing is registered
(agent-cli), and they keep the owner when the session's metadata is rewritten
during the run.

A real Agent on a scripted LLM, a real tool server (datetime) whose calls are
recorded; the user store is a tmp UserDatabase (root: admin, alice and bob:
user), never data/users.db.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from agent_system.auth import database
from agent_system.auth.models import UserCreate, UserRole
from agent_system.config.models import AgentConfig, AgentSystemConfig, AuthConfig, ToolConfig, ToolServerConfig
from agent_system.core.request_context import register_request_user, release_request_user_tree
from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from agent_system.servers.agent.server import Agent, tool_session_id
from agent_system.tools.base import ToolServerRegistry
from test_agent_finish_reason_transport import _llm_system

AUTH = AuthConfig(enabled=True, database_path="/nonexistent/agent-gate-test/users.db")


@pytest.fixture
def store(tmp_path, monkeypatch):
    users = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", users)
    for name, role in (("root", UserRole.ADMIN), ("alice", UserRole.USER), ("bob", UserRole.USER)):
        users.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse", role=role))
    return users


@pytest.fixture
def requests():
    registered = []

    def register(request_id, user):
        register_request_user(request_id, user)
        registered.append(request_id)
        return request_id

    yield register
    for request_id in registered:
        release_request_user_tree(request_id)


def _agent_with_a_tool(tool_steps=1, on_tool=None):
    """Agent "b" whose model calls datetime *tool_steps* times and then answers; returns (agent, llm calls, tool
    params). *on_tool* runs after each recorded tool call."""
    from plugins.datetime.server import DateTimeServer

    registry = ToolServerRegistry()
    tool = DateTimeServer("datetime", AgentSystemConfig(), ToolServerConfig(type="datetime", enabled=True))
    tool_params = []

    async def record(name, params):
        tool_params.append({key: params.get(key) for key in ("_user_id", "_request_id")})
        if on_tool is not None:
            on_tool()
        return {"status": "ok"}

    tool.call_with_status = record
    registry.register("datetime", tool)
    agent = Agent("b", AgentSystemConfig(llm_system=_llm_system(), auth=AUTH),
                  ToolServerConfig(type="agent", enabled=True,
                                   agent_config=AgentConfig(max_steps=tool_steps + 2, llm_profile="normal",
                                                            tools=ToolConfig(allowed=["datetime/*"]))),
                  registry)
    calls = []

    async def stream(messages, tools, cancellation_token=None, status_scope=None):
        calls.append([getattr(message, "content", None) for message in messages])
        if (len(calls) - 1) % (tool_steps + 1) < tool_steps:
            yield {"type": "final", "finish_reason": "tool_calls", "usage": {},
                   "assistant": {"role": "assistant", "content": "", "tool_calls": [
                       {"id": f"c{len(calls)}", "type": "function",
                        "function": {"name": "datetime_operations", "arguments": json.dumps({"operation": "now"})}}]}}
        else:
            yield {"type": "final", "finish_reason": "stop", "usage": {},
                   "assistant": {"role": "assistant", "content": "done"}}

    llm = AsyncMock()
    llm.supports_streaming = lambda: True
    llm.chat_tools_streaming = stream
    agent.llm = llm
    return agent, calls, tool_params


def _held_for(agent, session_id, user):
    agent._session_tracker.set_session_metadata(session_id, {"user_id": user, "agent_name": agent.name})


async def _run(agent, **kwargs):
    return [event async for event in agent.run_events("what time is it", **kwargs)]


async def test_where_nothing_is_registered_the_sessions_user_runs_the_tools(store):
    """agent-cli: its runs register no request owner, the session it opened names its user."""
    agent, calls, tool_params = _agent_with_a_tool()
    _held_for(agent, "S", "alice")

    await _run(agent, request_id="rq-unregistered", session_id="S")

    assert [params["_user_id"] for params in tool_params] == ["alice"], tool_params


@pytest.mark.parametrize("owner", ["bob", "root"])
async def test_a_run_in_a_session_held_for_another_user_is_refused_an_admins_too(store, requests, owner):
    """Gone on, the run is held, checkpointed and saved under the stored user -- alice's session file.
    POST /run refuses an admin another user's session; the run path does the same."""
    agent, calls, tool_params = _agent_with_a_tool()
    _held_for(agent, "S", "alice")

    events = await _run(agent, request_id=requests(f"rq-{owner}", owner), session_id="S")

    assert [event["type"] for event in events] == ["error", "end"], events
    assert calls == [] and tool_params == [], f"{owner}'s run went on in alice's session"
    assert agent._session_tracker.get_session_metadata("S")["user_id"] == "alice"


async def test_a_registered_owners_run_in_a_session_held_for_anonymous_is_refused(store, requests, tmp_path):
    """"anonymous" is what a run with no registered owner wrote -- and the session file is already saved
    under data/sessions/anonymous/, where every save of it goes on landing: the owner's run would put its
    turn into that file. Nothing of the run happens: no LLM call, no tool, no save."""
    from agent_system.services.session_manager import SessionManager
    from agent_system.services.session_service import SessionService

    agent, calls, tool_params = _agent_with_a_tool()
    agent._session_service = SessionService(SessionManager(storage_path=str(tmp_path / "sessions")))
    _held_for(agent, "S", "anonymous")

    events = await _run(agent, request_id=requests("rq-bob", "bob"), session_id="S")

    assert [event["type"] for event in events] == ["error", "end"], events
    assert calls == [] and tool_params == [], "bob's run went on in the anonymous session"
    assert agent._session_tracker.get_session_metadata("S")["user_id"] == "anonymous"
    assert not list((tmp_path / "sessions").glob("*/S.json")), "something of the refused run was saved"


async def test_a_second_user_cannot_continue_an_agent_tools_session_of_the_first(store):
    """The reported path: alice's run called b as a tool in her session S; bob's run reaches b under the same S."""
    agent, calls, tool_params = _agent_with_a_tool()
    registry = ToolServerRegistry()
    registry.register(agent.name, agent)

    async def call_b_as(user):
        items = [item async for item in ToolExecutionManager(registry).execute_tools_streaming(
            tool_calls=[{"id": "t1", "function": {"name": "b", "arguments": json.dumps({"task": "time?"})}}],
            tool_name_mapping={"b": "b"}, available_tools=["b"], step=1,
            request_id=f"parent-{user}", session_id="S", user_id=user)]
        release_request_user_tree(f"parent-{user}")
        [complete] = [item for item in items if item["type"] == "complete"]
        return json.loads(complete["messages"][0].content)

    first = await call_b_as("alice")
    llm_calls_after_alice = len(calls)
    second = await call_b_as("bob")

    assert first["status"] == "success" and llm_calls_after_alice, first
    assert second["status"] == "error", f"bob's call ran in alice's session: {second}"
    assert len(calls) == llm_calls_after_alice, "b ran again, in alice's session, for bob"
    assert [params["_user_id"] for params in tool_params] == ["alice"], tool_params


async def test_the_tools_keep_the_owner_when_the_sessions_metadata_is_rewritten_during_the_run(store, requests):
    """The session's metadata is state of the session id, shared by every run of it -- here rewritten while
    this run goes on. Its tools stay the run's owner's: the registered owner is this run's."""
    holder = {}

    def rewritten_by_another_request():
        holder["agent"]._session_tracker.set_session_metadata(
            "S", {"user_id": "alice", "agent_name": holder["agent"].name})

    agent, calls, tool_params = _agent_with_a_tool(tool_steps=2, on_tool=rewritten_by_another_request)
    holder["agent"] = agent
    _held_for(agent, "S", "bob")

    events = await _run(agent, request_id=requests("rq-bob", "bob"), session_id="S")

    assert "final" in [event["type"] for event in events], events
    assert len(tool_params) == 2, f"fixture: the second tool call after the rewrite did not happen: {tool_params}"
    assert agent._session_tracker.get_session_metadata("S")["user_id"] == "alice", "fixture: no rewrite"
    assert [params["_user_id"] for params in tool_params] == ["bob", "bob"], tool_params


async def test_a_call_without_a_request_id_is_its_injected_users_and_the_next_call_goes_on(store):
    """A plugin command dispatches with a _user_id but without a request id. The run stored "anonymous"
    as the session's user and ran its tools as anonymous -- and the same user's next call in the session was
    refused as another user's. Agent.call registers an id of its own for the injected _user_id."""
    from agent_system.core.request_context import request_user_map

    agent, calls, tool_params = _agent_with_a_tool()
    registry = ToolServerRegistry()
    registry.register(agent.name, agent)

    before = set(request_user_map)
    first = await agent.call("b", {"task": "time?", "_session_id": "S", "_user_id": "alice"})
    left_behind = set(request_user_map) - before
    items = [item async for item in ToolExecutionManager(registry).execute_tools_streaming(
        tool_calls=[{"id": "t1", "function": {"name": "b", "arguments": json.dumps({"task": "time?"})}}],
        tool_name_mapping={"b": "b"}, available_tools=["b"], step=1,
        request_id="parent-alice", session_id="S", user_id="alice")]
    release_request_user_tree("parent-alice")
    [complete] = [item for item in items if item["type"] == "complete"]
    second = json.loads(complete["messages"][0].content)

    assert (first["status"], second["status"]) == ("success", "success"), (first, second)
    # called as a tool, it runs on a session of its own below the caller's (tool_session_id)
    assert agent._session_tracker.get_session_metadata(tool_session_id("S", "b"))["user_id"] == "alice"
    assert [params["_user_id"] for params in tool_params] == ["alice", "alice"], tool_params
    assert left_behind == set(), f"the call's own request ids outlived it: {left_behind}"


async def test_a_basic_agents_task_without_a_request_id_is_its_injected_users(store):
    """The same for <agent>_execute_task, the tool nearly every shipped agent is called by."""
    from agent_system.core.request_context import request_user_map
    from plugins.basic_agent.server import BasicAgent

    registry = ToolServerRegistry()
    agent = BasicAgent("bb", AgentSystemConfig(llm_system=_llm_system(), auth=AUTH),
                       ToolServerConfig(type="basic_agent", enabled=True,
                                        agent_config=AgentConfig(max_steps=2, llm_profile="normal")), registry)
    donor, calls, tool_params = _agent_with_a_tool()
    agent.llm = donor.llm

    before = set(request_user_map)
    answer = await agent.call("bb_execute_task", {"task": "time?", "_session_id": "S", "_user_id": "alice"})
    left_behind = set(request_user_map) - before

    assert answer["status"] == "success", answer
    assert agent._session_tracker.get_session_metadata(tool_session_id("S", "bb"))["user_id"] == "alice"
    assert left_behind == set(), f"the task's own request ids outlived it: {left_behind}"


async def test_an_ungated_basic_agent_runs_for_the_registered_owner_not_an_injected_user(store, requests):
    """No gate decides here, so only the run itself shows whose it is: the request's owner (bob) is the
    session's stored user and the tools' user, whatever ``_user_id`` (root) comes with the call."""
    from plugins.basic_agent.server import BasicAgent

    donor, calls, tool_params = _agent_with_a_tool()
    agent = BasicAgent("bb", AgentSystemConfig(llm_system=_llm_system(), auth=AUTH),
                       ToolServerConfig(type="basic_agent", enabled=True,
                                        agent_config=AgentConfig(max_steps=3, llm_profile="normal",
                                                                 tools=ToolConfig(allowed=["datetime/*"]))),
                       donor.registry)
    agent.llm = donor.llm
    rid = requests("rq-bob", "bob")

    answer = await agent.call("bb_execute_task", {"task": "time?", "_request_id": rid, "_session_id": "S",
                                                  "_user_id": "root"})

    assert answer["status"] == "success", answer
    assert agent._session_tracker.get_session_metadata(tool_session_id("S", "bb"))["user_id"] == "bob"
    assert [params["_user_id"] for params in tool_params] == ["bob"], tool_params


async def test_a_preloaded_tool_runs_for_the_runs_user_and_the_models_next_call_goes_on(store):
    """tool_preload dispatches beside the model, with the run's request id. agent-cli registers none, so the
    preload asked the request alone, called the agent with no user, and that run stored "anonymous" as its
    session's user -- the model's own call of the same agent in the same run was then refused as another
    user's. The preload now asks the agent the way its tool calls do (Agent.tool_user): the request's owner,
    else the session's stored user."""
    from pathlib import Path

    import plugins.tool_preload.hooks as preload_hooks
    from agent_system.hooks import HookContext, HookType

    target, calls, tool_params = _agent_with_a_tool()
    registry = target.registry
    registry.register(target.name, target)
    caller = Agent("caller", AgentSystemConfig(llm_system=_llm_system(), auth=AUTH),
                   ToolServerConfig(type="agent", enabled=True,
                                    agent_config=AgentConfig(max_steps=2, llm_profile="normal",
                                                             tools=ToolConfig(allowed=["b*"]))),
                   registry)
    _held_for(caller, "S", "cli_user")  # agent-cli opened the session for its user
    rid = "rq-cli-run"  # and registers no request
    preload = preload_hooks.ToolPreloadPlugin(Path(preload_hooks.__file__).parent)
    context = HookContext(hook_type=HookType.PRE_LLM_CALL, request_id=rid, session_id="S", agent=caller,
                          agent_name=caller.name, messages=[], hook_config={"rules": []})
    try:
        pairs = await preload._execute_plan([(0, "b", {"task": "time?"})], context)
        items = [item async for item in ToolExecutionManager(registry).execute_tools_streaming(
            tool_calls=[{"id": "t1", "function": {"name": "b", "arguments": json.dumps({"task": "time?"})}}],
            tool_name_mapping={"b": "b"}, available_tools=["b"], step=1,
            request_id=rid, session_id="S", user_id=caller.tool_user(rid, "S"))]
    finally:
        release_request_user_tree(rid)
    preloaded = json.loads(pairs[-1].content)
    [complete] = [item for item in items if item["type"] == "complete"]
    second = json.loads(complete["messages"][0].content)

    assert (preloaded["status"], second["status"]) == ("success", "success"), (preloaded, second)
    assert target._session_tracker.get_session_metadata(tool_session_id("S", "b"))["user_id"] == "cli_user"
    assert [params["_user_id"] for params in tool_params] == ["cli_user", "cli_user"], tool_params
