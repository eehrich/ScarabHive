"""The Batch Queues panel in a real browser, against the real plugin: its router, its static files, and a real batch
queue manager holding jobs.

Seeded (collection window 30 s): ``anthropic:claude-x`` with one request of 1216 tokens waiting and job ``job-rec``,
recovered after a restart (submitted, no requests, provider id ``batches/rec-1``); ``gemini:flash`` with job
``job-done`` completed a minute ago after 100 s (40 of 40 requests, 1212280 tokens), and ``job-old`` completed ten
minutes ago; ``openai:gpt-4o`` with job ``job-run`` in progress for 125 s (3 of 5 requests done, 1 failed, provider id
``batch_abc``), job ``job-new`` pending and not yet submitted, and two requests waiting; ``openai:empty`` with an empty
request list; ``mistral:large`` with one request of 999970 tokens waiting and no job. Totals: 3 jobs completed, 1 failed, 1200 requests completed, 7 failed, processing times 0 s, 90 s
and 3599.7 s. Behind the panel's back: POST /__stub/off takes the manager away, /__stub/idle puts an empty one in its
place, /__stub/on brings the seeded one back, /__stub/advance completes job-run and adds a request to
``openai:gpt-4o``. With the cookie ``bm=fails`` the queues fail, ``bm=metrics`` the metrics fail, ``bm=slow`` the
queues take 1.5 s, ``bm=slower`` 3 s, ``bm=slowfail`` they fail after 1.5 s. GET /__stub/asked counts the queue loads asked for.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.llm.batch import initialization
from agent_system.llm.batch.models import BatchJob, BatchRequest, BatchStatus
from agent_system.llm.batch.queue_manager import BatchQueueManager
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent


def requests(count: int, model: str, chars: int = 400) -> list[BatchRequest]:
    return [BatchRequest(model=model, messages=[{"role": "user", "content": "x" * chars}]) for _ in range(count)]


def seeded_manager(storage: Path) -> BatchQueueManager:
    now = datetime.now(timezone.utc)
    manager = BatchQueueManager(storage_path=storage / "seeded")
    manager._collection_window = 30.0
    manager._queues["anthropic:claude-x"] = requests(1, "claude-x", 4000)
    manager._queues["openai:gpt-4o"] = requests(2, "gpt-4o")
    manager._queues["mistral:large"] = requests(1, "large", 3299888)
    manager._queues["openai:empty"] = []
    running = BatchJob(job_id="job-run", provider="openai", model="gpt-4o", status=BatchStatus.IN_PROGRESS,
                       requests=requests(5, "gpt-4o"), provider_job_id="batch_abc",
                       submitted_at=now - timedelta(seconds=125), completed_count=3, failed_count=1)
    waiting = BatchJob(job_id="job-new", provider="openai", model="gpt-4o", requests=requests(1, "gpt-4o"))
    recovered = BatchJob(job_id="job-rec", provider="anthropic", model="claude-x", status=BatchStatus.SUBMITTED, requests=[],
                         provider_job_id="batches/rec-1")
    manager._active_jobs.update({"job-run": running, "job-new": waiting, "job-rec": recovered})
    for job_id, ago in (("job-done", 60), ("job-old", 600)):
        manager._completed_jobs[job_id] = BatchJob(
            job_id=job_id, provider="gemini", model="flash", status=BatchStatus.COMPLETED, requests=requests(40, "flash", 100000),
            completed_count=40, submitted_at=now - timedelta(seconds=ago + 100), completed_at=now - timedelta(seconds=ago))
    metrics = manager._metrics
    metrics.completed_jobs, metrics.failed_jobs = 3, 1
    metrics.completed_requests, metrics.failed_requests = 1200, 7
    for seconds in (0.0, 90, 3599.7):
        metrics.add_processing_time(seconds)
    return manager


def panel_app(storage: Path):
    """The app; it installs its manager as the running one (the caller restores ``initialization``)."""
    from plugins.batch_monitor.plugin import PLUGIN_FACTORY

    manager = seeded_manager(storage)
    initialization._batch_queue_manager = manager
    plugin = PLUGIN_FACTORY("batch_monitor", {}, {})
    app = FastAPI()
    asked = {"queues": 0}

    @app.middleware("http")
    async def modes(request: Request, call_next):
        mode = request.cookies.get("bm")
        path = request.url.path
        if path == "/plugins/batch_monitor/queues":
            asked["queues"] += 1
        if (mode == "fails" and path == "/plugins/batch_monitor/queues") or (mode == "metrics" and path == "/plugins/batch_monitor/metrics"):
            return JSONResponse({"detail": "The manager lock timed out"}, status_code=500)
        if mode in ("slow", "slower", "slowfail") and path == "/plugins/batch_monitor/queues":
            # headers at once, body held, not cacheable: the browser's cache lock would otherwise hold back the
            # next request for the same URL until this answer is complete
            if mode == "slowfail":
                status, payload = 500, b'{"detail": "The manager lock timed out"}'
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
    async def queues_asked():
        return asked

    @app.post("/__stub/off")
    async def off():
        initialization._batch_queue_manager = None
        return {}

    @app.post("/__stub/idle")
    async def idle():
        initialization._batch_queue_manager = BatchQueueManager(storage_path=storage / "idle")
        return {}

    @app.post("/__stub/on")
    async def on():
        initialization._batch_queue_manager = manager
        return {}

    @app.post("/__stub/advance")
    async def advance():
        job = manager._active_jobs.pop("job-run")
        job.status, job.completed_count, job.completed_at = BatchStatus.COMPLETED, 4, datetime.now(timezone.utc)
        manager._completed_jobs["job-run"] = job
        manager._queues["openai:gpt-4o"].extend(requests(1, "gpt-4o"))
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("batch_monitor", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/batch_monitor", StaticFiles(directory=TESTS), name="panel-tests")
    return app


def run_page(storage: Path, timeout: float = PAGE_TIMEOUT) -> dict:
    before = initialization._batch_queue_manager
    try:
        return run_app_test_page(BROWSER, panel_app(storage), "tests/batch_monitor/panel_tests.html", timeout=timeout)
    finally:
        initialization._batch_queue_manager = before


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    return run_page(tmp_path_factory.mktemp("batch_jobs"))


EXPECTED = [
    'the panel counts queues, running jobs, finished jobs and requests, and the processing times',
    'each job and each queue with requests waiting gets a row with status, progress, tokens and time',
    'with batch processing off the panel says so and shows nothing of before',
    'with nothing waiting, running or just finished the panel says so',
    'an answer overtaken by a later load is dropped',
    'a failed load shows the error and nothing of before, also when only the totals fail',
    'ticks of the auto refresh ask nothing more while a load is on its way',
    'the auto refresh runs from the start and brings what changed',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_batch_monitor_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
