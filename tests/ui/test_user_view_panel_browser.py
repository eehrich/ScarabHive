"""The User view panel in a real browser, against the real UI router: an admin releases the panels people who are not
admins are offered. Authentication is off (the owner is an admin)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from agent_system.ui.resources import STATIC_DIR
from agent_system.ui.routes import router as ui_router
from agent_system.ui.views import ViewStore
from tests.ui.browser import find_browser, run_app_test_page
from tests.ui.test_catalog import auth

BROWSER = find_browser()
PAGE_TIMEOUT = 90
UI_TESTS = Path(__file__).resolve().parent


def stub_app(views: Path) -> FastAPI:
    app = FastAPI()
    app.state.config = auth(False)
    app.state.ui_views = ViewStore(views)

    app.include_router(ui_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/ui", StaticFiles(directory=UI_TESTS), name="ui-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    if BROWSER is None:
        pytest.skip("no Chromium-based browser installed")
    app = stub_app(tmp_path_factory.mktemp("views") / "views.json")
    return run_app_test_page(BROWSER, app, "tests/ui/user_view_panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    "it lists the panels a user may open, the default ones on, and says nobody has chosen yet",
    "a switch changes the preview and wakes the save button",
    "saving keeps it on the server, and the panel opened again shows it",
    "panels only admins may open are not offered",
    "a refused save says why and keeps the choice to correct",
]


@pytest.mark.timeout(PAGE_TIMEOUT + 60)
@pytest.mark.parametrize("name", EXPECTED)
def test_user_view_panel(results, name):
    assert results.get(name) == "ok", results


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
