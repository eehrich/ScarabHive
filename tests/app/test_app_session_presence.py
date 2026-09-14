"""Session presence in the API (core/session_presence.py).

The API saves a session after its run -- /run right after it, the streams in
their finally -- so it holds the session until that save: a woken run must not
start in between and have its turn overwritten. An append to a session no
request runs restores the file first, because a woken run may have continued
the session meanwhile.

Drives the real build_app over ASGI against a temp session store; the agent's
run is the only fake.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from agent_system.config.models import SessionPresenceConfig
from agent_system.core.session_presence import presence_for
from agent_system.llm.models import ChatMessage
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
    """The app on a temp store with presence on. ``at_save`` records, for every
    save, whether the session was held at that moment."""
    from agent_system import app as app_mod

    _disable_auth(monkeypatch)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))
    app = app_mod.build_app()
    app.state.config.session_presence = SessionPresenceConfig(enabled=True)
    manager = SessionManager(storage_path=str(tmp_path))
    service = SessionService(manager)
    monkeypatch.setattr(app_mod, "_session_service", service)  # never the real data/sessions
    presence = presence_for(app.state.config)
    at_save = []
    save = service.save_session

    async def recording_save(agent, user_id, session_id, *args, **kwargs):
        at_save.append((session_id, (presence.get(session_id, user_id) or {}).get("status")))
        return await save(agent, user_id, session_id, *args, **kwargs)

    monkeypatch.setattr(service, "save_session", recording_save)
    return SimpleNamespace(app=app, manager=manager, presence=presence, at_save=at_save)


async def _stored(api, session_id="s1", messages=()):
    session = await api.manager.create_session(
        user_id=USER, session_id=session_id, agent_name="chat_agent", llm_profile="default")
    if messages:
        session["messages"] = list(messages)
        await api.manager.save_session(session)


def _fake_run(monkeypatch, start_session=None):
    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        if start_session:
            yield {"type": "start", "session_id": start_session}
        yield {"type": "final", "summary": "done"}
        yield {"type": "end"}

    monkeypatch.setattr(Agent, "run_events", run_events)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_run_holds_its_session_through_the_save_after_it(api, monkeypatch):
    from agent_system.servers.agent import result_utils

    await _stored(api)

    async def run(agent, task, **kwargs):
        return {"summary": "done"}

    monkeypatch.setattr(result_utils, "collect_final_result", run)
    async with _client(api.app) as client:
        response = await client.post("/run", json={"task": "go", "session_id": "s1"}, timeout=60.0)

    assert response.status_code == 200, response.text
    assert api.at_save == [("s1", "running")]
    assert api.presence.get("s1", USER)["status"] == "idle"


async def test_run_with_files_holds_its_session_through_the_save_after_it(api, monkeypatch):
    await _stored(api)
    _fake_run(monkeypatch, start_session="s1")

    async with _client(api.app) as client:
        response = await client.post(
            "/run", params={"session_id": "s1"}, data={"task": "go"},
            files={"files": ("notes.txt", b"some notes", "text/plain")}, timeout=60.0)

    assert response.status_code == 200, response.text
    assert api.at_save == [("s1", "running")]
    assert api.presence.get("s1", USER)["status"] == "idle"


async def test_events_holds_its_session_through_the_save_after_the_job(api, monkeypatch):
    await _stored(api)
    _fake_run(monkeypatch)  # no start event: the id is known up front

    async with _client(api.app) as client:
        response = await client.get("/events", params={"task": "go", "session_id": "s1"}, timeout=60.0)

    assert response.status_code == 200, response.text
    assert api.at_save == [("s1", "running")]
    assert api.presence.get("s1", USER)["status"] == "idle"


async def test_events_on_a_new_session_holds_it_from_the_start_event(api, monkeypatch):
    _fake_run(monkeypatch, start_session="fresh1")

    async with _client(api.app) as client:
        response = await client.get("/events", params={"task": "go"}, timeout=60.0)

    assert response.status_code == 200, response.text
    assert api.at_save == [("fresh1", "running")]
    assert api.presence.list_for_user(USER) == [], "still held after the stream"


async def test_an_append_keeps_what_another_process_wrote_and_holds_through_its_save(api):
    await _stored(api, messages=[{"role": "user", "content": "first question"}])
    api.app.state.agent._session_tracker.set_session_messages(
        "s1", [ChatMessage(role="user", content="first question")])
    await asyncio.sleep(0.05)  # file times on Windows advance in ~16 ms steps
    # A woken run continues the session from disk meanwhile.
    woken = SessionManager(storage_path=str(api.manager.storage_path))
    session = await woken.load_session(USER, "s1")
    session["messages"].append({"role": "assistant", "content": "answer of the woken run"})
    await woken.save_session(session)

    async with _client(api.app) as client:
        response = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)

    assert response.status_code == 200, response.text
    assert api.at_save == [("s1", "running")]
    stored = await woken.load_session(USER, "s1", bypass_cache=True)
    assert [m["content"] for m in stored["messages"]] == [
        "first question", "answer of the woken run", "follow-up"]


async def test_an_append_keeps_what_this_process_has_not_saved_yet(api):
    # The other way round: the run is over, its answer is in memory and the
    # save is still to come. Re-reading the file here would drop the answer.
    await _stored(api, messages=[{"role": "user", "content": "first question"}])
    api.app.state.agent._session_tracker.set_session_messages("s1", [
        ChatMessage(role="user", content="first question"),
        ChatMessage(role="assistant", content="the answer of the run")])

    async with _client(api.app) as client:
        response = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)

    assert response.status_code == 200, response.text
    stored = await api.manager.load_session(USER, "s1", bypass_cache=True)
    assert [m["content"] for m in stored["messages"]] == [
        "first question", "the answer of the run", "follow-up"]


async def test_an_append_keeps_the_longer_conversation_when_the_file_is_a_stranger(api):
    # The manager's cache is bounded (TTL, LRU), so "no record of this file" is
    # not "another process wrote it" -- re-reading on that would drop the run's
    # answer just the same.
    await _stored(api, messages=[{"role": "user", "content": "first question"}])
    api.app.state.agent._session_tracker.set_session_messages("s1", [
        ChatMessage(role="user", content="first question"),
        ChatMessage(role="assistant", content="the answer of the run")])
    api.manager.clear_cache()

    async with _client(api.app) as client:
        response = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)

    assert response.status_code == 200, response.text
    stored = await api.manager.load_session(USER, "s1", bypass_cache=True)
    assert [m["content"] for m in stored["messages"]] == [
        "first question", "the answer of the run", "follow-up"]


async def test_a_session_that_cannot_be_read_is_not_left_held(api, monkeypatch):
    # Everything between taking the hold and handing it back has to let go
    # again: in this long-lived process a hold nobody releases refuses every
    # later run of that session, and no wake can reach it either.
    from agent_system.services.session_manager import SessionPermissionError
    from agent_system.services.session_service import SessionService

    await _stored(api, messages=[{"role": "user", "content": "first question"}])
    api.manager.clear_cache()  # no stamp, so the file is read -- and that is what fails

    async def refuse(self, agent, user_id, session_id):
        raise SessionPermissionError("not yours")

    monkeypatch.setattr(SessionService, "load_and_restore_session", refuse)

    async with _client(api.app) as client:
        response = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)

    assert response.status_code == 500, response.text
    assert api.presence.list_for_user(USER) == [], "the session is still held after the error"


def _busy(monkeypatch):
    """Every session is in another process's hands from here on."""
    from agent_system.core.session_presence import SessionBusy, SessionPresence

    def hold(self, session_id, user_id, agent_name):
        raise SessionBusy(session_id, "other_agent")

    monkeypatch.setattr(SessionPresence, "hold", hold)


