"""utils.profiling: what the Performance panel shows has to describe the process as it is."""
from __future__ import annotations

import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from agent_system.utils import profiling


def test_a_busy_executor_thread_is_active_and_a_waiting_one_idle():
    """thread.py:_worker sits below every executor thread's work: only the innermost frame says it waits."""
    stop = threading.Event()
    started = threading.Event()

    def busy():
        started.set()
        while not stop.is_set():
            sum(range(1000))

    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="probe")
    try:
        pool.submit(busy)
        pool.submit(lambda: None).result()
        started.wait(5)
        time.sleep(0.2)  # the finished worker is back waiting for work
        states = {t["name"]: t["idle"] for t in profiling._get_thread_details()["threads"] if t["name"].startswith("probe")}
    finally:
        stop.set()
        pool.shutdown()

    assert sorted(states.values()) == [False, True], states


def test_a_thread_waiting_on_an_event_is_idle():
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="probe-waiting", daemon=True)
    thread.start()
    time.sleep(0.1)
    try:
        info = next(t for t in profiling._get_thread_details()["threads"] if t["name"] == "probe-waiting")
    finally:
        release.set()
        thread.join()

    assert info["idle"] and info["idle_reason"] == "threading.py:wait"


async def test_an_early_wake_up_is_a_lag_sample_of_zero_not_skipped(monkeypatch):
    """Otherwise "current" keeps showing the last positive lag, however long ago it was."""
    monitor = profiling.EventLoopMonitor()
    readings = iter([0.0, 0.3, 1.0, 1.09])  # 200 ms late, then 10 ms early
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            monitor._running = False

    monkeypatch.setattr(profiling, "time", SimpleNamespace(perf_counter=lambda: next(readings)))
    monkeypatch.setattr(profiling, "asyncio", SimpleNamespace(sleep=sleep))
    monitor._running = True
    await monitor._monitor_loop()

    stats = monitor.get_lag_stats()
    assert stats["samples"] == 2 and stats["current_ms"] == 0 and stats["max_ms"] == pytest.approx(200)


def test_task_count_and_task_list_come_from_one_enumeration(monkeypatch):
    calls = []

    def all_tasks():
        calls.append(1)
        coro = SimpleNamespace(__qualname__="work")
        # a task more on every call, as on a busy server
        return [SimpleNamespace(get_name=lambda i=i: f"Task-{i}", get_coro=lambda: coro) for i in range(2 + len(calls))]

    monkeypatch.setattr(profiling.asyncio, "all_tasks", all_tasks)

    tasks = profiling.get_profiling_report()["async_tasks"]

    assert tasks["total_count"] == len(tasks["tasks"]) == 3 and len(calls) == 1


def test_the_tasks_listed_do_not_depend_on_the_order_the_loop_hands_them_out(monkeypatch):
    """all_tasks() is a set: cut unsorted, the same tasks would list differently on every call."""
    def task(name, coro):
        return SimpleNamespace(get_name=lambda: name, get_coro=lambda: SimpleNamespace(__qualname__=coro))

    tasks = [task("Task-9", "serve"), task("Task-10", "run_asgi"), task("Task-2", "serve"), task("Task-1", "main")]
    monkeypatch.setattr(profiling.asyncio, "all_tasks", lambda: list(tasks))

    first = profiling.get_async_tasks(limit=3)
    tasks.reverse()

    assert profiling.get_async_tasks(limit=3) == first
    assert [(t["coro"], t["name"]) for t in first["tasks"]] == [("main", "Task-1"), ("run_asgi", "Task-10"), ("serve", "Task-2")]
    assert first["total_count"] == 4


def test_the_report_does_not_walk_every_live_object(monkeypatch):
    """Asked every few seconds by the panel, on the event loop: len(gc.get_objects()) is too much."""
    def walk(*args):
        raise AssertionError("gc.get_objects() called")

    monkeypatch.setattr(gc, "get_objects", walk)

    memory = profiling.get_profiling_report()["memory"]

    assert "error" not in memory and memory["rss_mb"] > 0


def test_request_stats_are_counted_per_route_never_per_url(monkeypatch, tmp_path):
    """Each URL a client makes up must not open a row: ids, 404s, slash redirects, spellings of a static file."""
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "f.js").write_text("", encoding="utf-8")
    monkeypatch.setattr(profiling, "PROFILING_ENABLED", True)
    monkeypatch.setattr(profiling, "_request_profiler", profiling.RequestProfiler())
    app = FastAPI()

    @app.get("/sessions/{session_id}")
    async def session(session_id: str):
        return {}

    sub = FastAPI()

    @sub.get("/runs/{run_id}")
    async def run(run_id: str):
        return {}

    # Nested like api/debug_endpoints.py: newer fastapi leaves the inner route, without /debug, in the scope.
    outer, inner = APIRouter(prefix="/debug"), APIRouter(prefix="/memory")

    @inner.get("/objects/{kind}")
    async def objects(kind: str):
        return {}

    outer.include_router(inner)
    app.include_router(outer)
    app.mount("/static", StaticFiles(directory=tmp_path), name="static")
    app.mount("/sub", sub)
    profiling.add_profiling_middleware(app)
    client = TestClient(app, follow_redirects=False)
    answers = {path: client.get(path).status_code for path in (
        "/sessions/1", "/sessions/2", "/sessions/1/", "/sessions/2/", "/nope/1", "/nope/2",
        "/static/js/f.js", "/static//js/f.js", "/static/js//f.js", "/static/JS/f.js", "/sub/runs/7",
        "/debug/memory/objects/a", "/debug/memory/objects/b")}

    stats = profiling.get_profiler().get_stats()
    assert answers["/sessions/1/"] == 307 and answers["/static//js/f.js"] == 200
    assert answers["/debug/memory/objects/a"] == 200
    assert {path: row["count"] for path, row in stats.items()} == {
        "/sessions/{session_id}": 2, "(no route)": 4, "/static/{path}": 4, "/sub/runs/{run_id}": 1,
        "/debug/memory/objects/{kind}": 2}
    assert [r.path for r in profiling.get_profiler()._completed_requests][:2] == ["/sessions/1", "/sessions/2"]
