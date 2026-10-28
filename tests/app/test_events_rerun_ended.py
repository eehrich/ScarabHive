"""/events with ``rerun_ended``: a job that broke off runs again under its id; every other job is joined.

A finished job stays in the manager until the process ends, so a dispatcher's retry under the same request_id
was joined to it and only replayed its failure. With ``rerun_ended`` a job that failed, or ended without an
answer (a run that reports its failure as an error event ends normally), starts again as the same agent; a
running one, one that answered, and a cancelled one are joined as without it -- a dispatcher may send the flag
on every dispatch.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

pytestmark = pytest.mark.anyio


def _disable_auth(monkeypatch):
    from agent_system.config.models import AuthConfig

    class _DisabledAuth:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _DisabledAuth(), raising=False)


async def _ended_job(mgr, rid, agent_name, how):
    async def runner():
        yield {"type": "status", "message": "first-run"}
        if how == "raised":
            raise RuntimeError("the run broke")
        if how == "error event":
            yield {"type": "error", "message": "the machine failed"}
        elif how == "answered":
            yield {"type": "final", "content": "the answer"}
        elif how == "cancelled":
            raise asyncio.CancelledError()

    job = await mgr.create_job(request_id=rid, user_id="anonymous", agent_name=agent_name, session_id=None,
                               agent_runner=runner)
    try:
        await job.task
    except (asyncio.CancelledError, RuntimeError):
        pass
    assert job.status.value != "running", "fixture: the job did not end"
    return job


@pytest.mark.parametrize("how,flag,runs_again", [
    ("raised", True, True),
    ("error event", True, True),
    ("error event", False, False),
    ("answered", True, False),
    ("cancelled", True, False),
])
async def test_rerun_ended_runs_only_a_job_that_broke_off_again(monkeypatch, how, flag, runs_again):
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod
    from agent_system.services.background_job_manager import get_background_job_manager

    app = app_mod.build_app()
    agent = app.state.agent
    fresh_runs: list[str] = []

    async def _stub_run_events(task, request_id, session_id=None, **kwargs):
        fresh_runs.append(request_id)
        yield {"type": "final", "content": "second run"}

    monkeypatch.setattr(agent, "run_events", _stub_run_events, raising=True)
    rid = f"rerun-{how.replace(' ', '-')}-{int(flag)}"
    await _ended_job(get_background_job_manager(), rid, agent.name, how)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/events", json={"task": "go", "request_id": rid, "rerun_ended": flag},
                                 timeout=20.0)
    assert resp.status_code == 200, resp.text[:300]
    assert fresh_runs == ([rid] if runs_again else []), resp.text[:400]
    if runs_again:
        assert "second run" in resp.text
