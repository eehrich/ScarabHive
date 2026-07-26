"""Tests for the client-supplied request_id on POST /run.

2026-07-03 (writer_jobs audit, Finding A-F3): the writer-jobs worker
keys its reconcile probes and cancel propagation on jobs.request_id —
/run must accept that id (instead of always minting its own) or every
probe is a guaranteed miss. Guards under test:

  - format whitelist → 400 before any agent work
  - already-active id (BackgroundJob / any registry agent) → 409,
    which doubles as the duplicate-dispatch guard for worker retries
    that fire while the original run is still grinding.

Both guards raise BEFORE agent selection / LLM work, so they are
testable without a live agent run.
"""

from __future__ import annotations

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock

pytestmark = pytest.mark.anyio


def _disable_auth(monkeypatch):
    """Force AuthConfig(...).enabled to False for build_app in this test only.

    Applied via monkeypatch so pytest restores AuthConfig on teardown. A bare
    ``AuthConfig.enabled = _DisabledAuth()`` (as this did before) leaks a
    session-wide class override that disables auth for EVERY later test —
    poisoning the whole suite's auth-enforcement tests, which then see
    requires_auth=False and fail with `assert False` only in the full run.
    """
    from agent_system.config.models import AuthConfig

    class _DisabledAuth:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _DisabledAuth(), raising=False)


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )


async def test_run_rejects_malformed_request_id(
    monkeypatch: pytest.MonkeyPatch,
):
    """Format whitelist: ids flow into log lines, ownership maps and
    cancellation-token keys — anything outside [A-Za-z0-9_-]{8,64}
    is refused with 400."""
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod

    app = app_mod.build_app()
    async with _client(app) as client:
        resp = await client.post(
            "/run",
            json={"task": "noop", "request_id": "bad id with spaces!"},
        )
    assert resp.status_code == 400
    assert "invalid request_id" in resp.json()["detail"]


async def test_run_rejects_short_request_id(
    monkeypatch: pytest.MonkeyPatch,
):
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod

    app = app_mod.build_app()
    async with _client(app) as client:
        resp = await client.post(
            "/run", json={"task": "noop", "request_id": "abc"},
        )
    assert resp.status_code == 400


async def test_run_returns_409_when_request_id_already_active(
    monkeypatch: pytest.MonkeyPatch,
):
    """Duplicate-dispatch guard: a worker retry with the same
    request_id while the original run is still active gets a clean
    409 (classified transient writer-side → the retry waits) instead
    of silently starting a second concurrent agent run."""
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod

    mgr = MagicMock()
    mgr.is_request_active_anywhere = AsyncMock(return_value=True)
    monkeypatch.setattr(
        app_mod, "get_background_job_manager", lambda: mgr,
    )

    app = app_mod.build_app()
    async with _client(app) as client:
        resp = await client.post(
            "/run",
            json={"task": "noop", "request_id": "a" * 32},
        )
    assert resp.status_code == 409
    assert "already active" in resp.json()["detail"]
    mgr.is_request_active_anywhere.assert_awaited_once_with("a" * 32)
