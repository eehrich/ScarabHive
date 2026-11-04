"""What a refused or malformed request leaves behind: nothing.

- A /run carrying files whose session another process holds answers with an
  error event -- and its uploads, its request ownership entry and its attended
  mark go with it: the stream owns that cleanup once it is handed out.
- /events refused the same way lets go of the request ownership it registered.
- A JSON body that parses but is no object is a 400, not the AttributeError's 500.
- POST /sessions makes a session that is the caller's from the start.
- /chat/command checks the session's owner against the tracker of the agent the
  command runs on, not the entry agent's.

Drives the real build_app over ASGI against a temp session store, auth off.
"""
from __future__ import annotations

import tempfile
from types import SimpleNamespace

import httpx
import pytest

from agent_system import app_state
from agent_system.config.models import SessionPresenceConfig
from agent_system.core import request_context
from agent_system.servers.agent.server import Agent
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService

pytestmark = pytest.mark.anyio

USER = "anonymous"  # auth is off


def _disable_auth(monkeypatch):
    from agent_system.config.models import AuthConfig

    class _DisabledAuth:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _DisabledAuth(), raising=False)


@pytest.fixture
def api(tmp_path, monkeypatch):
    from agent_system import app as app_mod

    _disable_auth(monkeypatch)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path / "sessions"))
    app = app_mod.build_app()
    app.state.config.session_presence = SessionPresenceConfig(enabled=True)
    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    monkeypatch.setattr(app_state, "session_service", SessionService(manager))
    return SimpleNamespace(app=app, manager=manager, tmp=tmp_path)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _busy(monkeypatch):
    """Every session is in another process's hands from here on."""
    from agent_system.core.session_presence import SessionBusy, SessionPresence

    def hold(self, session_id, user_id, agent_name):
        raise SessionBusy(session_id, "other_agent")

    monkeypatch.setattr(SessionPresence, "hold", hold)


def _no_run(monkeypatch):
    ran = []

    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        ran.append(task)
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", run_events)
    return ran


async def _stored(api, session_id="s1"):
    await api.manager.create_session(
        user_id=USER, session_id=session_id, agent_name="chat_agent", llm_profile="default")


async def test_a_refused_run_with_files_removes_its_uploads_and_lets_go_of_the_request(api, monkeypatch):
    await _stored(api)
    ran = _no_run(monkeypatch)
    _busy(monkeypatch)
    upload_dir = api.tmp / "upload"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: (upload_dir.mkdir(), str(upload_dir))[1])

    async with _client(api.app) as client:
        response = await client.post(
            "/run", params={"session_id": "s1", "request_id": "refusedrun1"}, data={"task": "go"},
            files={"files": ("notes.txt", b"some notes", "text/plain")}, timeout=60.0)

    assert response.status_code == 200, response.text
    assert "another process" in response.text
    assert ran == [], "the refused run reached the agent"
    assert not upload_dir.exists(), "the uploads of a refused run stayed on disk"
    assert "refusedrun1" not in request_context.request_user_map, "the request's owner entry leaked"
    assert not request_context.run_is_attended("refusedrun1")


async def test_a_refused_events_run_lets_go_of_the_request(api, monkeypatch):
    await _stored(api)
    _no_run(monkeypatch)
    _busy(monkeypatch)

    async with _client(api.app) as client:
        response = await client.get(
            "/events", params={"task": "go", "session_id": "s1", "request_id": "refusedevt1"}, timeout=60.0)

    assert "another process" in response.text
    assert "refusedevt1" not in request_context.request_user_map, "the request's owner entry leaked"


@pytest.mark.parametrize("method, path", [
    ("POST", "/events"),
    ("POST", "/events/r1/append"),
    ("POST", "/sessions/s1/append"),
    ("POST", "/chat/resolve"),
])
async def test_a_body_that_is_no_object_is_a_bad_request(api, method, path):
    async with _client(api.app) as client:
        response = await client.request(method, path, json=["not", "an", "object"], timeout=60.0)

    assert response.status_code == 400, response.text
    assert "JSON object" in response.text


async def test_a_new_session_id_is_the_callers_from_the_start(api):
    async with _client(api.app) as client:
        response = await client.post("/sessions", timeout=60.0)

    assert response.status_code == 200, response.text
    sid = response.json()["session_id"]
    tracker = api.app.state.agent._session_tracker
    assert tracker.has_session(sid)
    assert (tracker.get_session_metadata(sid) or {}).get("user_id") == USER


async def test_a_plugin_command_checks_the_owner_against_its_own_agents_tracker(api, monkeypatch):
    from agent_system.api.app_context import AppContext

    asked = []

    async def verify(self, sid, current_user, tracker=None):
        asked.append((sid, tracker))

    monkeypatch.setattr(AppContext, "verify_session_owner", verify)
    entry = api.app.state.agent

    async with _client(api.app) as client:
        await client.post("/chat/command", json={"name": "no-such-command", "payload": "",
                                                 "session_id": "s1", "agent_name": entry.name}, timeout=60.0)

    assert asked == [("s1", entry._session_tracker)]

