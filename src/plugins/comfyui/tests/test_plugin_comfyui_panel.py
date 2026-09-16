"""The ComfyUI panel in a real browser, against the real plugin: its router, its job tracker, its client, its static
files. Only the network is a stand-in, where the client opens its HTTP sessions: two ComfyUI servers answered in memory.

Configured: ``gpu1.test:8188`` (online) and ``gpu2.test:8189`` (refuses every connection). gpu1 runs ``run-1111-a``
and has ``wait-2222-b`` and ``other-9999-z`` (not tracked: another client's) queued; its history holds ``hist-7777-g`` (succeeded). Tracked: ``run-1111-a`` (Flux Cover,
queued in the tracker although gpu1 runs it), ``wait-2222-b`` (Stable Audio, queued 200 s ago), ``far-6666-f`` (queued
on gpu2), ``lost-3333-c`` (queued on gpu1, but neither in its queue nor in its history), ``hist-7777-g`` (running in the
tracker), ``done-4444-d`` (completed ten minutes ago after 125.4 s, two images and a text; submitted before
``fail-5555-e``, so the two finished in the other order) and ``fail-5555-e`` (failed a quarter of an hour ago after
3725 s, with markup in its workflow name and its error) and ``old-8888-h`` (completed, its duration never recorded,
four images).

Behind the panel's back: POST /__stub/reset seeds everything anew, /__stub/finish?id= lets gpu1 finish a job,
/__stub/refuse makes gpu1 refuse interrupts, /__stub/offline takes gpu1 off the network, /__stub/clear forgets every job.
GET /__stub/asked counts the job lists asked for and the cancels; GET /__stub/calls lists the POSTs gpu1 received. With
the cookie ``cu_jobs=fails`` the job list fails, ``cu_jobs=slow`` takes 1.5 s, ``cu_jobs=slower`` 3 s, ``cu_jobs=slowfail``
fails after 1.5 s; ``cu_cancel=slow`` holds a cancel for 1.5 s.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.config.models import AgentSystemConfig, MCPConfig
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 150
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
GPU1 = "gpu1.test:8188"
GPU2 = "gpu2.test:8189"


class Answer:
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload

    async def json(self):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class ComfyUI:
    """The servers the client reaches, in memory. Any other host is refused: nothing leaves the process."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.online = {GPU1: True}
        self.running = ["run-1111-a"]
        self.pending = ["wait-2222-b", "other-9999-z"]
        self.history = {"hist-7777-g": {"status": {"status_str": "success"}}}
        self.refuse = False
        self.calls: list[str] = []

    def answer(self, method: str, url: str, body) -> Answer:
        address, _, path = url.removeprefix("http://").partition("/")
        if not self.online.get(address):
            host, _, port = address.partition(":")
            raise aiohttp.ClientConnectorError(SimpleNamespace(host=host, port=int(port), ssl=True),
                                               OSError(111, "Connect call failed"))
        if method == "GET" and path == "queue":
            return Answer(200, {"queue_running": [[0, id_, {}] for id_ in self.running],
                                "queue_pending": [[i + 1, id_, {}] for i, id_ in enumerate(self.pending)]})
        if method == "GET" and path.startswith("history/"):
            id_ = path.removeprefix("history/")
            return Answer(200, {id_: self.history[id_]} if id_ in self.history else {})
        self.calls.append(f"{method} /{path} {json.dumps(body) if body else ''}".strip())
        if method == "POST" and path == "interrupt":
            if self.refuse:
                return Answer(500, {})
            self.history.update({id_: {"status": {"status_str": "error"}} for id_ in self.running})
            self.running = []
            return Answer(200, {})
        if method == "POST" and path == "queue":
            self.pending = [id_ for id_ in self.pending if id_ not in body["delete"]]
            return Answer(200, {})
        return Answer(404, {})

    def session(self):
        comfy = self

        class Session:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def get(self, url, params=None):
                return comfy.answer("GET", url, None)

            def post(self, url, json=None, headers=None):
                return comfy.answer("POST", url, json)

        return Session


