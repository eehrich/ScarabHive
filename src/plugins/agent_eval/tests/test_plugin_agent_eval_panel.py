"""The Evals panel in a real browser, against the real plugin router and seeded runs.

Seeded in a data directory of its own:

- ``20261020-100000-aaaa`` -- suite ``coder-basics``, arm ``base``, the suite's baseline;
- ``20261021-100000-bbbb`` -- the same suite, arms ``base`` and ``terse``; ``terse`` passes every task ``base``
  fails half the time, ``fix-pager`` with check output and answer;
- ``20261022-100000-cccc`` -- meta says running, its process is long gone.

With the cookie ``ev_mode=fails`` the run list answers 500. POST /plugins/agent_eval/runs is answered by the stub
(never a real run); GET /__stub/started says how many it took; POST /__stub/append adds a 25th run to the two-arm
run; POST /__stub/stop_one writes a stopped and a refused record (neither counts) into the died run.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agent_system.paths import DATA_DIR_ENV
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
PLUGINS = TESTS.parents[1]
TASKS = ["fix-pager", "add-cli-flag", "rename-module", "parse-dates", "split-config", "cache-lookup"]


def _seed() -> None:
    from plugins.agent_eval import store

    def run(run_id, arms, status="done", pid=None):
        store.run_dir(run_id).mkdir(parents=True)
        store.write_meta(run_id, {"run_id": run_id, "suite": "coder-basics", "suite_path": "evals/coder/suite.yaml",
                                  "agent": "coder", "arms": arms, "reference_arm": arms[0], "tasks": len(TASKS),
                                  "repeats": 2, "status": status, "started": "2026-10-21T10:00:00+00:00",
                                  "pid": pid or os.getpid()})
        n = 0
        for rep in (1, 2):
            for task in TASKS:
                for arm in arms:
                    n += 1
                    passed = arm == "terse" or rep == 1
                    store.append_record(run_id, {
                        "run_id": run_id, "n": n, "arm": arm, "task": task, "kind": "learn", "rep": rep,
                        "dir": f"runs/{n:04d}-{arm}-r{rep}", "scored": True, "status": "done",
                        "score": 1.0 if passed else 0.4, "passed": passed,
                        "detail": f"{5 if passed else 2}/5 passed", "answer": f"Changed {task}: all tests green.",
                        "cost": 0.012 if arm == "base" else 0.009, "duration_s": 41.5, "model": "~vendor/model-a",
                        "request_id": f"ev-{run_id[-4:]}-{n}-{task}", "errors": []})

    run("20261020-100000-aaaa", ["base"])
    store.set_baseline("coder-basics", "20261020-100000-aaaa")
    run("20261021-100000-bbbb", ["base", "terse"])
    run("20261022-100000-cccc", ["base"], status="running", pid=2 ** 22 + 7)


def panel_app(tmp_path: Path) -> FastAPI:
    """The panel's app: the real router and static files, the run list in *tmp_path*. Sets the process's data
    directory -- the screenshot script runs this in a process of its own; the test restores it."""
    os.environ[DATA_DIR_ENV] = str(tmp_path / "data")
    _seed()
    from plugins.agent_eval.plugin import PLUGIN_FACTORY

    config = SimpleNamespace(plugins=SimpleNamespace(plugin_dirs=[str(PLUGINS)]))
    plugin = PLUGIN_FACTORY("agent_eval", config, SimpleNamespace())
    app = FastAPI()
    started = {"count": 0}

    @app.middleware("http")
    async def stub(request: Request, call_next):
        path = request.url.path
        if request.cookies.get("ev_mode") == "fails" and path.endswith("/runs") and request.method == "GET":
            return JSONResponse({"detail": "The run store is locked"}, status_code=500)
        if path == "/plugins/agent_eval/runs" and request.method == "POST":
            started["count"] += 1
            return JSONResponse({"run_id": "20261023-100000-dddd"})
        return await call_next(request)

    @app.get("/__stub/started")
    async def count():
        return started

    @app.post("/__stub/stop_one")
    async def stop_one():
        from plugins.agent_eval import store
        store.append_record("20261022-100000-cccc", {
            "run_id": "20261022-100000-cccc", "n": 13, "arm": "base", "task": "fix-pager", "kind": "learn",
            "rep": 3, "dir": "runs/0013-base-r3", "scored": False, "status": "stopped"})
        # refused before it ran: the usage store found no call, so its cost is 0, not unknown
        store.append_record("20261022-100000-cccc", {
            "run_id": "20261022-100000-cccc", "n": 14, "arm": "base", "task": "add-cli-flag", "kind": "learn",
            "rep": 3, "dir": "runs/0014-base-r3", "scored": False, "status": "refused", "cost": 0.0})
        return {"ok": True}

    @app.post("/__stub/append")
    async def append():
        from plugins.agent_eval import store
        store.append_record("20261021-100000-bbbb", {
            "run_id": "20261021-100000-bbbb", "n": 25, "arm": "terse", "task": "fix-pager", "kind": "learn",
            "rep": 3, "dir": "runs/0025-terse-r3", "scored": True, "status": "done", "score": 1.0, "passed": True,
            "detail": "5/5 passed", "answer": "Done.", "cost": 0.009, "errors": []})
        return {"ok": True}

    registry = PluginWebRegistry()
    registry.register_web_plugin("agent_eval", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/agent_eval", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    before = os.environ.get(DATA_DIR_ENV)
    try:
        app = panel_app(tmp_path_factory.mktemp("eval_panel"))
        return run_app_test_page(BROWSER, app, "tests/agent_eval/panel_tests.html", timeout=PAGE_TIMEOUT)
    finally:
        if before is None:
            os.environ.pop(DATA_DIR_ENV, None)
        else:
            os.environ[DATA_DIR_ENV] = before


EXPECTED = [
    'the runs are listed with status, baseline and pass counts',
    'a runner that died is shown as died, not running',
    'a run shows its arms, the verdict and the task matrix',
    'comparing against the baseline names it as the reference',
    'a record opens with its check output and answer',
    'the chosen tab stays chosen when new runs come in',
    'switching to another run compares it against its own reference again',
    'a run counts only the agent runs that ran',
    'starting asks first: cancelled starts nothing, confirmed starts the suite',
    'a failed load of the runs says so',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_agent_eval_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
