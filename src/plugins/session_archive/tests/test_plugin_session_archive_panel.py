"""The Session Archive panel in a real browser, against the real plugin and a real archive.

Seeded for ``anonymous``: three archived conversations -- ``root_one`` ("The
first conversation", three sessions), ``root_two`` (two) and ``root_three``
(two), archived in that order so the newest is first -- plus one live tree
``root_old`` of two sessions that is old enough for a sweep and one, ``root_new``,
that is not.

With the cookie ``sa_mode=refuse`` a restore is answered 409, ``slow`` holds it
1.5 s, ``fails`` fails the listing, ``cap`` lets a sweep take one tree. GET /__stub/live says which sessions are
live again; POST /__stub/asked counts the deletions that reached the service.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agent_system.auth.dependencies import get_optional_user
from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.services.session_archive import SessionArchive
from agent_system.services.session_manager import SessionManager
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
USER = "anonymous"


async def _tree(sm: SessionManager, root: str, title: str, children: list[str], days: float) -> None:
    await sm.create_session(user_id=USER, title=title, session_id=root, agent_name="chat_agent")
    for child in children:
        await sm.create_session(user_id=USER, title=child, session_id=child,
                                agent_name="chat_agent", parent_session_id=root)
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    user_dir = sm.storage_path / USER
    for session_id in [root, *children]:
        path = user_dir / f"{session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["messages"] = [{"role": "user", "content": f"hello from {session_id}"}]
        data["updated_at"] = stamp
        path.write_text(json.dumps(data), encoding="utf-8")
        old = time.time() - days * 86400
        os.utime(path, (old, old))
    for index_path in [user_dir / "index.json", *user_dir.glob(".subs.*.index.json")]:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for session_id in [root, *children]:
            if session_id in index:
                index[session_id]["updated_at"] = stamp
        index_path.write_text(json.dumps(index), encoding="utf-8")
    sm.clear_cache()


async def _seed(archive: SessionArchive, sm: SessionManager) -> None:
    # Archived one at a time, so "archived_at" orders them.
    for root, title, children in [
        ("root_one", "The first conversation", ["kid_one_a", "kid_one_b"]),
        ("root_two", "The second one", ["kid_two_a"]),
        ("root_three", "The third one", ["kid_three_a"]),
    ]:
        await _tree(sm, root, title, children, days=90)
        await archive.archive_user(USER)
        await asyncio.sleep(0.01)
    await _tree(sm, "root_old", "Old enough to sweep", ["kid_old_a"], days=90)
    await _tree(sm, "root_new", "Still in use", [], days=1)


def panel_app(tmp_path: Path) -> FastAPI:
    from plugins.session_archive.plugin import PLUGIN_FACTORY

    sm = SessionManager(storage_path=str(tmp_path / "sessions"))
    archive = SessionArchive(sm, archive_path=str(tmp_path / "session_archive"))
    asyncio.run(_seed(archive, sm))

    plugin = PLUGIN_FACTORY("session_archive", AgentSystemConfig(), ToolServerConfig())
    app = FastAPI()
    app.state.session_archive = archive
    asked = {"forgets": 0}

    @app.middleware("http")
    async def failing_or_slow(request: Request, call_next):
        mode = request.cookies.get("sa_mode")
        path = request.url.path
        if mode == "refuse" and path.endswith("/restore"):
            return JSONResponse(
                {"detail": "2 session(s) of this tree are live again (kid_three_a)"},
                status_code=409)
        if mode == "fails" and path.endswith("/archived"):
            return JSONResponse({"detail": "The archive is locked"}, status_code=500)
        if mode == "slow" and path.endswith("/restore"):
            await asyncio.sleep(1.5)
        if path.endswith("/sweep"):
            # One tree per pass, so the pass ends capped with the rest waiting.
            archive.max_trees_per_sweep = 1 if mode == "cap" else 200
        if request.method == "DELETE" and "/archived/" in path:
            asked["forgets"] += 1
        return await call_next(request)

    @app.get("/__stub/live")
    async def live():
        roots = [s["session_id"] for s in await sm.list_root_sessions(USER)]
        children = await sm.list_child_sessions(USER, "root_one")
        return {"roots": roots, "children_of_root_one": len(children)}

    @app.post("/__stub/asked")
    async def counts():
        return dict(asked)

    # No auth in this app: the panel answers about the requesting user, and
    # without a token that is "anonymous" -- which is what the archive holds.
    app.dependency_overrides[get_optional_user] = lambda: None

    registry = PluginWebRegistry()  # the plugin's router and static files, as the app mounts them
    registry.register_web_plugin("session_archive", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/session_archive", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    app = panel_app(tmp_path_factory.mktemp("archive_panel"))
    return run_app_test_page(
        BROWSER, app, "tests/session_archive/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'the panel lists every archived conversation with its figures',
    'a number column sorts by its value, not by the text it rounds to',
    'a sort holds through a redraw, and clicking again reverses it',
    'a conversation restored leaves the archive and is a session again',
    'deleting asks first, and a cancelled question changes nothing',
    'deleting confirmed takes the archive away for good',
    'a refusal from the server is shown and the conversation stays',
    'the buttons of a row stay off until the action is answered',
    'with nothing archived the panel says when conversations move here',
    'a capped pass says how much is still waiting, and a finished one does not',
    'archiving by hand again finds nothing and says that too',
    'a failed load shows the error instead of what was there before',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_session_archive_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
