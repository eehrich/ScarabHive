"""The Memory Profile panel in a real browser, against the real routes: the core panel route, the /debug/memory
router with its admin guard and validation, and the profiler module. Only what the profiler reads from the
interpreter and the OS is a stand-in -- the heap walk, the process memory, tracemalloc and the gc counters, the last
two moving on every reading as on a live server -- plus
the user lookup behind the session cookie.

Seeded on reset: a baseline, then a snapshot, then the heap grows (dict +1000, agent_system.core.Session +500) and a
second snapshot is taken. The heap holds a type whose name is markup; with tracemalloc on, the allocation sites
include one whose file name is markup.

Behind the panel's back: POST /__stub/reset seeds everything anew, /__stub/grow?type=&by= grows the heap,
/__stub/snapshot takes a snapshot as the periodic task would, /__stub/flag?on=false turns memory profiling off, /__stub/trace starts
tracemalloc. GET /__stub/asked counts the summaries asked for, the heap walks, the snapshots and tracemalloc starts,
and says whether tracemalloc runs with how many frames.
A collection frees 4 MB and reports 1234 objects.
Cookies: ``mp_role`` = admin | user (none: not signed in); ``mp_summary`` = fails | slow (1.5 s) | slower (3 s) |
slowfail (fails after 1.5 s); ``mp_action=slow`` holds every POST for 1.5 s before it reaches the route.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 180
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

UI_TESTS = Path(__file__).resolve().parent
MB = 1024 * 1024
HEAP = {"dict": 50000, "function": 20000, "agent_system.core.Session": 300,
        '<img src=x onerror="window.parent.__xss=1">': 7}
ALLOCATIONS = [{"file": "src/agent_system/core/session.py:120", "size_kb": 2048.0, "count": 900},
               {"file": "<b>generated</b>:1", "size_kb": 12.5, "count": 3}]


class Process:
    """The interpreter and the OS as the profiler reads them, in memory."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.heap = dict(HEAP)
        self.rss = 512.0
        self.tracing = False
        self.nframes = None
        self.collections = 0
        self.asked = {"summary": 0, "walks": 0, "snapshots": 0, "starts": 0, "gcs": 0}

    def count_objects(self):
        self.asked["walks"] += 1
        return dict(self.heap)

    def process_memory(self):
        self.rss += 0.001  # like a real process: never the same twice, still 512 MB when rounded
        return {"rss_mb": self.rss, "vms_mb": 2048.0, "percent": 3.2}

    def gc_count(self):
        self.collections += 1  # generation 0 moves on every reading, as on a live server
        return (self.collections, 1, 0)

    def top_allocations(self, limit=20):
        return [dict(a) for a in ALLOCATIONS] if self.tracing else []

    def collect(self):
        self.asked["gcs"] += 1
        self.rss -= 4  # what the collection gave back
        return 1234

    def start(self, nframes):
        self.tracing, self.nframes = True, nframes

    def stop(self):
        self.tracing, self.nframes = False, None


