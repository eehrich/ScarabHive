"""Files an agent hands to the person in a session: kept with the session, served to whoever may see it, and gone
with it (services/session_files.py, GET /api/sessions/<id>/files/<name>).

Before, a picture an agent made reached nobody but the model: the chat draws no image, and no address served a
file the agent wrote.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system import app_state
from agent_system.api.dependencies import get_session_manager
from agent_system.api.session_endpoints import session_router
from agent_system.auth.models import UserRole
from agent_system.services import session_files
from tests.session_owners import two_users, user, viewed_by

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


@pytest.fixture
def served(tmp_path, monkeypatch):
    two_users(tmp_path, monkeypatch)
    manager = app_state.session_service.session_manager
    source = tmp_path / "made" / "forest background.png"
    source.parent.mkdir()
    source.write_bytes(PNG)
    root = session_files.root_beside(manager.storage_path)
    url, copy = session_files.hand_over(root, "alice", "s-alice", source)

    app = FastAPI()
    app.include_router(session_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    return TestClient(app), viewed_by(app), manager, url, copy, source


def test_the_person_gets_the_file_inline_and_as_a_download(served):
    client, viewer, _, url, _, _ = served
    viewer["user"] = user("alice")

    assert url == "/api/sessions/s-alice/files/forest%20background.png"
    shown = client.get(url)
    assert shown.status_code == 200 and shown.content == PNG
    assert shown.headers["content-type"] == "image/png"
    assert shown.headers["content-disposition"].startswith("inline")
    assert shown.headers["x-content-type-options"] == "nosniff"
    assert shown.headers["content-security-policy"].startswith("sandbox"), "never run as a page of this origin"
    assert client.get(url + "?download=true").headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("who", [user("bob"), None])
def test_nobody_else_gets_it(served, who):
    client, viewer, _, url, _, _ = served
    viewer["user"] = who

    assert client.get(url).status_code == 404


def test_an_admin_gets_another_users_file(served):
    client, viewer, _, url, _, _ = served
    viewer["user"] = user("root", UserRole.ADMIN)

    assert client.get(url).content == PNG


@pytest.mark.parametrize("path", ["/api/sessions/s-alice/files/nope.png", "/api/sessions/s-alice/files/..%2Fs-bob.json",
                                  "/api/sessions/s-alice/files/.hidden"])
def test_a_name_that_is_no_handed_over_file_is_not_found(served, path):
    client, viewer, _, _, _, _ = served
    viewer["user"] = user("alice")

    assert client.get(path).status_code == 404


def test_a_second_file_of_the_same_name_keeps_the_first(served):
    _, _, manager, url, copy, source = served
    source.write_bytes(PNG + b"v2")

    second, copy2 = session_files.hand_over(session_files.root_beside(manager.storage_path), "alice", "s-alice", source)

    assert second.endswith("forest%20background-2.png") and copy.read_bytes() == PNG and copy2.read_bytes() == PNG + b"v2"


def test_the_files_go_with_their_session(served):
    _, _, manager, _, copy, _ = served

    asyncio.run(manager.delete_session("alice", "s-alice"))

    assert not copy.parent.exists()


def test_the_terminal_finds_the_file_behind_the_address(served):
    _, _, manager, url, copy, _ = served

    assert session_files.find_by_url(session_files.root_beside(manager.storage_path), url) == copy


def test_an_address_cannot_climb_out_of_its_session(served, tmp_path):
    """The terminal decodes the address itself: %2F must not walk into another session's files."""
    _, _, manager, _, _, source = served
    root = session_files.root_beside(manager.storage_path)
    session_files.hand_over(root, "bob", "s-bob", source, "secret.png")

    assert session_files.find_by_url(root, "/api/sessions/s-alice/files/..%2F..%2Fbob%2Fs-bob%2Fsecret.png") is None
    assert session_files.find_by_url(root, "/api/sessions/s-bob/files/secret.png"), "fixture: the file is there"


def test_a_name_starting_with_a_dash_is_served(served, tmp_path):
    client, viewer, manager, _, _, source = served
    viewer["user"] = user("alice")

    url, _ = session_files.hand_over(session_files.root_beside(manager.storage_path), "alice", "s-alice", source,
                                     "-final.png")

    assert client.get(url).content == PNG, url


def test_outside_the_app_the_files_lie_beside_the_session_store_every_process_uses(tmp_path, monkeypatch):
    """The terminal and a woken run have no app: they must find what the app's chat serves."""
    monkeypatch.setattr(app_state, "session_service", None)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "store" / "sessions"))

    assert session_files.default_root() == tmp_path / "store" / "session_files"


def test_a_name_keeps_its_letters_without_their_accents(served):
    _, _, manager, _, _, source = served

    url, _ = session_files.hand_over(session_files.root_beside(manager.storage_path), "alice", "s-alice", source,
                                     "Ärger im Wald.png")

    assert url.endswith("/Arger%20im%20Wald.png"), url
