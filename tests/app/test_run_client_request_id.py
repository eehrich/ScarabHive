"""Tests for the client-supplied request_id on POST /run and /events.

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

2026-08-26: the same contract now holds for GET/POST /events (the
story_design dispatch path). _handle_events used to use a supplied id
ONLY as a reconnect lookup and silently minted a new one on the first
dispatch — every /events-dispatched run then lived under an id its
caller never learned, so cancel propagation 404'd ("agent_already_
gone") while the run kept burning tokens, and reconcile probes never
matched. The /events tests below pin the adoption and both guards.
"""

from __future__ import annotations

import asyncio

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


async def test_run_adopts_client_request_id(
    monkeypatch: pytest.MonkeyPatch,
):
    """The file's whole reason for existing: /run must RUN under the
    caller's id, not just validate it. Asserting only that the guard saw
    the id leaves the assignment itself untested — dropping it would keep
    every guard test green while the run went back to a minted id."""
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod

    app = app_mod.build_app()
    seen: list[str] = []

    async def _stub_run_events(task, request_id, session_id=None, **kwargs):
        seen.append(request_id)
        yield {"type": "status", "message": "stub-run"}

    monkeypatch.setattr(
        app.state.agent, "run_events", _stub_run_events, raising=True,
    )

    rid = "runrid-adopt-1234"
    async with _client(app) as client:
        resp = await client.post(
            "/run", json={"task": "noop", "request_id": rid}, timeout=30.0,
        )

    assert resp.status_code == 200
    assert seen == [rid], (
        f"run did not execute under the client-supplied id: {seen!r}"
    )


async def test_events_rejects_malformed_request_id(
    monkeypatch: pytest.MonkeyPatch,
):
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod

    app = app_mod.build_app()
    async with _client(app) as client:
        resp = await client.get("/events?task=noop&request_id=abc")
    assert resp.status_code == 400
    assert "invalid request_id" in resp.json()["detail"]


async def test_events_returns_409_when_request_id_active_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
):
    """An id that is live somewhere (registry agent, /run job) but not a
    reconnectable BackgroundJob must be refused, not adopted — otherwise
    two concurrent runs share ownership/cancellation keys."""
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod

    mgr = MagicMock()
    mgr.get_job = AsyncMock(return_value=None)  # no reconnect target
    mgr.is_request_active_anywhere = AsyncMock(return_value=True)
    monkeypatch.setattr(
        app_mod, "get_background_job_manager", lambda: mgr,
    )

    app = app_mod.build_app()
    async with _client(app) as client:
        resp = await client.get("/events?task=noop&request_id=" + "b" * 32)
    assert resp.status_code == 409
    assert "already active" in resp.json()["detail"]


async def test_events_adopts_client_request_id(
    monkeypatch: pytest.MonkeyPatch,
):
    """The core of the 2026-08 story_design cancel bug: a NEW /events run
    must be keyed under the caller-supplied request_id, so the caller's
    later cancel/status probes against that id actually hit this run.

    Production path end-to-end (routing → security → _handle_events →
    BackgroundJobManager.create_job); only the agent's run_events behind
    the job boundary is stubbed so no LLM is involved."""
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod

    app = app_mod.build_app()

    async def _stub_run_events(task, request_id, session_id=None, **kwargs):
        yield {"type": "status", "message": "stub-run"}

    monkeypatch.setattr(
        app.state.agent, "run_events", _stub_run_events, raising=True,
    )

    rid = "evrid-adopt-1234"
    lines: list[str] = []
    async with _client(app) as client:
        async with client.stream(
            "GET", f"/events?task=noop&request_id={rid}", timeout=10.0,
        ) as resp:
            assert resp.status_code == 200
            # Drain the (short, stubbed) stream so create_job has run.
            async for line in resp.aiter_lines():
                lines.append(line)

    # Prove the stub actually ran: without this the test would stay green
    # if a refactor made _get_agent_with_overrides hand out a different
    # agent instance — and would then quietly do a real LLM call.
    assert any("stub-run" in line for line in lines), (
        "run_events stub did not run — the test hit a different agent "
        f"instance; got: {lines!r}"
    )

    from agent_system.services.background_job_manager import (
        get_background_job_manager,
    )
    job = await get_background_job_manager().get_job(rid)
    assert job is not None, (
        "run was not keyed under the client-supplied request_id — "
        "cancel/status probes against jobs.request_id would all miss"
    )


