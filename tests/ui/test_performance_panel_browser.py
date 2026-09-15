"""The Performance panel in a real browser, against the real UI and debug routers and the real report.

Seeded at the edge of utils.profiling: the request profiler holds real requests, the
clock it measures them with is a stand-in (fixed until /__stub/advance), and the parts
that read the live process -- threads, memory -- answer fixed values, some with markup in
them. The loop monitor holds a lag sample of 120 ms. As on a real server, each report counts
itself under /debug/profile, and the loop hands out its 31 tasks in a new order each time, one
of them the report's own request task under a new name.

Authentication is on; the cookie ``stub_role`` names the viewer (``admin``, ``user``,
none: not signed in). Behind the panel's back: POST /__stub/reset seeds everything anew,
/__stub/advance moves the clock by a second, /__stub/off and /__stub/on switch profiling,
/__stub/drift?on=1 makes every report find the clock 0.1 s and the lag 10 ms further (/__stub/drift
stops it), /__stub/traffic finishes a request on /tools/{name} and starts another one.
GET /__stub/asked counts the reports and the POSTs asked for. With the cookie
``pp_report=fails`` the report fails, ``slow`` takes 1.5 s, ``slower`` 3 s, ``slowfail``
fails after 1.5 s; ``pp_post=slow`` holds a POST for 1.5 s.
"""
from __future__ import annotations

import asyncio
import itertools
import random
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from agent_system.api import debug_endpoints
from agent_system.config.models import AuthConfig
from agent_system.ui.resources import STATIC_DIR
from agent_system.ui.routes import router as ui_router
from agent_system.utils import profiling
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 150
browser_only = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
                pytest.mark.timeout(PAGE_TIMEOUT + 60)]

UI_TESTS = Path(__file__).resolve().parent
XSS = '<img src=x onerror="window.parent.__xss=1">'


class Clock:
    def __init__(self):
        self.now = 100.0

    def perf_counter(self):
        return self.now


def seed(clock: Clock) -> None:
    clock.now = 100.0
    profiler = profiling.RequestProfiler()
    profiling._request_profiler = profiler
    for rid, path, took in [("a1", "/api/sessions/{session_id}", 0.2), ("a2", "/api/sessions/{session_id}", 0.4),
                            ("s1", "/agents/{name}/run", 2.5)]:
        profiler.start_request(rid, path.replace("{session_id}", rid).replace("{name}", "writer"), "POST")
        clock.now += took
        profiler.end_request(rid, 200, stats_path=path)
    profiler.start_request("live", f"/api/run/{XSS}", "GET")
    profiler.start_request("own", "/debug/profile", "GET")  # another admin's panel asking: not listed
    clock.now += 2.5  # the live request runs for 2.5 s when asked
    loop = profiling.EventLoopMonitor()
    loop._lag_samples = [20.0, 120.0]
    profiling._loop_monitor = loop


