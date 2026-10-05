"""The Session Archive API: whose archive it answers about, and what it refuses.

The service itself is tested in ``tests/session/test_session_archive.py``; what
matters here is the layer on top of it -- that every route resolves the
REQUESTING user and passes that user id down, and that an ArchiveError leaves
the panel with a readable answer instead of a 500.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.auth import database
from agent_system.auth.models import UserCreate, UserRole
from agent_system.auth.security import create_access_token
from agent_system.config.models import (
    AgentSystemConfig,
    AuthConfig,
    SessionArchiveConfig,
    ToolServerConfig,
)
from agent_system.services.session_archive import ArchiveBusy, ArchiveError, ArchiveNotFound
from plugins.session_archive.plugin import PLUGIN_FACTORY

PASSWORD = "correct-horse"


class FakeArchive:
    """Records which user id each call was made for."""

    retention_days = 30

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[str, Any]] = []

    async def list_archived(self, user_id):
        self.calls.append(("list", user_id))
        return [{"session_id": "root_x", "title": "An old one", "session_count": 7}]

    async def restore(self, user_id, root_session_id):
        self.calls.append(("restore", (user_id, root_session_id)))
        if self.error:
            raise self.error
        return {"session_id": root_session_id, "restored": 7, "title": "An old one"}

    async def forget(self, user_id, root_session_id):
        self.calls.append(("forget", (user_id, root_session_id)))
        if self.error:
            raise self.error
        return {"session_id": root_session_id, "sessions": 7}

    async def archive_user(self, user_id, *, dry_run=False, retention_days=None):
        self.calls.append(("sweep", (user_id, dry_run)))
        if self.error:
            raise self.error

        class Report:
            def as_dict(self):
                return {"user_id": user_id, "trees": 1, "dry_run": dry_run}

        return Report()


@pytest.fixture
def db(tmp_path, monkeypatch):
    users = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", users)
    for name in ("ada", "bob"):
        users.create_user(UserCreate(
            username=name, email=f"{name}@example.com", password=PASSWORD, role=UserRole.USER))
    return users


def client(archive: Any = None, auth_enabled: bool = True, sweep: bool = True) -> TestClient:
    plugin = PLUGIN_FACTORY(
        "session_archive",
        AgentSystemConfig(auth=AuthConfig(enabled=auth_enabled),
                          session_archive=SessionArchiveConfig(enabled=sweep)),
        ToolServerConfig(),
    )
    app = FastAPI()
    app.include_router(plugin.get_web_router())
    if archive is not None:
        app.state.session_archive = archive
    return TestClient(app, raise_server_exceptions=False)


def as_user(name: str, db) -> dict:
    account = db.get_user_by_username(name)
    token = create_access_token(
        {"sub": name, "user_id": account.id, "role": "user"},
        expires_delta=timedelta(minutes=5),
    )
    return {"Authorization": f"Bearer {token}"}


BASE = "/plugins/session_archive"


def test_the_listing_is_the_callers_own(db):
    archive = FakeArchive()
    response = client(archive).get(f"{BASE}/archived", headers=as_user("ada", db))

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "ada"
    assert body["retention_days"] == 30
    assert body["archived"][0]["session_id"] == "root_x"
    # The user id the service was asked about is the token's, not anything the
    # caller could put in the URL.
    assert archive.calls == [("list", "ada")]


@pytest.mark.parametrize("sweep", [True, False])
def test_the_listing_says_whether_the_sweep_runs(db, sweep):
    """With the sweep off, the panel must not promise that conversations move on their own."""
    response = client(FakeArchive(), sweep=sweep).get(f"{BASE}/archived")

    assert response.json()["sweep_enabled"] is sweep


def test_two_users_see_two_archives(db):
    archive = FakeArchive()
    api = client(archive)
    api.get(f"{BASE}/archived", headers=as_user("ada", db))
    api.get(f"{BASE}/archived", headers=as_user("bob", db))

    assert archive.calls == [("list", "ada"), ("list", "bob")]


def test_a_request_without_a_token_is_anonymous(db):
    archive = FakeArchive()
    assert client(archive).get(f"{BASE}/archived").status_code == 200
    assert archive.calls == [("list", "anonymous")]


def test_restore_passes_the_caller_and_the_tree(db):
    archive = FakeArchive()
    response = client(archive).post(
        f"{BASE}/archived/root_x/restore", headers=as_user("ada", db))

    assert response.status_code == 200
    assert response.json()["restored"] == 7
    assert archive.calls == [("restore", ("ada", "root_x"))]


def test_restoring_something_unknown_is_a_404(db):
    """Not there and cannot be done now are different answers."""
    archive = FakeArchive(ArchiveNotFound("No archived session root_x for user ada"))
    response = client(archive).post(
        f"{BASE}/archived/root_x/restore", headers=as_user("ada", db))

    assert response.status_code == 404


def test_a_restore_that_cannot_happen_is_readable(db):
    archive = FakeArchive(ArchiveError("2 session(s) of this tree are live again"))
    response = client(archive).post(
        f"{BASE}/archived/root_x/restore", headers=as_user("ada", db))

    assert response.status_code == 409
    assert "live again" in response.json()["detail"]


def test_forgetting_something_unknown_is_a_404(db):
    archive = FakeArchive(ArchiveNotFound("No archived session root_x for user ada"))
    response = client(archive).delete(
        f"{BASE}/archived/root_x", headers=as_user("ada", db))

    assert response.status_code == 404
    assert "No archived session" in response.json()["detail"]


def test_a_forget_while_the_index_is_held_is_readable(db):
    """A sweep of another process holds the archive index: a refusal, not a 500."""
    archive = FakeArchive(ArchiveBusy("the archive index of ada is held by another process"))
    response = client(archive).delete(
        f"{BASE}/archived/root_x", headers=as_user("ada", db))

    assert response.status_code == 409
    assert "held by another process" in response.json()["detail"]


def test_forget_reaches_the_service(db):
    archive = FakeArchive()
    response = client(archive).delete(
        f"{BASE}/archived/root_x", headers=as_user("ada", db))

    assert response.status_code == 200
    assert archive.calls == [("forget", ("ada", "root_x"))]


def test_a_sweep_by_hand_is_for_the_caller_only(db):
    archive = FakeArchive()
    response = client(archive).post(f"{BASE}/sweep", headers=as_user("bob", db))

    assert response.status_code == 200
    assert response.json()["trees"] == 1
    assert archive.calls == [("sweep", ("bob", False))]


def test_a_sweep_while_one_runs_is_a_refusal_not_a_failure(db):
    """The panel's button sits next to a sweep that may be running."""
    archive = FakeArchive(ArchiveError("a sweep for ada is already running"))

    response = client(archive).post(f"{BASE}/sweep", headers=as_user("ada", db))

    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_a_dry_sweep_says_so(db):
    archive = FakeArchive()
    response = client(archive).post(
        f"{BASE}/sweep?dry_run=true", headers=as_user("ada", db))

    assert response.json()["dry_run"] is True
    assert archive.calls == [("sweep", ("ada", True))]


def test_without_the_service_the_panel_is_told(db):
    """A CLI-only or half-started app has no archive -- say so, do not crash."""
    response = client(archive=None).get(f"{BASE}/archived")

    assert response.status_code == 503
    assert "not available" in response.json()["detail"]