async def test_events_post_adopts_client_request_id(
    monkeypatch: pytest.MonkeyPatch,
):
    """POST is the route the writer's story_design dispatcher actually
    uses (worker/exec.py _post_events_sse). GET and POST share
    _handle_events but each unpacks request_id on its own line, so the
    GET test cannot cover this one."""
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod

    app = app_mod.build_app()

    async def _stub_run_events(task, request_id, session_id=None, **kwargs):
        yield {"type": "status", "message": "stub-run"}

    monkeypatch.setattr(
        app.state.agent, "run_events", _stub_run_events, raising=True,
    )

    rid = "evrid-post-adopt-1"
    lines: list[str] = []
    async with _client(app) as client:
        async with client.stream(
            "POST", "/events",
            json={"task": "noop", "request_id": rid}, timeout=10.0,
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                lines.append(line)

    assert any("stub-run" in line for line in lines), (
        f"run_events stub did not run; got: {lines!r}"
    )
    from agent_system.services.background_job_manager import (
        get_background_job_manager,
    )
    assert await get_background_job_manager().get_job(rid) is not None, (
        "POST /events did not key the run under the client-supplied id"
    )


async def test_events_reconnects_to_running_job_instead_of_409(
    monkeypatch: pytest.MonkeyPatch,
):
    """Guard ORDER: the reconnect lookup must happen BEFORE the
    already-active check. A RUNNING BackgroundJob makes
    is_request_active_anywhere True, so validating first would answer
    every legitimate reconnect with 409 — and the writer's retry path
    depends on exactly this reconnect."""
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod
    from agent_system.services.background_job_manager import (
        get_background_job_manager,
    )

    app = app_mod.build_app()
    may_finish = asyncio.Event()
    rid = "evrid-reconnect-1"
    mgr = get_background_job_manager()

    # Safety net: on the reconnect fast path the agent is never invoked.
    # If the guard order regresses, the request falls through to the
    # normal path — this makes that fail loudly instead of firing a real
    # LLM call and hanging the suite.
    fresh_runs: list[str] = []

    async def _stub_run_events(task, request_id, session_id=None, **kwargs):
        fresh_runs.append(request_id)
        yield {"type": "status", "message": "stub-run"}

    monkeypatch.setattr(
        app.state.agent, "run_events", _stub_run_events, raising=True,
    )

    async def _long_runner():
        yield {"type": "status", "message": "first-run"}
        await may_finish.wait()

    job = await mgr.create_job(
        request_id=rid, user_id="anonymous", agent_name="default",
        session_id=None, agent_runner=_long_runner,
    )
    await asyncio.sleep(0)
    # Fixture check — without a LIVE job this test would prove nothing.
    assert job.status.value == "running"

    # httpx's ASGITransport buffers the whole response body, so the
    # request only returns once the SSE stream closes. Release the job a
    # moment after the request starts: it is still RUNNING when the
    # handler decides reconnect-vs-new, then finishes so the stream ends.
    async def _release_soon():
        await asyncio.sleep(0.3)
        may_finish.set()

    releaser = asyncio.create_task(_release_soon())
    try:
        async with _client(app) as client:
            resp = await client.get(
                f"/events?task=&request_id={rid}", timeout=20.0,
            )
        assert resp.status_code == 200, (
            "reconnect to a running job was refused — the already-active "
            "guard ran before the reconnect lookup"
        )
        assert "reconnect" in resp.text, (
            f"no reconnect event on the stream: {resp.text[:400]!r}"
        )
        assert fresh_runs == [], (
            "the request started a NEW run instead of reconnecting: "
            f"{fresh_runs!r}"
        )
    finally:
        releaser.cancel()
        may_finish.set()
        if not job.task.done():
            job.task.cancel()


async def test_events_surfaces_duplicate_refusal_instead_of_second_run(
    monkeypatch: pytest.MonkeyPatch,
):
    """When create_job refuses a duplicate (its CAS lost the race against
    a concurrent request), /events must say so on the stream instead of
    letting the exception escape mid-body.

    The refusal itself is contract-tested against the real manager in
    tests/services; here the manager is the stub because the race cannot
    be scheduled deterministically through HTTP."""
    _disable_auth(monkeypatch)
    from agent_system import app as app_mod
    from agent_system.services.background_job_manager import (
        DuplicateRequestIdError, get_background_job_manager,
    )

    app = app_mod.build_app()
    runs: list[str] = []

    async def _stub_run_events(task, request_id, session_id=None, **kwargs):
        runs.append(request_id)
        yield {"type": "status", "message": "stub-run"}

    monkeypatch.setattr(
        app.state.agent, "run_events", _stub_run_events, raising=True,
    )

    rid = "evrid-duplicate-1"
    real_manager = get_background_job_manager()

    async def _refuse(**kwargs):
        raise DuplicateRequestIdError(kwargs["request_id"])

    monkeypatch.setattr(real_manager, "create_job", _refuse, raising=True)

    body = ""
    async with _client(app) as client:
        async with client.stream(
            "GET", f"/events?task=noop&request_id={rid}", timeout=10.0,
        ) as resp:
            assert resp.status_code == 200  # headers were already sent
            async for line in resp.aiter_lines():
                body += line

    assert "already running" in body, (
        f"no refusal event reached the client: {body!r}"
    )
    assert runs == [], f"a second agent run started anyway: {runs!r}"