def panel_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from agent_system.api import debug_endpoints
    from agent_system.api.debug_endpoints import router as debug_router
    from agent_system.auth import database, dependencies
    from agent_system.auth.models import UserRole
    from agent_system.ui.routes import router as ui_router
    from agent_system.utils import memory_profiling

    proc = Process()
    monkeypatch.setattr(memory_profiling, "count_objects", proc.count_objects)
    monkeypatch.setattr(memory_profiling, "process_memory", proc.process_memory)
    monkeypatch.setattr(memory_profiling, "top_allocations", proc.top_allocations)
    monkeypatch.setattr(memory_profiling, "tracemalloc", SimpleNamespace(
        is_tracing=lambda: proc.tracing, start=proc.start, stop=proc.stop,
        get_traced_memory=lambda: (3 * MB, 5 * MB)))
    monkeypatch.setattr(memory_profiling, "gc", SimpleNamespace(
        get_count=proc.gc_count, get_threshold=lambda: (700, 10, 10), garbage=[]))
    monkeypatch.setattr(memory_profiling, "MEMORY_PROFILING_ENABLED", True)
    monkeypatch.setattr(debug_endpoints, "gc", SimpleNamespace(collect=proc.collect))

    roles = {"admin": UserRole.ADMIN, "user": UserRole.USER}

    async def signed_in(request, credentials, api_key, db):
        role = roles.get(request.cookies.get("mp_role"))
        return SimpleNamespace(role=role, is_active=True) if role else None

    monkeypatch.setattr(dependencies, "get_optional_user", signed_in)
    monkeypatch.setattr(database, "get_db", lambda: None)

    def seed():
        proc.reset()
        detector = memory_profiling.MemoryLeakDetector()
        monkeypatch.setattr(memory_profiling, "_leak_detector", detector)
        monkeypatch.setattr(memory_profiling, "MEMORY_PROFILING_ENABLED", True)
        detector._object_tracker.set_baseline()
        detector.take_snapshot()
        proc.heap["dict"] += 1000
        proc.heap["agent_system.core.Session"] += 500
        detector.take_snapshot()
        proc.asked["walks"] = 0

    seed()
    app = FastAPI()
    app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=True))

    @app.middleware("http")
    async def stand_ins(request: Request, call_next):
        path = request.url.path
        summary = path == "/debug/memory" and request.method == "GET"
        mode = request.cookies.get("mp_summary") if summary else None
        if summary:
            proc.asked["summary"] += 1
        if path == "/debug/memory/snapshot":
            proc.asked["snapshots"] += 1
        if path == "/debug/memory/tracemalloc/start":
            proc.asked["starts"] += 1
        if request.method == "POST" and path.startswith("/debug/") and request.cookies.get("mp_action") == "slow":
            await asyncio.sleep(1.5)
        if mode == "fails":
            return JSONResponse({"detail": "The profiler is stuck"}, status_code=500)
        if mode not in ("slow", "slower", "slowfail"):
            return await call_next(request)
        # headers at once, body held, not cacheable: the browser's cache lock would otherwise hold back the next
        # request for the same URL until this answer is complete
        if mode == "slowfail":
            status, payload = 500, b'{"detail": "The profiler is stuck"}'
        else:
            answer = await call_next(request)
            status, payload = answer.status_code, b"".join([chunk async for chunk in answer.body_iterator])

        async def body():
            await asyncio.sleep(3 if mode == "slower" else 1.5)
            yield payload
        return StreamingResponse(body(), status_code=status, media_type="application/json",
                                 headers={"Cache-Control": "no-store"})

    @app.post("/__stub/reset")
    async def stub_reset():
        seed()
        return {}

    @app.post("/__stub/grow")
    async def stub_grow(type: str, by: int):
        proc.heap[type] = proc.heap.get(type, 0) + by
        return {}

    @app.post("/__stub/snapshot")
    async def stub_snapshot():
        memory_profiling.get_leak_detector().take_snapshot()  # the periodic task's, not the panel's
        return {}

    @app.post("/__stub/flag")
    async def stub_flag(on: bool):
        monkeypatch.setattr(memory_profiling, "MEMORY_PROFILING_ENABLED", on)
        return {}

    @app.post("/__stub/trace")
    async def stub_trace():
        proc.start(10)
        return {}

    @app.get("/__stub/asked")
    async def stub_asked():
        return {**proc.asked, "tracing": proc.tracing, "nframes": proc.nframes}

    app.include_router(ui_router)
    app.include_router(debug_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/ui", StaticFiles(directory=UI_TESTS), name="ui-tests")
    return app


@pytest.fixture(scope="module")
def results():
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield run_app_test_page(BROWSER, panel_app(monkeypatch), "tests/ui/memory_profile_panel_tests.html",
                                timeout=PAGE_TIMEOUT)


EXPECTED = [
    'an administrator sees the process memory, the latest snapshot, the growth since the baseline, the trend and the collector',
    'type names and allocation sites are drawn as text, never as markup',
    'loading and the auto refresh walk no heap and store nothing',
    'a snapshot is taken on a click, walks the heap once and is drawn',
    'collecting garbage asks first, is sent once on a double click, and the figures are read anew',
    'setting the baseline asks first: declined nothing happens, confirmed it is replaced',
    'tracemalloc starts and stops on a confirmed click; a snapshot asked for records the allocations, which outlast later periodic snapshots',
    'the server refuses the page and every memory route to anyone but an administrator',
    'with memory profiling off the page and its data are not found, and a running trace can still be stopped',
    'starting tracemalloc takes only a frame count from 1 to 100',
    'a double click acts once, and the button stays disabled until the answer is drawn',
    'the keyboard focus stays on its button when the cards are drawn anew and when a question closes',
    'the cards stay as drawn while only the moving figures change',
    'a failed load shows the error and nothing of before',
    'an answer overtaken by a later load is dropped',
    'ticks of the auto refresh ask nothing more while a load is on its way',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_memory_profile_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
