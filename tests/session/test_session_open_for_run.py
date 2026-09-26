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
