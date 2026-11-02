"""The shell in a real browser in the two views: a user gets the simple view -- their panels, topics in the composer,
no agent or model choice, their areas on the start page; an admin sees everything and switches in the account menu.
The shell tests' stub app, with GET /api/ui/view answered from /__stub/view (shell_view_tests.html)."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import Request

from tests.ui.browser import find_browser, run_app_test_page
from tests.ui.test_shell_browser import stub_app

BROWSER = find_browser()
PAGE_TIMEOUT = 120


def view_app():
    app = stub_app()
    state: dict[str, Any] = {"view": {}, "puts": []}

    @app.post("/__stub/view")
    async def set_view(request: Request):
        state["view"] = await request.json()
        return {}

    @app.get("/__stub/view")
    async def get_view():
        return state["view"]

    @app.get("/__stub/view-puts")
    async def puts():
        return state["puts"]

    @app.get("/api/ui/view")
    async def view():
        return state["view"]

    @app.put("/api/ui/view/mode")
    async def set_mode(request: Request):
        mode = (await request.json())["mode"]
        state["puts"].append(mode)
        state["view"] = {**state["view"], "mode": mode, "panels": ["help"] if mode == "simple" else None}
        return {"mode": mode}

    # ahead of the real UI router the stub app includes: these answer, not it
    stubbed = app.router.routes[-6:]
    del app.router.routes[-6:]
    app.router.routes[0:0] = stubbed
    return app


@pytest.fixture(scope="module")
def results():
    if BROWSER is None:
        pytest.skip("no Chromium-based browser installed")
    return run_app_test_page(BROWSER, view_app(), "tests/ui/shell_view_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    "a user gets the simple view: their panels, topics in the composer, no agent or model choice",
    "a loaded session keeps the words of the simple view in the composer",
    "a first message on its way counts as a session: another topic starts a new chat",
    "/agent in the simple view: the new chat runs on the agent named, the bar says so",
    "the start page shows their areas, and a tile opens the panel",
    "the palette lists no agents and only their panels",
    "the account menu tells a user who chose their view, and has no System",
    "an admin sees everything as before, and may switch to the simple view",
    "a switch to the simple view is told to the server",
]


@pytest.mark.timeout(PAGE_TIMEOUT + 60)
@pytest.mark.parametrize("name", EXPECTED)
def test_shell_view(results, name):
    assert results.get(name) == "ok", results


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
