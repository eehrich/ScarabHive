"""What every reader of a run's events relies on, now that each of them gets every event.

- A stream counts itself as a reader of its job for as long as it is open, the run's
  own as much as a reconnect: the job cleanup keeps a job a reader is still on. It
  stops counting the moment its client goes, whenever that is.
- A reconnect with ``catch_up=skip&seen=N`` reads on from event N: the session load
  before it showed what the first N said.
- A request is found running on whichever agent server runs it.

The stream test drives the real build_app with auth ON, signed in as the admin of
data/users.db (read-only), as tests/app/test_session_resolve_endpoint.py does.
"""
from __future__ import annotations

import asyncio
import gc
import json
import sqlite3
import uuid
from types import SimpleNamespace

import httpx
import pytest

from agent_system.auth.security import create_access_token
from agent_system.servers.agent.server import Agent
from live_accounts import signing_key, token_generation
from agent_system.services.background_job_manager import BackgroundJobManager, get_background_job_manager

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _admin_headers():
    try:
        with sqlite3.connect("file:data/users.db?mode=ro", uri=True) as db:
            row = db.execute("select id, username, role from users where username='admin'").fetchone()
    except sqlite3.Error as e:
        pytest.skip(f"no user store to sign a token against: {e}")
    if not row:
        pytest.skip("no admin account to sign a token against")
    return {"Authorization": "Bearer " + create_access_token(
        {"sub": row[1], "user_id": row[0], "role": row[2], "gen": token_generation(row[0])},
        secret_key=signing_key(), algorithm="HS256")}


async def test_the_runs_own_stream_counts_as_a_reader_while_it_is_open(tmp_path, monkeypatch):
    from agent_system import app as app_mod

    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    app = app_mod.build_app()
    go = asyncio.Event()

    async def run_events(self, task, request_id=None, *args, **kwargs):
        yield {"type": "start", "request_id": request_id, "session_id": None}
        await go.wait()
        yield {"type": "end", "request_id": request_id}

    monkeypatch.setattr(Agent, "run_events", run_events)
    request_id = f"reader{uuid.uuid4().hex[:10]}"
    manager = get_background_job_manager()
    # ASGITransport hands the response over only once it is complete: the stream is read
    # in a task of its own, and looked at from here while the run waits.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        streaming = asyncio.create_task(client.post("/events", headers=_admin_headers(), timeout=30.0,
                                                    json={"task": "work", "request_id": request_id}))
        try:
            for _ in range(250):
                job = await manager.get_job(request_id)
                if job is not None and job.events_emitted:
                    break
                await asyncio.sleep(0.02)
            assert job is not None and job.events_emitted, "fixture: the run never started"
            await asyncio.sleep(0.1)
            assert job.sse_client_count == 1, "the run's own stream is not counted as its reader"
        finally:
            go.set()
        response = await streaming
    assert response.status_code == 200, response.text
    assert job.sse_client_count == 0, "the closed stream is still counted"


async def test_a_stream_its_client_left_during_a_send_stops_counting_at_once(tmp_path, monkeypatch):
    # Starlette cancels the response of a client that went, but never closes its generator:
    # caught in the middle of a send, the generator waited at its yield for the garbage
    # collector -- and the stream's `finally` with it. Here the collector is off.
    from agent_system import app as app_mod

    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    app = app_mod.build_app()
    go = asyncio.Event()

    async def run_events(self, task, request_id=None, *args, **kwargs):
        yield {"type": "start", "request_id": request_id, "session_id": None}
        for n in range(3):
            yield {"type": "status", "message": f"step {n}"}
        await go.wait()
        yield {"type": "end", "request_id": request_id}

    monkeypatch.setattr(Agent, "run_events", run_events)
    headers = [(b"host", b"test"), (b"content-type", b"application/json")]
    headers += [(name.lower().encode(), value.encode()) for name, value in _admin_headers().items()]
    request_id = f"left{uuid.uuid4().hex[:10]}"

    async def left_during_a_send(method, query, body):
        sent, gone, asked = [], asyncio.Event(), []

        async def receive():
            if not asked:
                asked.append(True)
                return {"type": "http.request", "body": body, "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                sent.append(message)
                if len(sent) == 3:
                    gone.set()
                    await asyncio.sleep(0.05)   # a slow socket: the cancel lands in here

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": "/events", "raw_path": b"/events", "query_string": query.encode(),
                 "root_path": "", "server": ("test", 80), "client": ("127.0.0.1", 1234), "headers": headers}
        await asyncio.wait_for(app(scope, receive, send), 10)
        assert len(sent) >= 3, "fixture: the client left before the stream said anything"

    manager = get_background_job_manager()
    gc.disable()
    try:
        await left_during_a_send("POST", "", json.dumps({"task": "work", "request_id": request_id}).encode())
        job = await manager.get_job(request_id)
        assert job is not None and job.status.value == "running", "fixture: the run is not going on"
        assert job.sse_client_count == 0, "the run's own stream still counts as its reader"
        await left_during_a_send("GET", f"task=&request_id={request_id}&catch_up=skip&seen=0", b"")
        assert job.sse_client_count == 0, "the reconnect still counts as its reader"
    finally:
        gc.enable()
        go.set()


async def test_a_reconnect_reads_on_from_the_events_its_session_load_already_showed(tmp_path, monkeypatch):
    from agent_system import app as app_mod

    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    app = app_mod.build_app()
    headers = _admin_headers()

    async def runner():
        for n in range(4):
            yield {"type": "tool_call", "n": n}

    request_id = f"seen{uuid.uuid4().hex[:10]}"
    manager = get_background_job_manager()
    await manager.create_job(request_id=request_id, user_id="admin", agent_name="default",
                             session_id=None, agent_runner=runner)
    job = await manager.get_job(request_id)
    for _ in range(250):
        if job.status.value != "running":
            break
        await asyncio.sleep(0.02)
    assert job.events_emitted == 4, "fixture: the run did not send its four events"

    async def numbers_read(query):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/events?task=&request_id={request_id}{query}", headers=headers)
        assert response.status_code == 200, response.text
        return [json.loads(line[len("data: "):]).get("n") for line in response.text.splitlines()
                if line.startswith("data: ") and '"tool_call"' in line]

    assert await numbers_read("&catch_up=skip&seen=2") == [2, 3], "the reconnect replayed what the load showed"
    assert await numbers_read("") == [0, 1, 2, 3], "a plain reconnect does not replay the buffer"


async def test_a_request_is_found_running_on_the_agent_that_runs_it():
    def agent_running(*request_ids):
        agent = Agent.__new__(Agent)
        agent._request_manager = SimpleNamespace(get_active_requests=lambda: set(request_ids))
        return agent

    manager = BackgroundJobManager()
    registered = agent_running("on-a-registered-agent")
    registry = SimpleNamespace(list=lambda: ["book"], get=lambda name: registered)
    manager.set_agent_registry(registry, default_agent=agent_running("on-the-default-agent"))

    assert await manager.is_request_active_anywhere("on-a-registered-agent")
    assert await manager.is_request_active_anywhere("on-the-default-agent")
    assert not await manager.is_request_active_anywhere("nowhere")
