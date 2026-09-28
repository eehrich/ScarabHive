"""/run and /events open a session through SessionService.open_for_run.

- A new session starts on the agent's template_vars; /events used to skip
  that, so an agent relying on an initial value started without it there.
- Another user's session answers 403, and /events leaves no ownership entry.
- A session a run of this process has is not reset under it (not saved yet, it
  reads as new), and another user's such session answers 403: opening would
  hand that run's session to someone else.
- A session a run of this agent holds (its session lock) is not read back from
  disk under that run, by the opening or by the copy check after it; and a
  request whose run is refused at that lock saves nothing.
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


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_a_session_a_run_of_this_agent_holds_is_not_read_back_under_it(api, endpoint):
    """/run and /events open the session (open_for_run, in_use: the lock owner is listed as running) and bring its
    copy up to date (_claim_session) before their own run is refused at the agent's session lock. Neither may read
    it back under the run that has it: with the session gone from the manager's cache (a bounded LRU), the copy
    check compared lengths and read it back -- the run's copy replaced, its metadata naming the asker."""
    from agent_system.llm.models import ChatMessage

    session = await api.manager.create_session(user_id="anonymous", session_id="s-run", agent_name="chat_agent",
                                               llm_profile="default")
    session["messages"] = [{"role": "user", "content": "on disk"}]
    await api.manager.save_session(session)
    tracker = api.app.state.agent._session_tracker
    tracker.set_session_messages("s-run", [ChatMessage(role="user", content="the run's own copy")])
    metadata = {"user_id": "anonymous", "agent_name": "chat_agent", "llm_profile": "default"}
    tracker.set_session_metadata("s-run", metadata)
    assert await tracker.acquire_session_lock("s-run", "api_turn"), "fixture: the lock was not taken"
    api.manager.clear_cache()  # the LRU let go of it: the copy check has no stamp to go by
    try:
        await _call(api.app, endpoint, "s-run")
        messages = [m.content for m in tracker.get_session_messages("s-run")]
        now = tracker.get_session_metadata("s-run")
    finally:
        await tracker.release_session_lock("s-run", "api_turn")

    assert messages == ["the run's own copy"], "read back under the run that has it"
    assert now is metadata, "the asker replaced the run's metadata"


@pytest.mark.parametrize("endpoint", ["/run", "/run with files", "/events"])
async def test_a_run_refused_at_the_session_lock_saves_nothing(api, endpoint, monkeypatch):
    """Another run of this process has the session (its lock), so this request's run is refused (Agent.run_events,
    ``error_type`` SESSION_LOCKED). Saving the session after it -- /run always did, /events for a completed job --
    wrote what the tracker holds, which is the other run's live state (a tool call without its result, say)."""
    from agent_system import app as app_mod
    from agent_system.servers.agent.server import SESSION_LOCKED

    async def refused(self, task, request_id=None, session_id=None, **kwargs):
        yield {"type": "error", "message": f"Session {session_id} is currently locked by another request",
               "request_id": request_id, "error_type": SESSION_LOCKED}
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", refused)
    session = await api.manager.create_session(user_id="anonymous", session_id="s-busy", agent_name="chat_agent",
                                               llm_profile="default")
    session["messages"] = [{"role": "user", "content": "on disk"}]
    await api.manager.save_session(session)
    service, saves = app_mod._session_service, []
    save = service.save_session

    async def counted(*args, **kwargs):
        saves.append(args[2] if len(args) > 2 else kwargs.get("session_id"))
        return await save(*args, **kwargs)

    monkeypatch.setattr(service, "save_session", counted)
    if endpoint == "/run with files":
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            response = await client.post("/run", data={"task": "go", "session_id": "s-busy"},
                                         files={"files": ("notes.md", b"the notes", "text/markdown")}, timeout=60.0)
    else:
        response = await _call(api.app, endpoint, "s-busy")

    assert response.status_code == 200, response.text
    assert "locked" in response.text, "fixture: the run was not refused"
    assert "s-busy" not in saves, f"{endpoint} saved the session after its run was refused"
