"""The Sub-Agents panel with example data, for the guide's screenshot (src/scripts/guide_screenshots.py): the real
plugin router and session files under ``tmp_path``, no agent runs.

Instance ``sub_agent_manager``, viewer ``ada``, session ``s-1``: ``sub_research_0001`` running (held in
``_running_agents``) with its current tool, ``sub_review_0002`` idle with a short transcript and the tokens of its last
call, ``sub_tests_0003`` interrupted, ``sub_pricing_0004`` failed, ``sub_review_0005`` archived; under
``sub_review_0002`` its own ``sub_lookup_0006``, idle. The ids count from 1 in this process.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agent_system.auth.dependencies import get_optional_user
from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
from agent_system.plugins.tool_adapter import PluginToolAdapter, plugin_tool_registry
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from agent_system.tools.base import ToolServerRegistry
from agent_system.ui.resources import STATIC_DIR
from plugins.sub_agent_manager import web_endpoints
from plugins.sub_agent_manager.manager import SubAgentManager

NAME = "sub_agent_manager"
USER = "ada"
AGENTS = ("research_agent", "code_reviewer", "test_runner")


def _registry() -> ToolServerRegistry:
    agents = ToolServerRegistry()
    for name in AGENTS:
        agent = Mock()
        agent.name = name
        agent.agent_config = AgentConfig(llm_profile="normal")
        agents.register(name, agent)
    return agents


def _ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


async def _seed(server, service) -> dict[str, str]:
    sessions = service.session_manager
    await sessions.create_session(user_id=USER, session_id="s-1", title="Release checklist", agent_name="assistant",
                                  llm_profile="normal")
    manager = server._get_manager(service, _registry())
    SubAgentManager._class_counter = 0  # readable ids in the picture: _0001, _0002, ...

    async def spawn(parent: str, agent_type: str, label: str, task: str, created: int, **metadata) -> str:
        sub_id = await manager.create_sub_session(parent_session_id=parent, agent_type=agent_type, initial_message=task,
                                                  instance_label=label, params={"_creator_plugin": NAME, "_user_id": USER})
        await manager.update_sub_session_metadata(parent_session_id=parent, sub_session_id=sub_id,
                                                  created_at=_ago(created), **metadata)
        return sub_id

    ids = {
        "research": await spawn("s-1", "research_agent", "research", "Compare the SLAs the three storage vendors publish",
                                40, last_used=_ago(9), current_activity="🔧 Running tool: web_search",
                                activity_updated_at=_ago(1)),
        "review": await spawn("s-1", "code_reviewer", "review", "Review the retry logic in the upload client", 35,
                              last_used=_ago(12)),
        "tests": await spawn("s-1", "test_runner", "tests", "Run the integration tests against staging", 30,
                             last_used=_ago(20), status="interrupted"),
        "pricing": await spawn("s-1", "research_agent", "pricing", "Summarise the vendors' pricing pages", 25,
                               last_used=_ago(24), status="failed", error="Error: the provider timed out"),
        "old": await spawn("s-1", "code_reviewer", "review", "Check the changelog wording", 90, last_used=_ago(80),
                           status="archived"),
    }
    ids["lookup"] = await spawn(ids["review"], "research_agent", "lookup", "Find the upstream issue about timeouts",
                                20, last_used=_ago(16))
    for key in ("research", "tests", "pricing", "old", "lookup"):
        other = await sessions.load_session(USER, ids[key])
        other["messages"] = [{"role": "user", "content": other.get("title") or "Task"},
                             {"role": "assistant", "content": "Working on it."}]
        await sessions.save_session(other)
    sub = await sessions.load_session(USER, ids["review"])
    sub["messages"] = [
        {"role": "user", "content": "Review the retry logic in the upload client"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "read_file", "arguments": '{"path": "src/upload/client.py"}'}}]},
        {"role": "tool", "content": "class UploadClient: ...", "name": "read_file", "tool_call_id": "c1"},
        {"role": "assistant", "content": "The retry loop never backs off: three attempts in under a second. "
                                         "Use an exponential delay and give up on 4xx answers."},
    ]
    await sessions.save_session(sub)
    return ids


def panel_app(tmp_path: Path, monkeypatch) -> FastAPI:
    from plugins.context_usage_tracker.tracker import UsageTracker
    from plugins.sub_agent_manager.plugin import PLUGIN_FACTORY

    config = ToolServerConfig(allowed_agents=list(AGENTS), max_sub_agents_per_type=10)
    plugin = PLUGIN_FACTORY(NAME, AgentSystemConfig(), config)
    service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path)))
    ids = asyncio.run(_seed(plugin.server, service))
    service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path)))  # served in the app's loop
    plugin.server._running_agents.add(ids["research"])

    tracker = UsageTracker(storage_path=tmp_path / "usage" / "usage.json")
    tracker.record_usage(agent_id="review", agent_name="code_reviewer", session_id=ids["review"], total_tokens=13000,
                         prompt_tokens=12000, completion_tokens=1000, context_window=200000)
    monkeypatch.setattr(web_endpoints, "get_session_service", lambda: service)
    monkeypatch.setitem(plugin_tool_registry.plugin_servers, "context_usage_tracker",
                        PluginToolAdapter("context_usage_tracker", Mock(tracker=tracker)))

    app = FastAPI()
    app.dependency_overrides[get_optional_user] = lambda: SimpleNamespace(username=USER)
    web = PluginWebRegistry()
    web.register_web_plugin(NAME, plugin)
    web.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
