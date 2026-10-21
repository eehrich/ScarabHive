"""has_session_data: every manager instance's panel lists every sub-agent of a session, so the chat's "Open in" menu
offers only the instance that answers for one -- the instance that spawned it, by the entry the spawn wrote into the
parent session. Two instances are loaded, as plugins.yaml would load them; the sub-agent is spawned through the
manager's own create path. A session whose sub-sessions no manager spawned (a machine's run) offers none."""
import asyncio
from unittest.mock import Mock

import pytest

from agent_system import app_state
from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
from agent_system.plugins.tool_adapter import PluginToolAdapter, plugin_tool_registry
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from agent_system.tools.base import ToolServerRegistry

USER = "ada"


def _registry() -> ToolServerRegistry:
    agents = ToolServerRegistry()
    agent = Mock()
    agent.name = "research_agent"
    agent.agent_config = AgentConfig(llm_profile="normal")
    agents.register("research_agent", agent)
    return agents


@pytest.fixture
def instances(tmp_path, monkeypatch):
    from plugins.sub_agent_manager.plugin import PLUGIN_FACTORY

    service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path)))
    monkeypatch.setattr(app_state, "session_service", service)
    plugins = {}
    for name in ("sam_a", "sam_b"):
        plugins[name] = PLUGIN_FACTORY(name, AgentSystemConfig(), ToolServerConfig(allowed_agents=["research_agent"]))
        monkeypatch.setitem(plugin_tool_registry.plugin_servers, name, PluginToolAdapter(name, plugins[name]))

    async def seed():
        sessions = service.session_manager
        for session_id in ("s-spawned", "s-none", "s-run"):
            await sessions.create_session(user_id=USER, session_id=session_id)
        manager = plugins["sam_a"].server._get_manager(service, _registry())
        await manager.create_sub_session(parent_session_id="s-spawned", agent_type="research_agent",
                                         initial_message="Look it up", instance_label="look",
                                         params={"_creator_plugin": "sam_a", "_user_id": USER})
        await sessions.create_session(user_id=USER, session_id="s-run-step", parent_session_id="s-run")

    asyncio.run(seed())
    return plugins


def _offered(plugins, session_id, user_id=USER):
    return {name for name, plugin in plugins.items() if asyncio.run(plugin.has_session_data(session_id, user_id))}


def test_only_the_spawning_instance_is_offered(instances):
    assert _offered(instances, "s-spawned") == {"sam_a"}


def test_a_session_without_sub_agents_offers_none(instances):
    assert _offered(instances, "s-none") == set()
    assert _offered(instances, "s-run") == set(), "sub-sessions no manager spawned were taken for sub-agents"


def test_another_users_session_is_offered(instances):
    """An admin's view: under whom its sub-agents lie is a scan away, so every panel is offered."""
    assert _offered(instances, "s-none", "an-admin") == {"sam_a", "sam_b"}


def test_an_entry_whose_sub_session_was_deleted_still_offers_its_instance(instances):
    """The panel lists the parent's entries, not its sub-sessions: one deleted behind its entry is still shown."""
    sessions = app_state.session_service.session_manager

    async def delete_the_sub_session():
        parent = await sessions.load_session(USER, "s-spawned")
        (instance_id,) = parent["metadata"]["sub_agents"]
        await sessions.delete_session(USER, instance_id, create_backup=False)
        assert not sessions._has_children(USER, "s-spawned"), "fixture: the sub-session is still there"

    asyncio.run(delete_the_sub_session())

    assert _offered(instances, "s-spawned") == {"sam_a"}