def seed(tracker) -> None:
    now = datetime.now(timezone.utc)
    ago = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()  # noqa: E731
    with sqlite3.connect(tracker.db_path) as conn:
        conn.execute("DELETE FROM jobs")
    rows = [
        # prompt_id, workflow_id, workflow_name, status, submitted, started, completed, duration, outputs, error, server
        ("run-1111-a", "flux_cover", "Flux Cover", "queued", ago(300), None, None, None, None, None, GPU1),
        ("wait-2222-b", "stable_audio", "Stable Audio", "queued", ago(200), None, None, None, None, None, GPU1),
        ("far-6666-f", "flux_cover", "Flux Cover", "queued", ago(100), None, None, None, None, None, GPU2),
        ("lost-3333-c", "flux_cover", None, "queued", ago(400), None, None, None, None, None, GPU1),
        ("hist-7777-g", "stable_audio", "Stable Audio", "running", ago(80), ago(50), None, None, None, None, GPU1),
        ("done-4444-d", "flux_cover", "Flux Cover", "completed", ago(9500), ago(9400), ago(600), 125.4,
         json.dumps({"images": ["a.png", "b.png"], "text": ["t.txt"]}), None, GPU1),
        ("fail-5555-e", "tts", '<img src=x onerror="window.parent.__xss=1">', "failed", ago(9000), ago(8000), ago(900),
         3725, None, "<b>CUDA</b> out of memory", GPU1),
        ("old-8888-h", "flux_cover", "Flux Cover", "completed", ago(9900), None, ago(9800), None,
         json.dumps({"images": ["c.png", "d.png", "e.png", "f.png"]}), None, GPU1),
    ]
    with sqlite3.connect(tracker.db_path) as conn:
        conn.executemany(
            "INSERT INTO jobs (prompt_id, workflow_id, workflow_name, status, submitted_at, started_at, completed_at,"
            " duration_seconds, outputs, error_message, server_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*row[:10], f"http://{row[10]}") for row in rows])


def panel_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from plugins.comfyui import comfyui_client
    from plugins.comfyui.server import ComfyUIServer

    comfy = ComfyUI()
    monkeypatch.setattr(comfyui_client, "aiohttp", SimpleNamespace(
        ClientSession=comfy.session(), ClientTimeout=aiohttp.ClientTimeout,
        ClientConnectorError=aiohttp.ClientConnectorError))
    config = MCPConfig()
    config.host, config.port = "gpu1.test", 8188
    config.servers = [{"host": "gpu1.test", "port": 8188}, {"host": "gpu2.test", "port": 8189}]
    config.output_dir = str(tmp_path / "outputs")
    config.cleanup_age_hours = 0
    plugin = ComfyUIServer("comfyui", AgentSystemConfig(), config)
    tracker = plugin.job_tracker
    seed(tracker)
    app = FastAPI()
    asked = {"jobs": 0, "cancels": 0}

    @app.middleware("http")
    async def stand_ins(request: Request, call_next):
        path = request.url.path
        mode = request.cookies.get("cu_jobs")
        if path == "/plugins/comfyui/jobs":
            asked["jobs"] += 1
            if mode == "fails":
                return JSONResponse({"detail": "The job database is locked"}, status_code=500)
        if path.endswith("/cancel"):
            asked["cancels"] += 1
            if request.cookies.get("cu_cancel") == "slow":
                await asyncio.sleep(1.5)  # before it reaches the plugin: the job is still active meanwhile
        if not (path == "/plugins/comfyui/jobs" and mode in ("slow", "slower", "slowfail")):
            return await call_next(request)
        # headers at once, body held, not cacheable: the browser's cache lock would otherwise hold back the next
        # request for the same URL until this answer is complete
        if mode == "slowfail":
            status, payload = 500, b'{"detail": "The job database is locked"}'
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

    @app.get("/__stub/calls")
    async def stub_calls():
        return comfy.calls

    @app.post("/__stub/reset")
    async def stub_reset():
        comfy.reset()
        seed(tracker)
        return {}

    @app.post("/__stub/finish")
    async def stub_finish(id: str):
        comfy.running.remove(id)
        comfy.history[id] = {"status": {"status_str": "success"}}
        return {}

    @app.post("/__stub/refuse")
    async def stub_refuse():
        comfy.refuse = True
        return {}

    @app.post("/__stub/offline")
    async def stub_offline():
        comfy.online[GPU1] = False
        return {}

    @app.post("/__stub/clear")
    async def stub_clear():
        with sqlite3.connect(tracker.db_path) as conn:
            conn.execute("DELETE FROM jobs")
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted as the app mounts them
    registry.register_web_plugin("comfyui", plugin)
    registry.apply_to_app(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/comfyui", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        app = panel_app(tmp_path_factory.mktemp("comfyui_panel"), monkeypatch)
        yield run_app_test_page(BROWSER, app, "tests/comfyui/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'opened, the panel shows each server with its queue, or offline with the reason',
    'the tracker is brought up to date with the servers before the jobs are counted and listed',
    'a finished job shows its outcome, error, duration and outputs',
    'workflow names and errors are drawn as text, never as markup',
    "the finished jobs come in the tracker's order until a click on a column head sorts them by value, and that order holds when they are drawn anew",
    'cancelling asks first: declined nothing happens, confirmed the job is cancelled on its server',
    'a refused cancel is shown as an error and the job stays active',
    'a job that finished before the cancel reached it keeps its outcome',
    'a double click cancels once, and the button stays disabled until the answer is in',
    'the keyboard focus stays on a cancel button when the jobs are drawn anew',
    'with ComfyUI offline the panel says so and keeps the tracked jobs as they were',
    'with no jobs the panel says so',
    'a failed load shows the error and nothing of before',
    'an answer overtaken by a later load is dropped',
    'ticks of the auto refresh ask nothing more while a load is on its way',
    'the auto refresh runs from the start and brings what changed',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_comfyui_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
