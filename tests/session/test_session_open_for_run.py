"""SessionService.open_for_run: the one way a run opens its session.

/run, /events, agent-cli and agent-run each did it by hand, and a new session
got the agent's template_vars in three of five.
"""
from __future__ import annotations

import pytest

from agent_system.config.models import (
    AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
    PluginsConfig, ToolServerConfig,
)
from agent_system.llm.models import ChatMessage
from agent_system.servers.agent.entry import entry_agent
from agent_system.services.session_manager import SessionManager, SessionPermissionError
from agent_system.services.session_service import SessionService
from agent_system.tools.base import ToolServerRegistry

pytestmark = pytest.mark.anyio
OWN_VARS = {"workflow_phase": "planning"}


@pytest.fixture
def setup(tmp_path):
    config = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
            profiles={"normal": LLMProfile(model_ref="m")},
            default_profile="normal",
        ),
        plugins=PluginsConfig(servers={"probe": ToolServerConfig(
            type="agent", enabled=True,
            agent_config=AgentConfig(llm_profile="normal", template_vars=dict(OWN_VARS)))}),
    )
    agent = entry_agent("probe", config, ToolServerRegistry())
    manager = SessionManager(storage_path=str(tmp_path))
    return agent, manager, SessionService(manager)


async def _stored(manager, user_id, session_id, messages, context_vars):
    session = await manager.create_session(user_id=user_id, session_id=session_id,
                                           agent_name="probe", llm_profile="stored-profile")
    session["messages"] = messages
    session["context_vars"] = context_vars
    await manager.save_session(session)


async def test_a_new_session_starts_from_nothing_on_the_agents_template_vars(setup):
    """What this process still holds under the id (a first save that failed)
    is not the new session: no messages, no vars of its own, no start mark."""
    agent, _manager, service = setup
    tracker = agent._session_tracker
    tracker.set_session_messages("s-new", [ChatMessage(role="user", content="left over in this process")])
    tracker.set_session_template_vars("s-new", {"workflow_phase": "drafting", "book_id": 7})
    assert tracker.start_session("s-new") is False, "fixture: the leftover must count as started"

    existed = await service.open_for_run(agent, "alice", "s-new", "turbo")

    assert existed is False
    assert tracker.get_session_messages("s-new") == [], "a new session carried what the process held"
    assert tracker.get_session_template_vars("s-new") == OWN_VARS, "leftover vars survived"
    assert tracker.start_session("s-new") is True, "the new session reads as already started"
    assert tracker.get_session_metadata("s-new") == {
        "user_id": "alice", "agent_name": "probe", "llm_profile": "turbo"}


async def test_a_stored_session_keeps_its_own_vars_and_names_this_run(setup):
    agent, manager, service = setup
    await _stored(manager, "alice", "s-old", [{"role": "user", "content": "earlier"}],
                  {"workflow_phase": "drafting"})

    existed = await service.open_for_run(agent, "alice", "s-old", "turbo")

    tracker = agent._session_tracker
    assert existed is True
    assert [m.content for m in tracker.get_session_messages("s-old")] == ["earlier"]
    assert tracker.get_session_template_vars("s-old") == {"workflow_phase": "drafting"}, (
        "the agent's defaults went over what the session had")
    assert tracker.get_session_metadata("s-old")["llm_profile"] == "turbo", "not this run's profile"


async def test_another_users_session_is_refused(setup):
    agent, manager, service = setup
    await _stored(manager, "alice", "s-hers", [{"role": "user", "content": "private"}], {})

    with pytest.raises(SessionPermissionError):
        await service.open_for_run(agent, "mallory", "s-hers", "turbo")
    assert agent._session_tracker.get_session_messages("s-hers") == []


async def _run_has(agent, session_id, request_id="run_1") -> dict:
    """A run of this agent has the session: its lock, and its state in the tracker. Returns the run's metadata."""
    tracker = agent._session_tracker
    tracker.set_session_messages(session_id, [ChatMessage(role="user", content="earlier"),
                                              ChatMessage(role="user", content="the run's unsaved turn")])
    tracker.set_session_template_vars(session_id, {"workflow_phase": "writing"})
    metadata = {"user_id": "alice", "agent_name": "probe", "llm_profile": "the run's"}
    tracker.set_session_metadata(session_id, metadata)
    assert await tracker.acquire_session_lock(session_id, request_id), "fixture: the lock was not taken"
    return metadata


@pytest.mark.parametrize("stored", [True, False], ids=["stored", "not saved yet"])
async def test_a_session_a_run_of_this_agent_holds_is_left_to_that_run(setup, stored):
    """A second opener while a run of this agent has the session (a second web tab, a web chat beside an API
    turn): its own run is refused at the session lock -- but opening it read the session back from disk under the
    running one, which lost its turn so far, and set new metadata naming the opener as the run's user."""
    agent, manager, service = setup
    if stored:
        await _stored(manager, "alice", "s-run", [{"role": "user", "content": "earlier"}],
                      {"workflow_phase": "drafting"})
    metadata = await _run_has(agent, "s-run")
    tracker = agent._session_tracker

    existed = await service.open_for_run(agent, "alice", "s-run", "turbo", in_use=True)

    assert existed is True, "a session a run has is new to the opener -- /events then set new metadata per event"
    assert [m.content for m in tracker.get_session_messages("s-run")] == ["earlier", "the run's unsaved turn"]
    assert tracker.get_session_template_vars("s-run") == {"workflow_phase": "writing"}
    assert tracker.get_session_metadata("s-run") is metadata, "the opener replaced the run's metadata"


async def test_another_user_is_refused_a_session_a_run_of_this_agent_holds(setup):
    agent, manager, service = setup
    await _stored(manager, "alice", "s-run", [{"role": "user", "content": "earlier"}], {})
    metadata = await _run_has(agent, "s-run")

    with pytest.raises(SessionPermissionError):
        await service.open_for_run(agent, "mallory", "s-run", "turbo", in_use=True)
    assert agent._session_tracker.get_session_metadata("s-run") is metadata


@pytest.mark.parametrize("in_use, locked", [(True, False), (False, True)],
                         ids=["in use elsewhere", "locked by the opener"])
async def test_a_session_no_run_of_this_agent_holds_for_another_is_read_as_usual(setup, in_use, locked):
    """In use without this agent's lock -- a run on another agent, or a job whose run has let go of the lock,
    which it does after its last save -- this agent's tracker holds no run's state the disk has not. And the lock without in_use is the opener's own (an API turn takes
    it before it opens the session). Both read the session from disk as usual."""
    agent, manager, service = setup
    await _stored(manager, "alice", "s-run", [{"role": "user", "content": "on disk"}], {"workflow_phase": "drafting"})
    tracker = agent._session_tracker
    tracker.set_session_messages("s-run", [ChatMessage(role="user", content="stale copy")])
    if locked:
        assert await tracker.acquire_session_lock("s-run", "the_opener"), "fixture: the lock was not taken"

    existed = await service.open_for_run(agent, "alice", "s-run", "turbo", in_use=in_use)

    assert existed is True
    assert [m.content for m in tracker.get_session_messages("s-run")] == ["on disk"]
    assert tracker.get_session_metadata("s-run")["llm_profile"] == "turbo"
