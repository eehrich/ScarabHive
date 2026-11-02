"""The shell in a real browser: a user is offered the released panels in the launcher and the palette, an admin every
panel; the chat stays as it is for both. The shell tests' stub app, with GET /api/ui/view answered from /__stub/view
(shell_view_tests.html)."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException, Request

from tests.ui.browser import find_browser, run_app_test_page
from tests.ui.test_shell_browser import stub_app

BROWSER = find_browser()
PAGE_TIMEOUT = 120


def view_app():
    app = stub_app()
    state: dict[str, Any] = {"view": {}, "me": None}

    @app.post("/__stub/view")
    async def set_view(request: Request):
        state["view"] = await request.json()
        return {}

    @app.get("/__stub/view")
    async def get_view():
        return state["view"]

    @app.post("/__stub/me")
    async def set_me(request: Request):
        state["me"] = await request.json()
        return {}

    @app.get("/auth/me")
    async def me():
        if state["me"] is None:
            raise HTTPException(status_code=404)  # authentication off
        return state["me"]

    @app.get("/api/ui/view")
    async def view():
        return state["view"]

    # ahead of the stub app's own view route and the real UI router: these answer, not they
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
    "when the server cannot say, an admin is offered every panel and anyone else what their role opens",
    "the released panels come in the order chosen, without what the catalogue lacks",
    "a user is offered only the released panels in the launcher",
    "the chat stays as it is: the agent, model and thinking choice, the welcome, no topics",
    "the palette lists only the released panels, and the agents as before",
    "the account menu tells a user who released their panels",
    "an admin is offered every panel and opens the User view from the account menu",
    "an admin who has not switched keeps the server buttons, the palette's actions and System",
    "a user gets no server buttons, none of the palette's actions and no System",
]


@pytest.mark.timeout(PAGE_TIMEOUT + 60)
@pytest.mark.parametrize("name", EXPECTED)
def test_shell_view(results, name):
    assert results.get(name) == "ok", results


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
