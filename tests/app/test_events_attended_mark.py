"""A run is marked attended only when the client that starts it says so.

The mark is what tool_approval asks before it puts a call to a person: only a
client that shows the run's questions to the person who started it (the web
chat) sends ``attended``. Everything else that starts runs -- the writer's
dispatches over /events, the openai_api plugin, the CLIs -- reads the stream as
a program, and a question put there would wait for nobody.

Drives the real build_app (auth off, as tests/app/test_run_client_request_id.py
does); only the agent's run_events behind the endpoint is stubbed, and it records
what the mark said while the run ran.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from agent_system.core.request_context import release_run_attended, run_is_attended, set_run_attended

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _disable_auth(monkeypatch):
    """AuthConfig.enabled reads False for this test only (see test_run_client_request_id.py)."""
    from agent_system.config.models import AuthConfig

    class _DisabledAuth:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _DisabledAuth(), raising=False)


@pytest.fixture
def started(tmp_path, monkeypatch):
    """(app, marks): marks[request_id] is what run_is_attended said while that run ran."""
    _disable_auth(monkeypatch)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    from agent_system import app as app_mod

    app = app_mod.build_app()
    marks = {}

    async def run_events(task, request_id=None, session_id=None, **kwargs):
        marks[request_id] = run_is_attended(request_id)
        yield {"type": "status", "message": "stub-run"}

    monkeypatch.setattr(app.state.agent, "run_events", run_events, raising=True)
    return app, marks


def _rid() -> str:
    return f"attend{uuid.uuid4().hex[:12]}"


async def _read(app, method: str, url: str, **kwargs) -> int:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(method, url, timeout=15.0, **kwargs) as response:
            async for _ in response.aiter_lines():
                pass
            return response.status_code


async def test_the_chat_start_marks_the_run(started):
    app, marks = started
    rid = _rid()

    assert await _read(app, "POST", "/events", json={"task": "go", "request_id": rid, "attended": True}) == 200

    assert marks == {rid: True}


async def test_a_start_that_does_not_say_so_is_not_marked(started):
    """Only a JSON true counts: a program that sends the string "false" is no person."""
    app, marks = started
    plain, stringly = _rid(), _rid()

    await _read(app, "POST", "/events", json={"task": "go", "request_id": plain})
    await _read(app, "POST", "/events", json={"task": "go", "request_id": stringly, "attended": "false"})

    assert marks == {plain: False, stringly: False}


async def test_get_events_takes_the_flag(started):
    app, marks = started
    rid = _rid()

    await _read(app, "GET", f"/events?task=go&request_id={rid}&attended=true")

    assert marks == {rid: True}


async def test_a_reused_id_does_not_keep_an_old_mark(started):
    """A run whose stream ended while it still ran keeps its mark (nothing released
    it); a later run a program starts under the same id must not inherit it."""
    app, marks = started
    rid = _rid()
    set_run_attended(rid, True)
    try:
        await _read(app, "POST", "/events", json={"task": "go", "request_id": rid})
    finally:
        release_run_attended(rid)

    assert marks == {rid: False}


async def test_the_mark_goes_when_the_run_is_over(started):
    app, marks = started
    rid = _rid()

    await _read(app, "POST", "/events", json={"task": "go", "request_id": rid, "attended": True})

    assert marks == {rid: True}, "fixture: the run did not run marked"
    assert run_is_attended(rid) is False


@pytest.mark.parametrize("user, auth_enabled, expected", [
    ("signed in", True, True),
    ("anonymous", True, False),   # the answer route takes a sign-in
    (None, True, False),          # an endpoint that let the request through without a user
    (None, False, True),          # authentication off: one user, everyone is them
    ("anonymous", False, True),
])
def test_only_someone_who_can_answer_is_asked(user, auth_enabled, expected):
    """With authentication on, a question to nobody signed in could only time
    out. (The runs above take this predicate: with auth off here, a start that
    says attended is marked.)"""
    from agent_system.app import _asks_a_person
    from agent_system.auth.enforcement import AnonymousUser
    from agent_system.auth.models import User

    who = {"signed in": User(username="alice", email="alice@example.com", id=1,
                             created_at="2026-01-01T00:00:00Z"),
           "anonymous": AnonymousUser(), None: None}[user]

    assert _asks_a_person(True, who, auth_enabled) is expected
    assert _asks_a_person(False, who, auth_enabled) is False


async def test_a_start_refused_as_a_duplicate_leaves_no_mark(started, monkeypatch):
    """Refused before its run started (another one won the id): the mark is the
    run's to set, so a refused start neither sets one nor overwrites a live one's."""
    from agent_system.services.background_job_manager import (
        BackgroundJobManager, DuplicateRequestIdError)

    async def taken(self, request_id, **kwargs):
        raise DuplicateRequestIdError(request_id)

    monkeypatch.setattr(BackgroundJobManager, "create_job", taken)
    app, marks = started
    fresh, live = _rid(), _rid()
    set_run_attended(live, True)   # the run that holds the id, marked by its own start
    try:
        await _read(app, "POST", "/events", json={"task": "go", "request_id": fresh, "attended": True})
        await _read(app, "POST", "/events", json={"task": "go", "request_id": live})
        assert run_is_attended(live) is True, "a refused start took a live run's mark"
    finally:
        release_run_attended(live)

    assert marks == {}, "fixture: a refused start ran"
    assert run_is_attended(fresh) is False


@pytest.mark.parametrize("sent, expected", [("true", True), (None, False)])
async def test_a_run_with_files_takes_the_flag(started, sent, expected):
    app, marks = started
    rid = _rid()
    form = {"task": "read this", "request_id": rid, **({"attended": sent} if sent else {})}

    status = await _read(app, "POST", "/run", data=form,
                         files={"files": ("note.txt", b"some text", "text/plain")})

    assert status == 200
    assert marks == {rid: expected}
    assert run_is_attended(rid) is False, "the mark outlived the run"
