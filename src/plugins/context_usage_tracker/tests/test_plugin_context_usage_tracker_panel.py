"""The Context & Cost Usage panel in a real browser, against the real plugin: its router, its tracker database, its
static files.

Seeded: session ``s-1`` with two billed calls of ``writer`` (model ``m-fast``, request ``r-1``) and an estimated call
of the sub-agent ``helper`` in ``s-1-sub`` (model ``m-cheap``, request ``r-1_sub_ab``); session ``s-2`` with a call of
``coder`` (``m-fast``, no cost, no latency); session ``s-3`` with 120 calls of ``bulk`` (more than the history's former default of 100); from a pre-SQLite usage file,
the all-time totals of ``archivist``, whose calls predate the cache-rate pair. POST /__stub/call records one more call
of ``writer`` in ``s-1``. With the cookie ``cu_usage=fails`` the usage answer fails, with ``cu_usage=slow`` both
answers take 1.5 s.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

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


def seed(tracker) -> None:
    tracker.record_usage("writer-1", "writer", "s-1", total_tokens=1200, prompt_tokens=1000, completion_tokens=200,
                         context_window=10000, cached_tokens=800, cost=0.002, model="m-fast", request_id="r-1",
                         latency_ms=800)
    tracker.record_usage("helper-1", "helper", "s-1-sub", total_tokens=500, prompt_tokens=400, completion_tokens=100,
                         context_window=10000, cost=0.0005, cost_is_estimate=True, model="m-cheap",
                         request_id="r-1_sub_ab", latency_ms=300)
    tracker.record_usage("writer-1", "writer", "s-1", total_tokens=1800, prompt_tokens=1500, completion_tokens=300,
                         context_window=10000, cost=0.003, model="m-fast", request_id="r-1", latency_ms=1200)
    tracker.record_usage("coder-1", "coder", "s-2", total_tokens=150, prompt_tokens=100, completion_tokens=50,
                         context_window=10000, model="m-fast", request_id="r-2")
    for i in range(120):
        tracker.record_usage("bulk-1", "bulk", "s-3", total_tokens=10, prompt_tokens=8, completion_tokens=2,
                             context_window=10000, model="m-bulk", request_id=f"r-bulk-{i}")


def panel_app(tmp_path: Path):
    from plugins.context_usage_tracker.plugin import ContextUsageTrackerPlugin

    legacy = tmp_path / "usage.json"  # imported once, when the store first opens
    legacy.write_text(json.dumps({"history": [], "agents": {"archivist-1": {
        "agent_name": "archivist", "total_calls": 5, "total_tokens": 5000, "total_prompt_tokens": 1000,
        "total_cached_tokens": 900}}}), encoding="utf-8")
    plugin = ContextUsageTrackerPlugin("context_usage_tracker", {}, {"storage_path": str(legacy)})
    seed(plugin.tracker)
    app = FastAPI()

    @app.middleware("http")
    async def failing_or_slow(request: Request, call_next):
        mode = request.cookies.get("cu_usage")
        if mode == "fails" and request.url.path.endswith("/usage"):
            return JSONResponse({"detail": "The usage store is locked"}, status_code=500)
        if mode == "slow" and request.url.path.endswith(("/usage", "/history")):
            await asyncio.sleep(1.5)
        return await call_next(request)

    @app.post("/__stub/call")
    async def add_call():
        plugin.tracker.record_usage("writer-1", "writer", "s-1", total_tokens=2000, prompt_tokens=1600,
                                    completion_tokens=400, context_window=10000, cost=0.004, model="m-fast",
                                    request_id="r-1b", latency_ms=900)
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("context_usage_tracker", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/context_usage_tracker", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    app = panel_app(tmp_path_factory.mktemp("usage_panel"))
    return run_app_test_page(BROWSER, app, "tests/context_usage_tracker/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'opened on a session, the panel counts it and its sub-agents: billed and estimated cost apart, cache share, context',
    'all sessions count every call, and the calls tab shows the newest of them',
    'the overview draws the newest 60 calls, tokens and cost, for the agent chosen',
    'agents sort by a column, one way and the other, and show their cache share: all-time from the paired counts, a session from its calls',
    'a failed load shows the error and nothing of what was shown before, and keeps the agent chosen',
    'a tick of the auto refresh leaves a load still on its way alone',
    'the LLMs tab adds up each model with its average and p95 latency',
    "calls filter by agent, mark a sub-agent's call and open in the drawer",
    'the auto refresh runs from the start and brings a new call',
    'with no session open the panel says so instead of counting every session',
    'clearing asks first and, confirmed, empties the panel',
    'the chart takes real colours from the theme and a theme change draws it in the new ones',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_context_usage_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
