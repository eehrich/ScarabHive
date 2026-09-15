"""The System panel in a real browser: the status says why, and only to administrators.

The page is the real panel template and script; the server behind it is a
stub. The cookie ``stub_role`` picks what /admin/system answers: ``admin`` --
a status with a server that did not start and an agent config error; ``user``
-- 403; ``noauth`` -- 404, as a server without authentication has no admin routes.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from agent_system.ui.resources import STATIC_DIR
from agent_system.ui.routes import router
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

UI_TESTS = Path(__file__).resolve().parent

STATUS = {
    "status": "error",
    "checks": [
        {"name": "servers", "level": "error", "detail": "1 configured server(s) did not start: server 'x': boom"},
        {"name": "llm", "level": "ok", "detail": "no model is paused"},
        {"name": "deploy", "level": "warn", "detail": "code on disk is bbbbbbbb (later), this process runs aaaaaaaa"},
    ],
    "build": {"commit": {"hash": "a" * 40, "date": "2026-09-15T10:00:00+02:00", "subject": "running"},
              "commit_on_disk": None, "started_at": 1757923200, "uptime_seconds": 3700, "python": "3.12.5"},
    "process": {"pid": 42, "memory_mb": 512.5, "threads": 12, "async_tasks": 30},
    "servers": {"declared": 4, "running": 3, "agents": 2, "problems": ["server 'x': failed to start: boom"],
                "config_findings": ["agent 'lazy_one': LLM config does not resolve"]},
    "hooks": {"registered": 10, "enabled": 6},
    "llm_blocked": [],
}


def stub_app() -> FastAPI:
    app = FastAPI()

    @app.get("/admin/system")
    async def system(request: Request):
        if request.cookies.get("stub_role") == "noauth":
            raise HTTPException(status_code=404)
        if request.cookies.get("stub_role") != "admin":
            raise HTTPException(status_code=403, detail="Administrators only")
        return STATUS

    @app.get("/admin/active-sessions")
    async def active_sessions():
        return {"total": 2, "sessions": []}

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "9.9.9", "uptime_seconds": 60}

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/ui", StaticFiles(directory=UI_TESTS), name="ui-tests")
    return app


@pytest.fixture(scope="module")
def results():
    return run_app_test_page(BROWSER, stub_app(), "tests/ui/system_panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'an administrator sees the status with the reason behind each check',
    'an administrator sees the servers that did not start and the counts',
    'anyone else sees that the server answers, not a status without reasons',
    'without authentication the overview still shows that the server answers',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_system_panel(results, name):
    assert results.get(name) == "ok", results
