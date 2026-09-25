"""/run and /events open a session through SessionService.open_for_run.

- A new session starts on the agent's template_vars; /events used to skip
  that, so an agent relying on an initial value started without it there.
- Another user's session answers 403, and /events leaves no ownership entry.
- A session a run of this process has answers 409 before anything is touched:
  opening would reset that run's unsaved session, or hand it to someone else.
"""
from __future__ import annotations

import httpx
import pytest

from agent_system.core.request_context import request_user_map
from agent_system.servers.agent.server import Agent
from agent_system.services.background_job_manager import BackgroundJobManager
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService

pytestmark = pytest.mark.anyio
OWN_VARS = {"workflow_phase": "planning"}
ENDPOINTS = ["/run", "/events"]


@pytest.fixture
def api(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_system import app as app_mod
    from agent_system.config.models import AuthConfig

    class _Off:
        def __get__(self, obj, objtype=None):
            return False

        def __set__(self, obj, value):
            pass

    monkeypatch.setattr(AuthConfig, "enabled", _Off(), raising=False)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    app = app_mod.build_app()
    manager = SessionManager(storage_path=str(tmp_path))
    monkeypatch.setattr(app_mod, "_session_service", SessionService(manager))
    monkeypatch.setattr(app.state.agent.agent_config, "template_vars", dict(OWN_VARS))
    at_run = {}

    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        at_run[session_id] = dict(self._session_tracker.get_session_template_vars(session_id))
        yield {"type": "final", "summary": "done"}
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", run_events)
    return SimpleNamespace(app=app, manager=manager, at_run=at_run)


async def _call(app, endpoint, session_id, request_id=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        if endpoint == "/run":
            return await client.post("/run", json={"task": "go", "session_id": session_id}, timeout=60.0)
        params = {"task": "go", "session_id": session_id}
        if request_id:
            params["request_id"] = request_id
        return await client.get("/events", params=params, timeout=60.0)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_a_new_session_starts_on_the_agents_template_vars(api, endpoint):
    sid = f"new-{endpoint[1:]}"
    response = await _call(api.app, endpoint, sid)

    assert response.status_code == 200, response.text
    assert api.at_run == {sid: OWN_VARS}, f"{endpoint} ran without the agent's template_vars"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_another_users_session_answers_403_and_leaves_no_entry(api, endpoint):
    session = await api.manager.create_session(user_id="bob", session_id="s-bob",
                                               agent_name="chat_agent", llm_profile="default")
    session["messages"] = [{"role": "user", "content": "bob's"}]
    await api.manager.save_session(session)
    rid = f"rq403{endpoint[1:]}"

    response = await _call(api.app, endpoint, "s-bob", request_id=rid)

    assert response.status_code == 403, response.text
    assert not api.at_run, "a run started on another user's session"
    assert rid not in request_user_map, "the refused request stayed registered"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_a_session_a_run_here_has_is_not_reset_under_it(api, endpoint, monkeypatch):
    """Not saved yet, it reads as new -- but what the tracker holds is that run's."""
    async def running(self):
        return {"s-busy": {"user_id": "anonymous"}}
    monkeypatch.setattr(BackgroundJobManager, "active_sessions", running)
    tracker = api.app.state.agent._session_tracker
    tracker.set_session_template_vars("s-busy", {"book_id": 7})

    response = await _call(api.app, endpoint, "s-busy")

    assert response.status_code == 200, response.text
    assert api.at_run == {"s-busy": {"book_id": 7}}, "the running run's session was reset"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_another_users_running_session_answers_403(api, endpoint, monkeypatch):
    """Not on disk yet, so the store cannot say whose it is -- the run can."""
    async def running(self):
        return {"s-alice": {"user_id": "alice"}}
    monkeypatch.setattr(BackgroundJobManager, "active_sessions", running)
    tracker = api.app.state.agent._session_tracker
    tracker.set_session_metadata("s-alice", {"user_id": "alice", "agent_name": "x", "llm_profile": "p"})

    response = await _call(api.app, endpoint, "s-alice")

    assert response.status_code == 403, response.text
    assert not api.at_run
    assert tracker.get_session_metadata("s-alice")["user_id"] == "alice", "the run was handed to someone else"
