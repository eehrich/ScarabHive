"""The Sub-Agents panel in a real browser, against the real plugin: its router, its list, info and delete handlers, the
session files they read and write, its static files. No agent runs: ``sub_research_a`` is held in ``_running_agents``,
the seam a run in this process goes through; the panel shows every sub-agent as stored, run here or not.

Seeded under ``tmp_path``, instance ``sam_writer`` with phase filtering on and ``info_max_limit`` 15, session ``s-1`` (phase ``planning``):
``sub_research_a`` running with the activity "Running tool: web_search"; ``sub_writer_b`` idle, its last activity
"Completed", with a 45-message transcript (a tool call and markup among it) and a task summary of markup;
``sub_writer_c`` reporting "Thinking..." without a run in this process -- as one running in another process does,
which the panel shows as stored and must not mark interrupted; ``sub_writer_h`` interrupted; ``sub_writer_d`` archived;
``sub_writer_e`` failed; and
``sub_other_f``, spawned by another manager instance, which this one does not list. Last used in that order, newest
first. Session ``s-2``: ``sub_writer_g`` idle. Session ``s-3``: none.

Behind the panel's back: POST /__stub/spawn creates a sub-agent in ``s-1`` (idle, newest), POST /__stub/drop/{id}
deletes a sub-agent's session file. GET /__stub/asked counts the lists asked for, per session. Cookies: ``sa_list=fails``
fails the list, ``sa_list=slow`` holds it 1.5 s; ``sa_slow=<id>`` holds that sub-agent's transcript 1.5 s, and its
archive 1.5 s before it archives.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
from agent_system.tools.base import ToolServerRegistry
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from agent_system.ui.resources import STATIC_DIR
from plugins.sub_agent_manager import web_endpoints
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 150
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
NAME = "sam_writer"
USER = "ada"
MARKUP = '<img src="x" onerror="window.parent.__xss = 1">'
CONFIG = dict(max_sub_agents_per_type=10, info_max_limit=15, allowed_agents=["story_designer", "research_agent", "writer_agent"],
              phase_filtering={"enabled": True, "phase_variable": "workflow_phase",
                               "phase_agents": {"planning": ["story_designer"], "_default": []}})


def registry() -> ToolServerRegistry:
    agents = ToolServerRegistry()
    for name in ("research_agent", "writer_agent", "other_agent"):
        agent = Mock()
        agent.name = name
        agent.agent_config = AgentConfig(llm_profile="normal")
        agents.register(name, agent)
    return agents


def transcript() -> list[dict]:
    messages = []
    for i in range(21):
        messages += [{"role": "user", "content": f"Question {i}"}, {"role": "assistant", "content": f"Answer {i}"}]
    messages.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "kit"}'}}]})
    messages.append({"role": "tool", "content": "Three results", "name": "web_search", "tool_call_id": "call_1"})
    messages.append({"role": "assistant", "content": MARKUP})
    return messages  # 45


async def spawn(server, service, parent: str, agent_type: str, label: str, creator: str = NAME, **metadata) -> str:
    manager = server._get_manager(service, registry())
    sub_id = await manager.create_sub_session(parent_session_id=parent, agent_type=agent_type, initial_message=f"Task of {label}",
                                              instance_label=label, params={"_creator_plugin": creator, "_user_id": USER})
    if metadata:
        await manager.update_sub_session_metadata(parent_session_id=parent, sub_session_id=sub_id, **metadata)
    return sub_id


async def seed(server, service) -> dict[str, str]:
    sessions = service.session_manager
    for session in ("s-1", "s-2", "s-3"):
        await sessions.create_session(user_id=USER, session_id=session, title=session, agent_name="coordinator", llm_profile="normal")
    parent = await sessions.load_session(USER, "s-1")
    parent["context_vars"] = {"workflow_phase": "planning"}
    await sessions.save_session(parent)
    stamp = lambda minutes: f"2026-09-15T10:{minutes:02d}:00+00:00"  # noqa: E731
    ids = {
        "a": await spawn(server, service, "s-1", "research_agent", "research_a", last_used=stamp(50),
                         current_activity="Running tool: web_search"),
        "b": await spawn(server, service, "s-1", "writer_agent", "writer_b", last_used=stamp(40), task_summary=MARKUP,
                         current_activity="Completed"),
        "c": await spawn(server, service, "s-1", "writer_agent", "writer_c", last_used=stamp(30), current_activity="Thinking..."),
        "h": await spawn(server, service, "s-1", "writer_agent", "writer_h", last_used=stamp(25), status="interrupted"),
        "d": await spawn(server, service, "s-1", "writer_agent", "writer_d", last_used=stamp(20), status="archived"),
        "e": await spawn(server, service, "s-1", "writer_agent", "writer_e", last_used=stamp(10), status="failed"),
        "f": await spawn(server, service, "s-1", "other_agent", "other_f", creator="sam_other", last_used=stamp(55)),
        "g": await spawn(server, service, "s-2", "writer_agent", "writer_g"),
    }
    sub = await sessions.load_session(USER, ids["b"])
    sub["messages"] = transcript()
    await sessions.save_session(sub)
    return ids


def panel_app(tmp_path: Path) -> FastAPI:
    from plugins.sub_agent_manager.plugin import PLUGIN_FACTORY

    seeding = PLUGIN_FACTORY(NAME, AgentSystemConfig(), ToolServerConfig(**CONFIG))
    ids = asyncio.run(seed(seeding.server, SessionService(session_manager=SessionManager(storage_path=str(tmp_path)))))
    assert len(set(ids.values())) == 8

    plugin = PLUGIN_FACTORY(NAME, AgentSystemConfig(), ToolServerConfig(**CONFIG))  # served in the app's own event loop
    service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path)))
    plugin.server._running_agents.add(ids["a"])
    app = FastAPI()
    app.state.session_service = service
    app.state.ids = ids
    asked: dict[str, int] = {}
    base = f"/plugins/{NAME}/sub-agents"

    @app.middleware("http")
    async def stub_modes(request: Request, call_next):
        path = request.url.path
        held = False
        if path == base:
            session = request.query_params.get("session_id", "")
            asked[session] = asked.get(session, 0) + 1
            if request.cookies.get("sa_list") == "fails":
                return JSONResponse({"detail": "The session store is locked"}, status_code=500)
            held = request.cookies.get("sa_list") == "slow"
        slow = request.cookies.get("sa_slow")
        if slow and path == f"{base}/{slow}":
            held = True
        if not held:
            return await call_next(request)
        if request.method == "DELETE":  # held before it archives: until answered, the list still shows the sub-agent open
            await asyncio.sleep(1.5)
            return await call_next(request)
        answer = await call_next(request)
        payload = b"".join([chunk async for chunk in answer.body_iterator])

        async def body():  # headers at once: an identical request must not queue behind this one in the browser
            await asyncio.sleep(1.5)
            yield payload
        return StreamingResponse(body(), status_code=answer.status_code, media_type="application/json",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/__stub/ids")
    async def seeded_ids():
        return ids

    @app.get("/__stub/asked")
    async def lists_asked():
        return asked

    @app.post("/__stub/spawn")
    async def spawned():
        return {"id": await spawn(plugin.server, service, "s-1", "writer_agent", "writer_new", last_used="2026-09-15T11:00:00+00:00")}

    @app.post("/__stub/drop/{sub_id}")
    async def drop(sub_id: str):
        await service.session_manager.delete_session(USER, sub_id, create_backup=False)
        return {}

    @app.get("/__stub/raw/{session}")
    async def raw_session(session: str):
        return {"text": service.session_manager._get_session_path(USER, session).read_text(encoding="utf-8")}

    @app.get("/__stub/status/{session}/{sub_id}")
    async def stored_status(session: str, sub_id: str):
        parent = await service.session_manager.load_session(USER, session, bypass_cache=True)
        return {"status": parent["metadata"]["sub_agents"][sub_id]["status"]}

    registry_ = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry_.register_web_plugin(NAME, plugin)
    registry_.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/sub_agent_manager", StaticFiles(directory=TESTS), name="panel-tests")
    return app


def run_panel(app: FastAPI) -> dict:
    """The page's results, the web endpoints reaching the app's session service as they reach the running app's."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(web_endpoints, "get_session_service", lambda: app.state.session_service)
        return run_app_test_page(BROWSER, app, "tests/sub_agent_manager/panel_tests.html", timeout=PAGE_TIMEOUT)


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    return run_panel(panel_app(tmp_path_factory.mktemp("sam_panel")))


EXPECTED = [
    'opening and refreshing the panel leaves the stored sub-agents of the session byte for byte as they were',
    'opened on a session, the panel counts its sub-agents by state and draws the open ones with what each is doing',
    'the filter shows the running ones or all, archived and ended included',
    'a transcript opens in the drawer on its tail, earlier messages load above it, markup as text',
    'a transcript opened after another shows only the one opened last',
    'archiving asks once, cancelled keeps the sub-agent and confirmed archives it on the server',
    'an archive on its way keeps its button disabled across redraws, and the other cards usable',
    'a sub-agent gone behind the panel is refused with a notice and opens nothing',
    'the endpoints refuse a sub-agent of another session or none as not found',
    'a failed load shows the error and none of the sub-agents shown before',
    'a tick of the auto refresh leaves a load still on its way alone',
    'an answer for the session left behind is not drawn, and an open transcript stays without taking the focus',
    'an archive acts on the session whose card was clicked, also after the chat switched',
    'with no session open the panel says so and asks for nothing',
    'cards drawn anew keep the keyboard focus',
    'closing the drawer gives the focus back to its card, also when the list was drawn anew behind it',
    'the auto refresh runs from the start and brings a new sub-agent',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_sub_agent_manager_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
