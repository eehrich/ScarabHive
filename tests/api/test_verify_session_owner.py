"""AppContext.verify_session_owner: what it does with an id that is no session id, and with a lookup that fails.

The owner lookup on disk joins the id into a path, so a name no session can have
is the caller's mistake (400) before anything is looked up. And a lookup that
fails refuses: read as "no owner", it let anybody through whenever the store
could not be asked.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent_system import app_state
from agent_system.api import app_context as app_context_mod
from agent_system.api.app_context import AppContext

pytestmark = pytest.mark.anyio

ALICE = SimpleNamespace(username="alice")


class _NoJobs:
    async def active_sessions(self):
        return {}


class _Tracker:
    def __init__(self, metadata=None):
        self.metadata = metadata or {}

    def get_session_metadata(self, sid):
        return self.metadata.get(sid)


class _Store:
    def __init__(self, owner=None, error=None):
        self.owner, self.error, self.asked = owner, error, []

    def find_session_owner(self, sid):
        self.asked.append(sid)
        if self.error is not None:
            raise self.error
        return self.owner


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(app_context_mod, "get_background_job_manager", lambda: _NoJobs())
    return AppContext(app=None, config=None, agent=SimpleNamespace(_session_tracker=_Tracker()),
                      config_path="", security_enforcer=None)


def _store(monkeypatch, store):
    monkeypatch.setattr(app_state, "session_service", SimpleNamespace(session_manager=store))
    return store


@pytest.mark.parametrize("sid", ["../../etc/x", "a/b", "s1.json", ""])
async def test_an_id_no_session_can_have_is_refused_before_any_lookup(ctx, monkeypatch, sid):
    store = _store(monkeypatch, _Store(owner="alice"))

    with pytest.raises(HTTPException) as refused:
        await ctx.verify_session_owner(sid, ALICE)

    assert refused.value.status_code == 400
    assert store.asked == [], "the id went into a path lookup"


@pytest.mark.parametrize("sid", ["s1", "ephemeral-0b9c3c1e-7a1f-4b9e-9d2c-1f2e3d4c5b6a", "s1--coder-1a2b3c4d"])
async def test_the_ids_the_system_makes_pass_the_check(ctx, monkeypatch, sid):
    _store(monkeypatch, _Store(owner="alice"))

    await ctx.verify_session_owner(sid, ALICE)


async def test_a_lookup_that_fails_refuses_instead_of_letting_anybody_in(ctx, monkeypatch):
    _store(monkeypatch, _Store(error=PermissionError("unreadable")))

    with pytest.raises(HTTPException) as refused:
        await ctx.verify_session_owner("s1", ALICE)

    assert refused.value.status_code == 503


async def test_no_store_on_disk_yet_is_nobodys_session(ctx, monkeypatch):
    _store(monkeypatch, _Store(error=FileNotFoundError("no sessions directory yet")))

    await ctx.verify_session_owner("s1", ALICE)


async def test_another_users_session_on_disk_is_refused(ctx, monkeypatch):
    _store(monkeypatch, _Store(owner="bob"))

    with pytest.raises(HTTPException) as refused:
        await ctx.verify_session_owner("s1", ALICE)

    assert refused.value.status_code == 403


async def test_the_tracker_the_caller_names_answers_before_the_disk(ctx, monkeypatch):
    store = _store(monkeypatch, _Store(owner="alice"))

    with pytest.raises(HTTPException) as refused:
        await ctx.verify_session_owner("s1", ALICE, _Tracker({"s1": {"user_id": "bob"}}))

    assert refused.value.status_code == 403
    assert store.asked == []


async def test_without_auth_nothing_is_checked(ctx, monkeypatch):
    store = _store(monkeypatch, _Store(error=RuntimeError("never asked")))

    await ctx.verify_session_owner("../x", None)

    assert store.asked == []
