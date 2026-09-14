"""The Todos panel in a real browser, against the real plugin: its router, its task store, its static files.

Seeded, session ``s-1``: task_001 "Design the schema" (high, completed), task_002 "Implement the model" (critical,
depends on task_001, in progress at 40%), task_003 "Write the tests" (medium, depends on task_002 and so blocked,
tags ``qa`` and ``backend``, a description), task_004 "Drop the legacy importer" (low, cancelled), task_005 "Review
the pull request" (medium, not started) and task_006 "Update the changelog" (low, not started, made to depend on
task_002 after its creation: waiting on it without being blocked). Sessions ``s-2`` and ``s-3``: task_001 to
task_003, "Second session task 1" to 3 and "Third session task 1" to 3, none started. Behind the panel's back in
``s-1``: POST /__stub/gone deletes task_003, POST /__stub/depend adds task_007 depending on task_005, POST
/__stub/finish completes task_006, POST /__stub/cancel cancels task_007, POST /__stub/add adds a task. GET /__stub/asked counts the task lists asked for,
per session. With the cookie ``td_tasks=fails`` the task list fails, with ``td_tasks=slow`` it takes 1.5 s.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
S1 = {"session_id": "s-1"}


async def seed(server) -> None:
    await server.create_todo("Design the schema", priority="high", context=S1)
    await server.update_todo("task_001", new_status="completed", context=S1)
    await server.create_todo("Implement the model", priority="critical", depends_on=["task_001"], context=S1)
    await server.update_todo("task_002", new_status="in-progress", progress=40, context=S1)
    await server.create_todo("Write the tests", description="Unit and browser tests", tags=["qa", "backend"],
                             depends_on=["task_002"], context=S1)
    await server.create_todo("Drop the legacy importer", priority="low", context=S1)
    await server.update_todo("task_004", new_status="cancelled", context=S1)
    await server.create_todo("Review the pull request", context=S1)
    await server.create_todo("Update the changelog", priority="low", context=S1)
    await server.update_todo("task_006", add_depends_on=["task_002"], context=S1)
    for session, name in (("s-2", "Second"), ("s-3", "Third")):
        for number in range(1, 4):
            await server.create_todo(f"{name} session task {number}", allow_duplicates=True, context={"session_id": session})


def panel_app(tmp_path: Path):
    from plugins.todo.plugin import PLUGIN_FACTORY

    config = SimpleNamespace(storage_path=str(tmp_path))
    asyncio.run(seed(PLUGIN_FACTORY("todo", {}, config).server))  # on disk: the panel's plugin reads it from there
    plugin = PLUGIN_FACTORY("todo", {}, config)
    app = FastAPI()
    asked: dict[str, int] = {}

    @app.middleware("http")
    async def task_lists(request: Request, call_next):
        mode = request.cookies.get("td_tasks")
        if request.method == "GET" and request.url.path == "/plugins/todo/tasks":
            session = request.query_params["session_id"]
            asked[session] = asked.get(session, 0) + 1
            if mode == "fails":
                return JSONResponse({"detail": "The task store is locked"}, status_code=500)
            if mode == "slow":
                await asyncio.sleep(1.5)
        return await call_next(request)

    @app.get("/__stub/asked")
    async def lists_asked():
        return asked

    @app.post("/__stub/gone")
    async def gone():
        await plugin.server.delete_todo("task_003", context=S1)
        return {}

    @app.post("/__stub/depend")
    async def depend():
        await plugin.server.create_todo("Follow up on the review", depends_on=["task_005"], context=S1)
        return {}

    @app.post("/__stub/finish")
    async def finish():
        await plugin.server.update_todo("task_006", new_status="completed", context=S1)
        return {}

    @app.post("/__stub/cancel")
    async def cancel():
        await plugin.server.update_todo("task_007", new_status="cancelled", context=S1)
        return {}

    @app.post("/__stub/add")
    async def add():
        await plugin.server.create_todo("Added by an agent", context=S1)
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("todo", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/todo", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    app = panel_app(tmp_path_factory.mktemp("todo_panel"))
    return run_app_test_page(BROWSER, app, "tests/todo/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'opened on a session, the panel counts its tasks by status and draws each with what can be done to it',
    'the filters narrow the tasks by status and priority and say how many are shown',
    'an action acts on the session whose task was clicked, also while the list switches',
    'start and complete change the task on the server, and a completed dependency unblocks the next',
    'deleting asks first: cancelled the task stays, confirmed it goes',
    'a refusal of the server is shown and the list loaded anew',
    'a failed load shows the error and none of the tasks shown before, also through a filter',
    'a tick of the auto refresh leaves a load still on its way alone',
    'with no session open the panel says so',
    'the auto refresh runs from the start and brings a task an agent adds',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_todo_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