async def test_a_run_on_a_session_another_process_has_is_refused_and_force_runs_it(
        api, monkeypatch):
    from agent_system.servers.agent import result_utils

    await _stored(api)
    ran = []

    async def run(agent, task, **kwargs):
        ran.append(task)
        return {"summary": "done"}

    monkeypatch.setattr(result_utils, "collect_final_result", run)
    _busy(monkeypatch)

    async with _client(api.app) as client:
        refused = await client.post("/run", json={"task": "go", "session_id": "s1"}, timeout=60.0)
        forced = await client.post(
            "/run", json={"task": "go", "session_id": "s1", "force": True}, timeout=60.0)

    assert refused.status_code == 409 and "another process" in refused.text
    assert forced.status_code == 200, forced.text
    assert ran == ["go"], "the refused run reached the agent anyway"


async def test_events_refuses_a_session_another_process_has(api, monkeypatch):
    await _stored(api)
    _fake_run(monkeypatch)
    _busy(monkeypatch)

    async with _client(api.app) as client:
        response = await client.get(
            "/events", params={"task": "go", "session_id": "s1"}, timeout=60.0)

    assert "another process" in response.text
    assert api.at_save == [], "the job ran on it anyway"


async def test_a_session_deleted_in_this_process_takes_no_run_and_no_append(api, monkeypatch):
    """Its saves would not write it again: a run or an append on it would be lost without a word."""
    from agent_system.servers.agent import result_utils

    await _stored(api, messages=[{"role": "user", "content": "first question"}])
    ran = []

    async def run(agent, task, **kwargs):
        ran.append(task)
        return {"summary": "done"}

    monkeypatch.setattr(result_utils, "collect_final_result", run)
    _fake_run(monkeypatch)
    await api.manager.delete_session(USER, "s1")

    async with _client(api.app) as client:
        refused = await client.post("/run", json={"task": "go", "session_id": "s1", "force": True}, timeout=60.0)
        events = await client.get("/events", params={"task": "go", "session_id": "s1"}, timeout=60.0)
        append = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)

    assert refused.status_code == 409 and "has been deleted" in refused.text, refused.text
    assert "has been deleted" in events.text, events.text
    assert append.status_code == 409 and "has been deleted" in append.text, append.text
    assert ran == [] and api.at_save == [], "a run reached the deleted session anyway"


async def test_deleting_a_session_cancels_its_runs(api, monkeypatch):
    """Nothing writes a deleted session again: its runs -- another tab's too -- would go on for nothing. (The
    session router is mounted with authentication only, so its endpoint is called as the router calls it.)"""
    from agent_system.api.session_endpoints import delete_session
    from agent_system.services.background_job_manager import BackgroundJobManager

    await _stored(api)
    asked = []

    async def cancel_session(self, session_id):
        asked.append((session_id, api.manager.is_deleted(session_id)))
        return ["r1"]

    monkeypatch.setattr(BackgroundJobManager, "cancel_session", cancel_session)
    answer = await delete_session("s1", current_user=None, session_manager=api.manager, create_backup=False)

    assert answer == {"status": "deleted", "session_id": "s1", "cancelled_requests": ["r1"]}
    assert asked == [("s1", True)], "the runs were not cancelled once the session was gone"


async def test_an_append_to_a_session_another_process_has_is_refused(api, monkeypatch):
    await _stored(api, messages=[{"role": "user", "content": "first question"}])
    _busy(monkeypatch)

    async with _client(api.app) as client:
        response = await client.post(
            "/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)

    assert response.status_code == 409, response.text
