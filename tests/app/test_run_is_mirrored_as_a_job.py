"""A run started through POST /run can be followed like any other run.

writer_jobs starts every book that way (linear_book). /run collected the run for
its caller and made no BackgroundJob, so the chat -- which attaches to a
session's job -- could only read the run back on a reload, never follow it. The
run is now mirrored into a job; the caller still gets its answer from
collect_final_result, not through the job's queue.

Once such a run is over, the status endpoint answers as it did before: a
finished job's status is what writer_jobs' reconcile takes as proof that a book
run is done, and for a /run that proof never went through the job.

Drives the real build_app over ASGI; the agent's run is the only fake.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest

from agent_system.servers.agent.server import Agent
from agent_system.services.background_job_manager import JobStatus, get_background_job_manager

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _disable_auth(monkeypatch):
    from agent_system.config.models import AuthConfig

    class _DisabledAuth:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _DisabledAuth(), raising=False)


def _a_run_that_waits(monkeypatch, session_id):
    """The agent's run: it starts, says where, and waits for ``go`` before it answers."""
    go = asyncio.Event()

    async def run_events(self, task, request_id=None, session_id_=None, **kwargs):
        yield {"type": "start", "session_id": session_id, "request_id": request_id}
        yield {"type": "status", "message": "working", "request_id": request_id}
        await go.wait()
        yield {"type": "final", "summary": "done", "request_id": request_id}
        yield {"type": "end", "request_id": request_id}

    monkeypatch.setattr(Agent, "run_events", run_events)
    return go


async def _job_of(request_id, timeout=10.0):
    manager = get_background_job_manager()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = await manager.get_job(request_id)
        if job is not None and job.actual_session_id:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("the run never showed up as a job with its session")


async def test_a_run_started_through_run_can_be_followed_while_it_works(tmp_path, monkeypatch):
    from agent_system import app as app_mod

    _disable_auth(monkeypatch)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    app = app_mod.build_app()
    request_id = f"mirror{uuid.uuid4().hex[:10]}"
    session_id = f"s{uuid.uuid4().hex[:10]}"
    go = _a_run_that_waits(monkeypatch, session_id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        run = asyncio.create_task(client.post("/run", json={"task": "a book", "request_id": request_id},
                                              timeout=60.0))
        try:
            job = await _job_of(request_id)
            assert job.status == JobStatus.RUNNING and job.mirror
            assert job.actual_session_id == session_id, "the job does not know the session its run works in"
            # what the chat asks before it attaches (called directly: the session
            # endpoints sign in on their own, and the job manager is the real one)
            from agent_system.api.session_endpoints import list_active_sessions
            owner = SimpleNamespace(username=job.user_id)
            owns = SimpleNamespace(belongs_to=lambda user_id, sid: True)
            active = (await list_active_sessions(ids=session_id, current_user=owner, session_manager=owns))["active"]
            assert active.get(session_id, {}).get("attachable") is True, active
            assert active[session_id]["request_id"] == request_id
            # the events are there for a page that attaches
            await asyncio.sleep(0.05)
            assert job.events_emitted >= 2, "the run's events were not mirrored into the job"
        finally:
            go.set()
        response = await run

    assert response.status_code == 200, response.text
    assert response.json()["summary"] == "done", "the caller of /run lost its answer to the mirror"


async def test_a_finished_run_from_run_is_not_offered_as_a_finished_job(tmp_path, monkeypatch):
    """writer_jobs' reconcile marks a book row done on a finished job's status with
    the job's keys; a /run's caller had its answer, and its row must be resumed, not
    buried, when the worker lost it."""
    from agent_system import app as app_mod

    _disable_auth(monkeypatch)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    app = app_mod.build_app()
    request_id = f"mirror{uuid.uuid4().hex[:10]}"
    go = _a_run_that_waits(monkeypatch, f"s{uuid.uuid4().hex[:10]}")
    go.set()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={"task": "a book", "request_id": request_id}, timeout=60.0)
        assert response.status_code == 200, response.text
        job = await get_background_job_manager().get_job(request_id)
        assert job is not None and job.mirror, "fixture: the run left no mirror job to hide"
        for _ in range(100):  # the relay ends a moment after the answer
            if job.status != JobStatus.RUNNING:
                break
            await asyncio.sleep(0.02)
        assert job.status != JobStatus.RUNNING, "fixture: the mirror job never finished"
        status = (await client.get(f"/api/requests/{request_id}/status")).json()

    assert "sse_clients" not in status and "events_buffered" not in status, status
    assert status.get("completed") is not True, status
