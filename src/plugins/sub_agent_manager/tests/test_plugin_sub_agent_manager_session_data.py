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


def test_a_sub_agent_no_instance_claims_offers_one_instance_only(instances):
    """A machine's run spawns without naming a manager: every instance's panel shows the entry, one is offered."""
    service = app_state.session_service

    async def spawn_unclaimed():
        await service.session_manager.create_session(user_id=USER, session_id="s-machine")
        manager = instances["sam_b"].server._get_manager(service, _registry())
        await manager.create_sub_session(parent_session_id="s-machine", agent_type="research_agent",
                                         initial_message="Draft it", instance_label="draft",
                                         params={"_user_id": USER})
        parent = await service.session_manager.load_session(USER, "s-machine")
        assert [m.get("creator_plugin") for m in parent["metadata"]["sub_agents"].values()] == [None], \
            "fixture: the entry names a creator"

    asyncio.run(spawn_unclaimed())

    assert _offered(instances, "s-machine") == {"sam_a"}


def test_a_sub_agent_the_second_instance_spawned_offers_that_one(instances):
    service = app_state.session_service

    async def spawn_by_b():
        await service.session_manager.create_session(user_id=USER, session_id="s-by-b")
        manager = instances["sam_b"].server._get_manager(service, _registry())
        await manager.create_sub_session(parent_session_id="s-by-b", agent_type="research_agent",
                                         initial_message="Check it", instance_label="check",
                                         params={"_creator_plugin": "sam_b", "_user_id": USER})

    asyncio.run(spawn_by_b())

    assert _offered(instances, "s-by-b") == {"sam_b"}


def test_the_instances_asking_at_once_read_the_session_once(instances, monkeypatch):
    """The menu asks every instance at the same moment: one read, not one parse per instance under the lock."""
    sessions = app_state.session_service.session_manager
    real, reads = sessions.load_session, []

    async def counted(user_id, session_id, *args, **kwargs):
        reads.append(session_id)
        await asyncio.sleep(0.05)  # the others ask meanwhile, as they do while a large file is parsed
        return await real(user_id, session_id, *args, **kwargs)
    monkeypatch.setattr(sessions, "load_session", counted)

    async def ask_all():
        return await asyncio.gather(*(plugin.has_session_data("s-spawned", USER) for plugin in instances.values()))

    answers = asyncio.run(ask_all())

    assert dict(zip(instances, answers)) == {"sam_a": True, "sam_b": False}
    assert reads == ["s-spawned"], f"read {len(reads)} times"


def test_one_asker_giving_up_leaves_the_shared_read_to_the_others():
    from plugins.sub_agent_manager.web_endpoints import read_once

    class Slow:
        async def load_session(self, user_id, session_id):
            await asyncio.sleep(0.1)
            return {"metadata": {"sub_agents": {}}}

    async def one_quits_one_waits():
        sessions = Slow()
        quitter = asyncio.create_task(asyncio.wait_for(read_once(sessions, USER, "s-x"), 0.01))
        waiter = asyncio.create_task(read_once(sessions, USER, "s-x"))
        with pytest.raises(TimeoutError):
            await quitter
        return await waiter

    assert asyncio.run(one_quits_one_waits()) == {"metadata": {"sub_agents": {}}}


def test_a_sub_agent_spawned_through_the_tool_offers_the_spawning_instance(instances, monkeypatch):
    """The instance's own create path writes who spawned (no creator handed in): stopped right after the entry is
    written, before a run would start."""
    from types import SimpleNamespace

    from plugins.sub_agent_manager.manager import SubAgentManager

    class Written(Exception):
        pass

    real = SubAgentManager.create_sub_session

    async def write_then_stop(self, *args, **kwargs):
        await real(self, *args, **kwargs)
        raise Written("entry written")

    monkeypatch.setattr(SubAgentManager, "create_sub_session", write_then_stop)
    service = app_state.session_service

    async def spawn_through_the_tool():
        await service.session_manager.create_session(user_id=USER, session_id="s-tool")
        answer = await instances["sam_b"].server._handle_create({
            "agent_type": "research_agent", "task": "Look it up", "_session_id": "s-tool", "_user_id": USER,
            "_session_service": service, "_agent": SimpleNamespace(registry=_registry())})
        assert "entry written" in answer["error"], f"fixture: stopped elsewhere ({answer})"

    asyncio.run(spawn_through_the_tool())

    assert _offered(instances, "s-tool") == {"sam_b"}


def test_the_instances_declare_their_panels_show_one_list(instances):
    """The menu lets a visible instance stand in for the one that answers only when the plugin declares this."""
    assert all(plugin.session_data_shared is True for plugin in instances.values())
