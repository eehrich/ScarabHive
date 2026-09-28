"""The SAM refuses a sub-agent behind a role gate its caller's user does not pass (metadata.min_role).

Refused as the tool's own error, before a sub-session exists -- the backstop in
Agent.run_events would refuse the run as well, but only after the SAM had made a
sub-session for a run that can never happen, and the model would read a
"completed" instance whose answer is an error.

A real SubAgentManagerServer and a real gated Agent on a scripted LLM that
records its calls; sessions and the user store (root: admin, bob: user) live in
tmp_path, never in data/.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_system.auth import database
from agent_system.auth.models import UserCreate, UserRole
from agent_system.config.models import (
    AgentConfig, AgentMetadata, AgentSystemConfig, AuthConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
    ToolServerConfig,
)
from agent_system.servers.agent.server import Agent
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from agent_system.tools.base import ToolServerRegistry
from plugins.sub_agent_manager.server import SubAgentManagerServer

AUTH = AuthConfig(enabled=True, database_path="/nonexistent/agent-gate-test/users.db")


def _scripted_llm():
    seen = []

    async def stream(messages, tools, cancellation_token=None, status_scope=None):
        seen.append(list(messages))
        yield {"type": "final", "assistant": {"role": "assistant", "content": "done"},
               "usage": {"completion_tokens": 5}, "finish_reason": "stop"}

    llm = AsyncMock()
    llm.supports_streaming = lambda: True
    llm.chat_tools_streaming = stream
    return llm, seen


@pytest.fixture
def world(tmp_path, monkeypatch):
    users = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", users)
    for name, role in (("root", UserRole.ADMIN), ("bob", UserRole.USER)):
        users.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse", role=role))

    service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path / "sessions")))
    registry = ToolServerRegistry()
    llm_system = LLMSystemConfig(models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
                                 profiles={"normal": LLMProfile(model_ref="m")}, default_profile="normal")
    worker = Agent("gated_worker", AgentSystemConfig(llm_system=llm_system, auth=AUTH),
                   ToolServerConfig(type="agent", enabled=True, agent_config=AgentConfig(max_steps=2, llm_profile="normal"),
                                    metadata=AgentMetadata(visibility="tool", min_role="admin")),
                   registry)
    worker.llm, seen = _scripted_llm()
    registry.register("gated_worker", worker)
    sam = SubAgentManagerServer("sam", AgentSystemConfig(auth=AUTH), ToolServerConfig(allowed_agents=["*"]))

    async def params_for(user, **more):
        """The params tool execution hands the SAM for a run of *user* in its own parent session."""
        parent = f"parent-{user}"
        try:
            await service.session_manager.load_session(user, parent)
        except Exception:
            await service.session_manager.create_session(user_id=user, session_id=parent,
                                                         agent_name="coordinator", llm_profile="normal")
        caller = SimpleNamespace(registry=registry, _session_service=service)
        return {"_session_id": parent, "_user_id": user, "_session_service": service, "_agent": caller,
                "_request_id": f"rq-{user}", **more}

    async def sub_agents(user):
        stored = await service.session_manager.load_session(user, f"parent-{user}", bypass_cache=True)
        return (stored.get("metadata") or {}).get("sub_agents") or {}

    return SimpleNamespace(sam=sam, worker=worker, seen=seen, params_for=params_for, sub_agents=sub_agents)


async def test_a_users_spawn_of_a_gated_agent_is_refused_before_a_sub_session_exists(world):
    answer = await world.sam.manage_sub_agent(
        await world.params_for("bob", operation="create", agent_type="gated_worker", task="work"))

    assert answer["status"] == "error", answer
    assert answer["error_type"] == "agent_role_gate", answer
    assert "gated_worker" in answer["error"], answer
    assert await world.sub_agents("bob") == {}, "a sub-session was made for a run that cannot happen"
    assert world.seen == [], "the gated agent's LLM was called"


async def test_an_admins_spawn_runs(world):
    answer = await world.sam.manage_sub_agent(
        await world.params_for("root", operation="create", agent_type="gated_worker", task="work"))

    assert (answer["status"], answer["outcome"]) == ("completed", "completed"), answer
    assert world.seen, "the admin's sub-agent never ran"


async def test_a_continue_after_the_gate_was_raised_is_refused_before_the_instance_is_reopened(world):
    world.worker.min_role = None  # the gate at the time the instance was made
    created = await world.sam.manage_sub_agent(
        await world.params_for("bob", operation="create", agent_type="gated_worker", task="work"))
    assert created["outcome"] == "completed", f"fixture: the ungated create failed: {created}"
    calls = len(world.seen)
    ended = (await world.sub_agents("bob"))[created["instance_id"]]["status"]

    world.worker.min_role = "admin"  # what a config reload does to the live agent
    answer = await world.sam.manage_sub_agent(
        await world.params_for("bob", operation="continue", instance_id=created["instance_id"], message="more"))

    assert answer["status"] == "error" and answer["error_type"] == "agent_role_gate", answer
    assert len(world.seen) == calls, "the gated agent ran again"
    assert (await world.sub_agents("bob"))[created["instance_id"]]["status"] == ended, "the instance was reopened"


async def test_without_a_named_caller_a_gated_spawn_is_refused_whoever_owns_the_parent_folder(world):
    """No ``_user_id``, no registered request: the caller is unidentified. The SAM's own user lookup would
    walk the session folders and find root's -- the parent session is root's -- which is no identity."""
    params = await world.params_for("root", operation="create", agent_type="gated_worker", task="work")
    del params["_user_id"]

    answer = await world.sam.manage_sub_agent(params)

    assert answer["status"] == "error" and answer["error_type"] == "agent_role_gate", answer
    assert world.seen == []


@pytest.mark.parametrize("owner, injected, runs", [
    ("root", None, True),     # a caller without _user_id (repair pipeline, AgentCaller): the request names it
    ("bob", "root", False),   # the registered owner is asked first, as Agent._run_denial does
])
async def test_the_registered_request_owner_names_the_caller(world, owner, injected, runs):
    from agent_system.core.request_context import register_request_user, release_request_user_tree

    params = await world.params_for("root", operation="create", agent_type="gated_worker", task="work")
    params["_request_id"] = "rq-named"
    if injected:
        params["_user_id"] = injected
    else:
        del params["_user_id"]
    register_request_user("rq-named", owner)
    try:
        answer = await world.sam.manage_sub_agent(params)
    finally:
        release_request_user_tree("rq-named")

    assert (answer.get("error_type") != "agent_role_gate") is runs, answer
    assert bool(world.seen) is runs


async def test_the_spawn_runs_and_is_stored_as_the_user_the_gate_judged(world, tmp_path):
    """No ``_user_id``, a parent session not saved yet: the SAM's own lookup found nobody and answered
    "anonymous" -- so the sub-session was stored, registered and run as anonymous, and the gated sub-run's
    backstop refused what the SAM gate had let through. One user now: the registered owner."""
    from types import SimpleNamespace

    from agent_system.core.request_context import register_request_user, release_request_user_tree

    params = await world.params_for("root", operation="create", agent_type="gated_worker", task="work")
    params.update(_session_id="parent-unsaved", _request_id="rq-unsaved")
    del params["_user_id"]
    params["_agent"] = SimpleNamespace(registry=world.worker.registry, _session_service=params["_session_service"],
                                       name="coordinator", agent_config=world.worker.agent_config)
    register_request_user("rq-unsaved", "root")
    try:
        answer = await world.sam.manage_sub_agent(params)
    finally:
        release_request_user_tree("rq-unsaved")

    assert (answer.get("status"), answer.get("outcome")) == ("completed", "completed"), answer
    sessions = tmp_path / "sessions"
    assert (sessions / "root" / f"{answer['instance_id']}.json").exists(), "the sub-session is not root's"
    assert not (sessions / "anonymous").exists(), "stored under anonymous"
