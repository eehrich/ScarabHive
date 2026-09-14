"""The Logs panel in a real browser, against the real plugin: its router, its static files, log files in tmp_path.

Configured (relative to the working directory, a tmp dir while the page runs): ``logs/app.log``, ``logs/other.log``,
``logs/empty.log`` and ``logs/missing.log``, which does not exist. ``logs/app.log.1`` holds 300 INFO entries "Filler
0" to "Filler 299" (09:00:00 on); ``logs/app.log`` a DEBUG entry, one without a level, the panel's own request (never
shown), an INFO entry with markup in it, a WARNING, an ERROR with a three-line traceback and a CRITICAL (10:00:00 to
10:00:05). ``logs/other.log`` holds one INFO entry "Other started", ``logs/empty.log`` nothing. Behind the panel's
back: POST /__stub/append adds an INFO entry "Appended <n>" to app.log, /__stub/hide moves the log directory away,
/__stub/unhide brings it back, /__stub/create makes ``logs/missing.log`` exist, /__stub/uncreate removes it. With the cookie ``lv=listfails`` the file list fails, ``lv=listslow`` it takes 1.5 s,
``lv=fails`` the entries fail, ``lv=slow`` they take 1.5 s, ``lv=slower`` 3 s, ``lv=slowfail`` they fail after 1.5 s.
GET /__stub/asked counts the entry loads asked for and names the query of the last.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.config.models import AgentConfig, AgentSystemConfig, MCPConfig
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 150
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
LIST = "/plugins/log_viewer/logs/list"
CONTENT = "/plugins/log_viewer/logs/content/"

APP_LOG = """\
2026-01-01 10:00:00,000 DEBUG agent_system.api Loaded 12 plugins
2026-01-01 10:00:00,500 Started without a level
2026-01-01 10:00:01,000 INFO uvicorn.access GET /plugins/log_viewer/logs/list 200
2026-01-01 10:00:02,000 INFO agent_system.api <img src=x onerror="window.parent.injected=1"> & <b>bold</b>
2026-01-01 10:00:03,000 WARNING agent_system.llm Retrying request
2026-01-01 10:00:04,000 ERROR agent_system.core Tool failed
Traceback (most recent call last):
  File "core.py", line 7, in run
ValueError: Bad Budget
2026-01-01 10:00:05,000 CRITICAL agent_system.app Out of memory
"""


def seed(root: Path) -> None:
    logs = root / "logs"
    logs.mkdir()
    filler = "".join(f"2026-01-01 09:{n // 60:02d}:{n % 60:02d},000 INFO agent_system.seed Filler {n}\n" for n in range(300))
    (logs / "app.log.1").write_bytes(filler.encode())
    (logs / "app.log").write_bytes(APP_LOG.encode())
    (logs / "other.log").write_bytes(b"2026-01-02 08:00:00,000 INFO agent_system.other Other started\n")
    (logs / "empty.log").write_bytes(b"")


def panel_app(root: Path):
    """The app; the log names are relative, so the page runs with ``root`` as the working directory (see run_page)."""
    from plugins.log_viewer.plugin import PLUGIN_FACTORY

    seed(root)
    config = MCPConfig(type="log_viewer", enabled=True, agent_config=AgentConfig())
    config.log_files = ["logs/app.log", "logs/other.log", "logs/empty.log", "logs/missing.log"]
    plugin = PLUGIN_FACTORY("log_viewer", AgentSystemConfig(), config)
    app = FastAPI()
    asked = {"content": 0, "last": ""}
    appended = {"count": 0}

    @app.middleware("http")
    async def modes(request: Request, call_next):
        mode = request.cookies.get("lv")
        path = request.url.path
        if path.startswith(CONTENT):
            asked["content"] += 1
            asked["last"] = f"{path[len(CONTENT):]}?{request.url.query}"
        if (mode == "listfails" and path == LIST) or (mode == "fails" and path.startswith(CONTENT)):
            return JSONResponse({"detail": "The log directory is not readable"}, status_code=500)
        if (mode in ("slow", "slower", "slowfail") and path.startswith(CONTENT)) or (mode == "listslow" and path == LIST):
            # headers at once, body held, not cacheable: the browser's cache lock would otherwise hold back the
            # next request for the same URL until this answer is complete
            if mode == "slowfail":
                status, payload = 500, b'{"detail": "The log directory is not readable"}'
            else:
                answer = await call_next(request)
                status, payload = answer.status_code, b"".join([chunk async for chunk in answer.body_iterator])

            async def held():
                await asyncio.sleep(3 if mode == "slower" else 1.5)
                yield payload
            return StreamingResponse(held(), status_code=status, media_type="application/json",
                                     headers={"Cache-Control": "no-store"})
        return await call_next(request)

    @app.get("/__stub/asked")
    async def entries_asked():
        return asked

    @app.post("/__stub/append")
    async def append():
        appended["count"] += 1
        with open(root / "logs" / "app.log", "ab") as log:
            log.write(f"2026-01-01 10:00:06,000 INFO agent_system.api Appended {appended['count']}\n".encode())
        return appended

    @app.post("/__stub/create")
    async def create():
        (root / "logs" / "missing.log").write_bytes(b"")
        return {}

    @app.post("/__stub/uncreate")
    async def uncreate():
        (root / "logs" / "missing.log").unlink(missing_ok=True)
        return {}

    @app.post("/__stub/hide")
    async def hide():
        (root / "logs").rename(root / "hidden")
        return {}

    @app.post("/__stub/unhide")
    async def unhide():
        if (root / "hidden").exists():
            (root / "hidden").rename(root / "logs")
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("log_viewer", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/log_viewer", StaticFiles(directory=TESTS), name="panel-tests")
    return app


def run_page(root: Path, app: FastAPI | None = None, timeout: float = PAGE_TIMEOUT) -> dict:
    app = app or panel_app(root)
    before = os.getcwd()
    os.chdir(root)
    try:
        return run_app_test_page(BROWSER, app, "tests/log_viewer/panel_tests.html", timeout=timeout)
    finally:
        os.chdir(before)


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    return run_page(tmp_path_factory.mktemp("log_viewer_panel"))


EXPECTED = [
    'the panel lists the existing files and shows the newest entries of the first with time, level and message',
    'a traceback folds under its first line',
    'log text is shown as text, never as markup',
    'the levels narrow the entries, error takes critical along, none chosen asks nothing',
    'the search finds text in tracebacks and says when nothing matches',
    'the count chooses how many of the newest entries are shown',
    'another file shows its entries, an empty one says so, a file appearing leaves the choice',
    'follow keeps the newest entry in view; off, a refresh keeps the place and a new choice starts at the newest',
    'without an existing log file the panel says so',
    'a failed load shows the error and nothing of before, also when only the file list fails',
    'an answer overtaken by a later load is dropped',
    'ticks of the auto refresh ask nothing more while a load is on its way',
    'the auto refresh is off at the start and, switched on, brings new entries',
    'the settings survive a reload',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_log_viewer_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
