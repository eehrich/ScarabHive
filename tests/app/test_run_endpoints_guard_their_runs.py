"""The run endpoints act only for the run's owner, and lose no message on the way.

- Status, cancel and a mid-run append are given nothing but a request id. Anyone
  signed in who learned one could read another user's run, stop it, or put words
  into it -- which the run then acts on with its owner's tools. An id reaches more
  than a run of its own -- a sub-run belongs to its caller, a cancel stops every
  run whose id extends it -- and is refused for any of those.
- A message appended to a session a run of this process has goes to that run, or
  is refused. Written into the session beside the run, it was answered "appended"
  and gone at the run's next save.
- GET /events with no task starts nothing: the chat's reconnect URL carries none,
  and an id whose job is gone would have started an empty turn.

Drives the real build_app with auth ON, signed in as the real accounts of
data/users.db (read-only), the way tests/app/test_session_resolve_endpoint.py does.
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid

import httpx
import pytest

from agent_system.auth.security import create_access_token
from agent_system.servers.agent.server import Agent
from live_accounts import token_generation
from agent_system.services.background_job_manager import get_background_job_manager

pytestmark = pytest.mark.anyio

DEV_SECRET = "published-signing-key-replace-with-your-own-0000000000"


def _account(role_clause):
    try:
        with sqlite3.connect("file:data/users.db?mode=ro", uri=True) as db:
            row = db.execute("select id, username, role from users "
                             f"where is_active = 1 and {role_clause} limit 1").fetchone()
    except sqlite3.Error as e:
        pytest.skip(f"no user store to sign a token against: {e}")
    if not row:
        pytest.skip(f"no account where {role_clause}")
    return row


def _headers(row):
    return {"Authorization": "Bearer " + create_access_token(
        {"sub": row[1], "user_id": row[0], "role": row[2], "gen": token_generation(row[0])},
        secret_key=DEV_SECRET, algorithm="HS256")}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def user():
    return _account("role != 'admin'")


@pytest.fixture
def admin():
    return _account("role = 'admin'")


@pytest.fixture
def app(tmp_path, monkeypatch):
    from agent_system import app as app_mod
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    return app_mod.build_app()


async def _a_waiting_job(owner, session_id=None, agent_name="default", request_id=None):
    """A job of ``owner`` that runs until the returned event is set."""
    done = asyncio.Event()

    async def runner():
        yield {"type": "status", "message": "working"}
        await done.wait()

    request_id = request_id or f"guard{uuid.uuid4().hex[:10]}"
    await get_background_job_manager().create_job(
        request_id=request_id, user_id=owner, agent_name=agent_name,
        session_id=session_id, agent_runner=runner)
    return request_id, done


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_another_users_run_can_be_neither_read_nor_stopped_nor_written_to(app, user, admin):
    request_id, done = await _a_waiting_job(owner=f"not-{user[1]}")
    stopped = []
    manager = get_background_job_manager()

    async def recording_cancel(rid, force_timeout=0.0):
        stopped.append(rid)
        return True

    try:
        async with _client(app) as client:
            status = await client.get(f"/api/requests/{request_id}/status", headers=_headers(user))
            cancel = await client.post(f"/api/requests/{request_id}/cancel", headers=_headers(user))
            append = await client.post(f"/events/{request_id}/append", headers=_headers(user),
                                       json={"content": "do something else"})
            # an admin may look at anyone's run
            as_admin = await client.get(f"/api/requests/{request_id}/status", headers=_headers(admin))
        assert (status.status_code, cancel.status_code, append.status_code) == (403, 403, 403), \
            (status.text, cancel.text, append.text)
        assert as_admin.status_code == 200 and as_admin.json()["status"] == "running", as_admin.text
    finally:
        done.set()


async def test_the_owner_reaches_their_own_run(app, user):
    request_id, done = await _a_waiting_job(owner=user[1])
    try:
        async with _client(app) as client:
            status = await client.get(f"/api/requests/{request_id}/status", headers=_headers(user))
        assert status.status_code == 200 and status.json()["status"] == "running", status.text
    finally:
        done.set()


async def test_a_message_for_a_session_a_run_has_goes_to_that_run(app, admin, monkeypatch):
    session_id = f"s{uuid.uuid4().hex[:10]}"
    request_id, done = await _a_waiting_job(owner=admin[1], session_id=session_id)
    handed = []

    async def append_user_message(self, rid, content):
        handed.append((rid, content))
        return True

    monkeypatch.setattr(Agent, "append_user_message", append_user_message)
    try:
        async with _client(app) as client:
            response = await client.post(f"/sessions/{session_id}/append", headers=_headers(admin),
                                         json={"content": "and one more thing"})
        assert response.status_code == 200, response.text
        assert handed == [(request_id, "and one more thing")], "the message did not reach the run"
    finally:
        done.set()


async def test_a_message_the_run_no_longer_takes_is_refused_not_dropped(app, admin, monkeypatch):
    session_id = f"s{uuid.uuid4().hex[:10]}"
    _, done = await _a_waiting_job(owner=admin[1], session_id=session_id)
    written = []

    async def finishing(self, rid, content):
        return False

    async def append_to_session(self, sid, content):
        written.append(content)
        return True

    monkeypatch.setattr(Agent, "append_user_message", finishing)
    monkeypatch.setattr(Agent, "append_to_session", append_to_session)
    try:
        async with _client(app) as client:
            response = await client.post(f"/sessions/{session_id}/append", headers=_headers(admin),
                                         json={"content": "too late"})
        assert response.status_code == 409, response.text
        assert not written, "the message was written beside the run, where its next save drops it"
    finally:
        done.set()


async def test_a_reconnect_to_a_job_that_is_gone_starts_nothing(app, admin, monkeypatch):
    started = []

    async def run_events(self, task, *args, **kwargs):
        started.append(task)
        yield {"type": "final", "summary": "an empty turn"}

    monkeypatch.setattr(Agent, "run_events", run_events)
    async with _client(app) as client:
        response = await client.get(f"/events?task=&request_id=gone{uuid.uuid4().hex[:8]}",
                                    headers=_headers(admin))
    assert response.status_code == 404, response.text
    assert not started, "a reconnect without a job started a run"


async def test_a_session_another_users_run_holds_takes_no_message_from_anyone_else(app, user, monkeypatch):
    # The run's session is not saved yet: no tracker the check reads and no file names its owner.
    session_id = f"s{uuid.uuid4().hex[:10]}"
    _, done = await _a_waiting_job(owner=f"not-{user[1]}", session_id=session_id)
    handed = []

    async def append_user_message(self, rid, content):
        handed.append(rid)
        return True

    monkeypatch.setattr(Agent, "append_user_message", append_user_message)
    try:
        async with _client(app) as client:
            response = await client.post(f"/sessions/{session_id}/append", headers=_headers(user),
                                         json={"content": "do something else"})
        assert response.status_code == 403, response.text
        assert not handed, "the message reached another user's run"
    finally:
        done.set()


async def test_a_message_for_a_run_without_a_job_goes_to_the_agent_it_runs_on(app, admin, monkeypatch):
    # A /run with files or a sub-agent's session: no job, only the agent's session lock says so.
    from agent_system import app as app_mod

    default_name = app.state.agent.name
    runs_it = next((a for a in map(app_mod._app_registry.get, app_mod._app_registry.list())
                    if isinstance(a, Agent) and a.name != default_name), None)
    if runs_it is None:
        pytest.skip("no registered agent besides the default one")
    session_id = f"s{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(runs_it._session_tracker, "active_sessions", lambda: {session_id: "nojob-run"})
    monkeypatch.setattr(runs_it._session_tracker, "get_session_metadata",
                        lambda sid: {"user_id": admin[1]} if sid == session_id else None)
    handed = []

    async def append_user_message(self, rid, content):
        handed.append((self.name, rid))
        return True

    monkeypatch.setattr(Agent, "append_user_message", append_user_message)
    async with _client(app) as client:
        response = await client.post(f"/sessions/{session_id}/append", headers=_headers(admin),
                                     json={"content": "and one more thing"})
    assert response.status_code == 200, response.text
    assert handed == [(runs_it.name, "nojob-run")], f"handed to {handed}"


async def test_a_request_without_a_job_is_still_its_owners(app, user):
    from agent_system.core.request_context import register_request_user

    request_id = f"nojob{uuid.uuid4().hex[:10]}"
    register_request_user(request_id, f"not-{user[1]}")
    async with _client(app) as client:
        status = await client.get(f"/api/requests/{request_id}/status", headers=_headers(user))
        cancel = await client.post(f"/api/requests/{request_id}/cancel", headers=_headers(user))
    assert (status.status_code, cancel.status_code) == (403, 403), (status.text, cancel.text)


async def test_a_sub_run_of_another_users_run_is_theirs_too(app, user):
    # A background sub-agent: its id is its caller's and `_…`, and the owner map forgets it
    # when its caller's turn ends -- it works on. A cancel of a segment of that id, which
    # nothing ever registered, stops every sub-agent of that call.
    from agent_system.core.cancellation import get_cancellation_manager

    request_id, done = await _a_waiting_job(owner=f"not-{user[1]}")
    sub_run = f"{request_id}_001_async_abc123"
    token = get_cancellation_manager().create_token(sub_run)
    try:
        async with _client(app) as client:
            status = await client.get(f"/api/requests/{sub_run}/status", headers=_headers(user))
            cancel = await client.post(f"/api/requests/{request_id}_001_async/cancel", headers=_headers(user))
        assert (status.status_code, cancel.status_code) == (403, 403), (status.text, cancel.text)
        assert not token.is_cancelled, "another user's sub-agent was stopped"
    finally:
        done.set()
        get_cancellation_manager().unregister_request(sub_run)


async def test_an_id_that_reaches_another_users_run_below_it_is_refused(app, user):
    # A caller may choose a run's id, `_` included: a cancel of what it extends -- an id
    # known to nobody -- stops it. A run with a job, and one without (a /run carrying files).
    from agent_system.core.request_context import register_request_user, release_request_user

    above = f"guard{uuid.uuid4().hex[:10]}"
    _, done = await _a_waiting_job(owner=f"not-{user[1]}", request_id=f"{above}_1")
    jobless = f"nojob{uuid.uuid4().hex[:10]}"
    register_request_user(f"{jobless}_1", f"not-{user[1]}")
    try:
        async with _client(app) as client:
            cancels = [await client.post(f"/api/requests/{rid}/cancel", headers=_headers(user))
                       for rid in (above, jobless)]
        assert [c.status_code for c in cancels] == [403, 403], [c.text for c in cancels]
    finally:
        done.set()
        release_request_user(f"{jobless}_1")


async def test_ones_own_run_is_read_and_written_to_whatever_runs_below_its_id(app, user, monkeypatch):
    # Status and append act on the id itself; only a cancel reaches the runs below it.
    mine, done_mine = await _a_waiting_job(owner=user[1])
    _, done_theirs = await _a_waiting_job(owner=f"not-{user[1]}", request_id=f"{mine}_1")

    async def append_user_message(self, rid, content):
        return True

    monkeypatch.setattr(Agent, "append_user_message", append_user_message)
    try:
        async with _client(app) as client:
            status = await client.get(f"/api/requests/{mine}/status", headers=_headers(user))
            append = await client.post(f"/events/{mine}/append", headers=_headers(user), json={"content": "and this"})
        assert (status.status_code, append.status_code) == (200, 200), (status.text, append.text)
    finally:
        done_mine.set()
        done_theirs.set()


async def test_a_message_through_a_finished_requests_session_reaches_the_run_on_the_default_agent(
        app, admin, monkeypatch):
    # The request named ran on another agent; the session's run now is one started without an
    # agent_name, on the app's default agent. Asked for "default", the resolver answered with
    # the agent it was handed -- the named request's.
    from agent_system import app as app_mod

    default_agent = app.state.agent
    other = next((a for a in map(app_mod._app_registry.get, app_mod._app_registry.list())
                  if isinstance(a, Agent) and a.name != default_agent.name), None)
    if other is None:
        pytest.skip("no registered agent besides the default one")
    session_id = f"s{uuid.uuid4().hex[:10]}"
    earlier = f"earlier{uuid.uuid4().hex[:8]}"

    async def finished():
        yield {"type": "final", "summary": "done"}

    manager = get_background_job_manager()
    job = await manager.create_job(request_id=earlier, user_id=admin[1], agent_name=other.name,
                                   session_id=session_id, agent_runner=finished)
    await job.task
    other._session_tracker._request_to_session[earlier] = session_id
    now, done = await _a_waiting_job(owner=admin[1], session_id=session_id, agent_name="default")
    handed = []

    async def append_user_message(self, rid, content):
        handed.append((self.name, rid))
        return self is default_agent and rid == now   # only the run that is going takes it

    monkeypatch.setattr(Agent, "append_user_message", append_user_message)
    try:
        async with _client(app) as client:
            response = await client.post(f"/events/{earlier}/append", headers=_headers(admin),
                                         json={"content": "and one more thing"})
        assert response.status_code == 200, (response.text, handed)
        assert handed[-1] == (default_agent.name, now), handed
    finally:
        done.set()
        other._session_tracker._request_to_session.pop(earlier, None)
