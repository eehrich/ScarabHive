"""The shell's power button in a real browser: it asks for power off, reload config or restart.

The script is the real one (static/js/shell/server.js) on a small page of its own
(server_controls_probe.html: the button and the status item, as index.html has them); the server behind it
is a stub. Two requests are running. POST /admin/reload-config reports two servers changed, one failed and the auth section changed.
POST /admin/server/restart "restarts" it: for 1.5 s /health does not answer, then it answers as a new process
(uptime from zero). With the cookie ``stub_server=refuses`` stop and restart answer 409, with ``stub_server=instant`` the restarted server answers at once -- it is never seen
down. GET /__stub/asked lists the actions posted.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

UI_TESTS = Path(__file__).resolve().parent
TEMPLATES = UI_TESTS.parents[1] / "templates"


def stub_app() -> FastAPI:
    app = FastAPI()
    state = {"booted": time.time() - 3600, "down_until": 0.0, "asked": []}

    @app.get("/health")
    async def health():
        if time.time() < state["down_until"]:
            raise HTTPException(status_code=503, detail="restarting")
        return {"status": "ok", "version": "9.9.9", "uptime_seconds": round(time.time() - state["booted"], 2)}

    @app.get("/admin/active-sessions")
    async def active_sessions():
        return {"total": 2, "sessions": []}

    @app.post("/admin/server/{action}")
    async def end(action: str, request: Request):
        mode = request.cookies.get("stub_server")
        if mode == "refuses":
            raise HTTPException(status_code=409, detail="This server was not started by agent-api")
        state["asked"].append(action)
        if action == "restart":
            state["down_until"] = 0.0 if mode == "instant" else time.time() + 1.5
            state["booted"] = max(state["down_until"], time.time())
        return {"status": "stopping" if action == "stop" else "restarting", "running_requests": 2}

    @app.post("/admin/reload-config")
    async def reload_config():
        state["asked"].append("reload")
        return {"status": "ok", "report": {"refreshed": [{"server": "a", "changes": {}}, {"server": "b", "changes": {}}],
                                           "errors": [{"server": "broken_server", "error": "boom"}],
                                           "auth": "changed on disk: takes effect on a restart"}}

    @app.get("/__stub/asked")
    async def asked():
        return state["asked"]

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/ui", StaticFiles(directory=UI_TESTS), name="ui-tests")
    return app


@pytest.fixture(scope="module")
def results():
    return run_app_test_page(BROWSER, stub_app(), "tests/ui/server_controls_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'the button is there for an admin only',
    'the power button asks which action, names the running requests and starts on Cancel: cancelled, nothing is asked of the server',
    'a double click opens one question',
    'a refusal of the server is shown and the button works again',
    'restarting waits for the server to go and come back, then loads the page anew',
    'a server back before it was seen down counts as restarted by its uptime',
    'reload config asks the server, says what changed, what failed and what waits for a restart, and leaves the page as it is',
    'from the palette an action asks on its own first: cancelled, nothing is asked of the server',
    'from the palette power off and restart ask on their own too and name the running requests',
    'power off asks the server and leaves the button off',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_server_controls(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)


def test_the_shell_page_has_the_power_button_hidden_until_the_script_shows_it():
    page = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    tag = next(line for line in page.splitlines() if 'id="serverButton"' in line)
    assert " hidden>" in tag and 'type="button"' in tag and "icon('power')" in tag, tag
