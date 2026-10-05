"""The phase the Sub-Agents panel shows: the session's own variable, else the default its agent's configuration sets --
the two create judges by (server._get_current_phase)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.plugins.tool_adapter import PluginToolAdapter, plugin_tool_registry
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from plugins.sub_agent_manager.server import SubAgentManagerServer
from plugins.sub_agent_manager.web_endpoints import SubAgentManagerWebFactory

CONFIG = dict(allowed_agents=["planner", "builder"],
              phase_filtering={"enabled": True, "phase_variable": "workflow_phase",
                               "phase_agents": {"planning": ["planner"]}})


def phase_of(tmp_path, context_vars: dict) -> dict:
    service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path)))

    async def seed_and_ask():
        sessions = service.session_manager
        await sessions.create_session(user_id="ada", session_id="s-1", title="s-1", agent_name="coordinator",
                                      llm_profile="normal")
        if context_vars:
            stored = await sessions.load_session("ada", "s-1")
            stored["context_vars"] = context_vars
            await sessions.save_session(stored)
        factory = SubAgentManagerWebFactory(SubAgentManagerServer("sam", AgentSystemConfig(), ToolServerConfig(**CONFIG)))
        return await factory._phase(service, "ada", "s-1")
    return asyncio.run(seed_and_ask())


@pytest.fixture
def coordinator(monkeypatch):
    agent = SimpleNamespace(agent_config=SimpleNamespace(template_vars={"workflow_phase": "planning"}))
    monkeypatch.setitem(plugin_tool_registry.plugin_servers, "coordinator", PluginToolAdapter("coordinator", agent))


def test_the_panel_shows_the_phase_the_agents_configuration_sets(tmp_path, coordinator):
    phase = phase_of(tmp_path, {})
    assert phase["current"] == "planning"
    assert phase["agents"] == ["planner"]


def test_the_sessions_own_phase_wins_over_the_configured_one(tmp_path, coordinator):
    phase = phase_of(tmp_path, {"workflow_phase": "building"})
    assert phase["current"] == "building"
    assert phase["agents"] == ["planner", "builder"]  # a phase the map does not name: every allowed agent
