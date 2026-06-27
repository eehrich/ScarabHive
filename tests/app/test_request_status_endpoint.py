"""Tests for GET /api/requests/{rid}/status.

The 2026-06-27 fix replaced the fallback-path lie ('status=completed,
completed=true' when neither BackgroundJobManager nor session_tracker
knew about the request) with an honest 'unknown / completed=false'
shape. These tests pin the new contract so a future refactor can't
silently reintroduce the silent-data-loss regression.

The endpoint is registered by build_app() so we exercise it via
httpx.ASGITransport with auth disabled — the same pattern
tests/status/test_status_stream_endpoint.py uses.
"""

from __future__ import annotations

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock

pytestmark = pytest.mark.anyio


def _disable_auth():
    """Patch AuthConfig.enabled to False before build_app sees it."""
    from agent_system.config.models import AuthConfig

    class _DisabledAuth:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    AuthConfig.enabled = _DisabledAuth()


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )


async def test_status_fallback_returns_unknown_not_completed(
    monkeypatch: pytest.MonkeyPatch,
):
    """2026-06-27 regression fix: when neither BackgroundJobManager
    nor session_tracker know about the request, the endpoint MUST
    NOT lie with completed=true. The honest response is
    status='unknown', completed=false plus error+reason so callers
    (writer-jobs reconcile pass, frontend poll loop) can act on the
    truth instead of treating it as a successful run."""
    _disable_auth()
    from agent_system import app as app_mod

    # Stub the background job manager to always say "I don't know".
    empty_mgr = MagicMock()
    empty_mgr.get_job = AsyncMock(return_value=None)
    monkeypatch.setattr(
        app_mod, "get_background_job_manager", lambda: empty_mgr,
    )

    app = app_mod.build_app()
    # Stub the session_tracker on the default agent so is_request_active
    # also returns False — both lookup layers miss.
    app.state.agent._session_tracker.is_request_active = AsyncMock(
        return_value=False,
    )

    async with _client(app) as client:
        resp = await client.get("/api/requests/ghost-rid/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"] == "ghost-rid"
    assert body["status"] == "unknown"
    assert body["completed"] is False
    assert body["reason"] == "no_active_run"
    # Frontend poll loop branches on status.error to surface a
    # message; this verifies the field is populated.
    assert "error" in body
    assert body["error"]


async def test_status_returns_running_when_session_tracker_has_it(
    monkeypatch: pytest.MonkeyPatch,
):
    """If the BackgroundJobManager doesn't have the request but the
    session_tracker says it's active, the endpoint correctly reports
    status='running', completed=false — the existing fallback chain
    must still work."""
    _disable_auth()
    from agent_system import app as app_mod

    empty_mgr = MagicMock()
    empty_mgr.get_job = AsyncMock(return_value=None)
    monkeypatch.setattr(
        app_mod, "get_background_job_manager", lambda: empty_mgr,
    )
    app = app_mod.build_app()
    app.state.agent._session_tracker.is_request_active = AsyncMock(
        return_value=True,
    )

    async with _client(app) as client:
        resp = await client.get("/api/requests/active-rid/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["completed"] is False


async def test_status_returns_completed_when_bg_job_has_it(
    monkeypatch: pytest.MonkeyPatch,
):
    """Real BackgroundJob path: the response carries the
    job-specific sse_clients / events_buffered keys that the
    writer-side reconcile uses as the positive-evidence marker for
    'this is a real completion' (vs the lying fallback)."""
    _disable_auth()
    from agent_system import app as app_mod
    from agent_system.services.background_job_manager import JobStatus

    # Use a real JobStatus enum value so the endpoint's
    # `job.status != JobStatus.RUNNING` check evaluates correctly.
    fake_job = MagicMock()
    fake_job.status = JobStatus.COMPLETED
    fake_job.error_message = None
    fake_job.sse_client_count = 0
    fake_job.event_queue = MagicMock()
    fake_job.event_queue.qsize = MagicMock(return_value=0)

    mgr = MagicMock()
    mgr.get_job = AsyncMock(return_value=fake_job)
    monkeypatch.setattr(
        app_mod, "get_background_job_manager", lambda: mgr,
    )

    app = app_mod.build_app()
    async with _client(app) as client:
        resp = await client.get("/api/requests/real-rid/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["completed"] is True
    # The positive-evidence keys producer.py T1 looks for:
    assert "sse_clients" in body
    assert "events_buffered" in body