def panel_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    import agent_system.auth.database as database
    import agent_system.auth.dependencies as dependencies

    clock = Clock()
    monkeypatch.setattr(profiling, "time", SimpleNamespace(perf_counter=clock.perf_counter))
    monkeypatch.setattr(profiling, "PROFILING_ENABLED", True)
    def task(name, coro):
        return SimpleNamespace(get_name=lambda: name, get_coro=lambda: SimpleNamespace(__qualname__=coro))

    fixed = [task(XSS, "<b>coro</b>")] + [task(f"Server-{i}", "Server.serve") for i in range(29)]
    request_tasks = itertools.count(100)

    def all_tasks():  # a set on a real loop: no order to rely on; uvicorn starts a task per request
        tasks = [*fixed, task(f"Task-{next(request_tasks)}", "RequestResponseCycle.run_asgi")]
        random.shuffle(tasks)
        return tasks
    monkeypatch.setattr(profiling, "asyncio", SimpleNamespace(all_tasks=all_tasks))
    monkeypatch.setattr(profiling, "_get_thread_details", lambda: {
        "count": 3, "active_count": 1, "idle_count": 2, "threads": [
            {"name": "MainThread", "ident": 1, "daemon": False, "alive": True, "idle": False, "idle_reason": None},
            {"name": XSS, "ident": 2, "daemon": True, "alive": True, "idle": True, "idle_reason": "threading.py:wait"},
            {"name": "asyncio_0", "ident": 3, "daemon": True, "alive": True, "idle": True, "idle_reason": "thread.py:_worker"}]})
    monkeypatch.setattr(profiling.MemoryMonitor, "get_memory_details", staticmethod(lambda: {"rss_mb": 512.4}))
    monkeypatch.setattr(profiling.MemoryMonitor, "get_memory_mb", staticmethod(lambda: 512.4))

    async def current_viewer(request, credentials, api_key, db):
        role = request.cookies.get("stub_role")
        return SimpleNamespace(role=SimpleNamespace(value=role), is_active=True) if role else None
    monkeypatch.setattr(dependencies, "get_optional_user", current_viewer)
    monkeypatch.setattr(database, "get_db", lambda: None)
    seed(clock)

    app = FastAPI()
    app.state.config = SimpleNamespace(auth=AuthConfig(enabled=True))
    asked = {"reports": 0, "posts": 0}
    drift = {"on": False}

    @app.middleware("http")
    async def stand_ins(request: Request, call_next):
        path = request.url.path
        mode = request.cookies.get("pp_report")
        if path == "/debug/profile" and request.method == "GET":
            asked["reports"] += 1
            profiler = profiling.get_profiler()  # counted like ProfilingMiddleware counts it: once answered
            profiler.start_request(f"report-{asked['reports']}", path, "GET")
            if drift["on"]:  # what a live server shows: every answer a little later, the loop a little busier
                clock.now += 0.1
                lag = profiling.get_loop_monitor()._lag_samples
                lag.append(lag[-1] + 10)
            if mode == "fails":
                return JSONResponse({"detail": "The profiler lock timed out"}, status_code=500)
        if path.startswith("/debug/profile/") and request.method == "POST":
            asked["posts"] += 1
            if request.cookies.get("pp_post") == "slow":
                await asyncio.sleep(1.5)
        if path == "/debug/profile" and request.method == "GET" and mode is None:
            answer = await call_next(request)
            profiler.end_request(f"report-{asked['reports']}", answer.status_code, stats_path="/debug/profile")
            return answer
        if not (path == "/debug/profile" and mode in ("slow", "slower", "slowfail")):
            return await call_next(request)
        # headers at once, body held, not cacheable: the browser's cache lock would otherwise hold back the next
        # request for the same URL until this answer is complete
        if mode == "slowfail":
            status, payload = 500, b'{"detail": "The profiler lock timed out"}'
        else:
            answer = await call_next(request)
            status, payload = answer.status_code, b"".join([chunk async for chunk in answer.body_iterator])

        async def body():
            await asyncio.sleep(3 if mode == "slower" else 1.5)
            yield payload
        return StreamingResponse(body(), status_code=status, media_type="application/json",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/__stub/asked")
    async def stub_asked():
        return asked

    @app.post("/__stub/reset")
    async def stub_reset():
        seed(clock)
        drift["on"] = False
        return {}

    @app.post("/__stub/advance")
    async def stub_advance():
        clock.now += 1
        return {}

    @app.post("/__stub/drift")
    async def stub_drift(on: bool = False):
        drift["on"] = on
        return {}

    @app.post("/__stub/traffic")
    async def stub_traffic():
        profiler = profiling.get_profiler()
        profiler.start_request("t1", "/tools/grep", "POST")
        profiler.end_request("t1", 200, stats_path="/tools/{name}")
        profiler.start_request("live2", "/api/run/second", "GET")
        return {}

    @app.post("/__stub/{switch}")
    async def stub_switch(switch: str):
        profiling.PROFILING_ENABLED = switch == "on"
        return {}

    app.include_router(ui_router)
    app.include_router(debug_endpoints.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/ui", StaticFiles(directory=UI_TESTS), name="ui-tests")
    return app


# ------------------------------------------------------------------ the server alone

@pytest.fixture
def client(monkeypatch):
    return TestClient(panel_app(monkeypatch))


def as_role(client: TestClient, role: str | None) -> TestClient:
    client.cookies.clear()
    if role:
        client.cookies.set("stub_role", role)
    return client


@pytest.mark.parametrize("method, path", [
    ("GET", "/ui/panels/performance"), ("GET", "/debug/profile"),
    ("POST", "/debug/profile/gc"), ("POST", "/debug/profile/reset"),
])
def test_page_and_data_are_for_administrators_only(client, method, path):
    answers = {role: as_role(client, role).request(method, path).status_code for role in (None, "user", "admin")}

    assert answers == {None: 401, "user": 403, "admin": 200}


def test_a_refused_viewer_changes_nothing(client):
    before = profiling.get_profiler().get_stats()

    assert as_role(client, "user").post("/debug/profile/reset").status_code == 403
    assert profiling.get_profiler().get_stats() == before and before


@pytest.mark.parametrize("method, path", [
    ("GET", "/ui/panels/performance"), ("GET", "/debug/profile"),
    ("POST", "/debug/profile/gc"), ("POST", "/debug/profile/reset"),
])
def test_with_profiling_off_nothing_of_it_exists(client, method, path):
    client.post("/__stub/off")

    assert as_role(client, "admin").request(method, path).status_code == 404
    assert as_role(client, "user").request(method, path).status_code == 403  # who may not ask learns nothing


def test_reading_never_changes_state_and_changing_takes_a_post(client):
    admin = as_role(client, "admin")

    def stats():  # without the row the report counts itself under
        return {path: row for path, row in admin.get("/debug/profile").json()["requests"]["stats_by_path"].items()
                if path != "/debug/profile"}
    before = stats()

    assert admin.get("/debug/profile/reset").status_code == 405
    assert admin.get("/debug/profile/gc").status_code == 405
    assert stats() == before != {}
    assert admin.post("/debug/profile/reset").status_code == 200
    assert stats() == {}


@pytest.mark.parametrize("path", ["/debug/profile/dashboard", "/debug/profile/requests", "/debug/profile/tasks",
                                  "/debug/profile/loop", "/debug/profile/memory"])
def test_the_old_dashboard_and_the_endpoints_only_it_needed_are_gone(client, path):
    assert as_role(client, "admin").get(path).status_code == 404


# ------------------------------------------------------------------ the browser

@pytest.fixture(scope="module")
def results():
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield run_app_test_page(BROWSER, panel_app(monkeypatch), "tests/ui/performance_panel_tests.html",
                                timeout=PAGE_TIMEOUT)


EXPECTED = [
    'the page is refused to anyone but an administrator',
    'the stats count running requests, tasks, lag, memory and threads',
    'running and slowest requests, time per route, tasks and threads are listed',
    'paths, task and thread names are drawn as text, never as markup',
    'what changes on every answer is updated in place, a card is drawn anew only when what it lists changed',
    'resetting asks first: declined nothing is sent, confirmed the stats are gone',
    'collecting garbage asks first and says what it collected',
    'a double click asks once, and the button stays disabled until drawn anew',
    'the keyboard focus returns to the button when the dialog closes and stays across redraws',
    'a viewer who is no longer an administrator, or signed out, sees why and nothing of before',
    'with profiling switched off the panel says so',
    'a failed load shows the error and nothing of before',
    'an answer overtaken by a later load is dropped',
    'ticks of the auto refresh ask nothing more while a load is on its way',
    'the auto refresh runs from the start and brings what changed',
]


@pytest.mark.parametrize("name", EXPECTED)
@browser_only[0]
@browser_only[1]
def test_performance_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


@browser_only[0]
@browser_only[1]
def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
