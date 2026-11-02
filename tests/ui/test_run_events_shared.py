"""run-events.js in a real browser: the shell and its panel frames follow the agent runs through ONE stream.

Each page with its own stream held one of the browser's six connections per host for good; with the shell and five
panels open, a panel opened in a new browser tab got its page but none of its data and stayed empty. The stream here
counts how many are open (run_events_shared_tests.html follows it from the page and eight frames)."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import REPO, find_browser, run_app_test_page

BROWSER = find_browser()
pytestmark = pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed")

RUN = {"run_id": "r1", "agent": "analyst", "state": "working", "title": "Analyse"}


def stream_app() -> FastAPI:
    app = FastAPI()
    streams = {"open": 0, "max": 0, "opened": 0}
    queues: list[asyncio.Queue] = []
    # who is signed in (/__stub/login); None: auth off, /auth/me answers 404 as the real app does then
    state = {"user": None}

    def runs_of(user):
        return [RUN] if user is None else [{**RUN, "run_id": f"r-{user}"}]

    @app.get("/auth/me")
    async def me():
        if state["user"] is None:
            raise HTTPException(status_code=404)
        return {"username": state["user"]}

    @app.post("/__stub/login")
    async def login(user: str):
        state["user"] = user
        return {}

    @app.get("/api/agent-runs")
    async def runs():
        return {"runs": runs_of(state["user"])}

    @app.get("/api/agent-runs/stream")
    async def stream(request: Request):
        queue: asyncio.Queue = asyncio.Queue()
        queues.append(queue)
        user = state["user"]   # a stream is the account's whose cookie opened it
        streams["open"] += 1
        streams["opened"] += 1
        streams["max"] = max(streams["max"], streams["open"])

        async def lines():
            try:
                yield "retry: 3000\n\n"
                yield f"data: {json.dumps({'type': 'runs.snapshot', 'runs': runs_of(user)})}\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"id: {event['event_id']}\ndata: {json.dumps({**event, 'who': user})}\n\n"
            finally:
                streams["open"] -= 1
                queues.remove(queue)

        return StreamingResponse(lines(), media_type="text/event-stream")

    @app.get("/__stub/streams")
    async def count():
        return streams

    @app.post("/__stub/say")
    async def say():
        for queue in queues:
            queue.put_nowait({"event_id": "e1", "run_id": "r1", "type": "run.step", "state": "working"})
        return {}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/ui", StaticFiles(directory=REPO / "tests" / "ui"), name="tests")
    return app


@pytest.fixture(scope="module")
def results():
    return run_app_test_page(BROWSER, stream_app(), "tests/ui/run_events_shared_tests.html")


EXPECTED = [
    "every page gets the runs as they stand, through one stream",
    "an event reaches every page once",
    "requests of the origin are not kept waiting by the streams",
    "a page of another account gets a stream of its own, never the last account's",
    "the stream ends when the last page stops following",
]


@pytest.mark.parametrize("name", EXPECTED)
def test_run_events_shared(results, name):
    assert results.get(name) == "ok", results


def test_every_check_the_page_ran_is_one_this_list_knows(results):
    assert sorted(results) == sorted(EXPECTED)
