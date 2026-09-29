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


async def test_an_append_to_a_session_this_process_saved_and_let_go_of_reads_it_back(api):
    """A settled API turn (openai_api) saves its conversation and takes it out of the agent's tracker. The file has
    not moved since this process wrote it, so the claim does not read it again -- and the append found no session:
    a 404 for a conversation that is plainly there."""
    from agent_system import app as app_mod

    agent = api.app.state.agent
    tracker = agent._session_tracker
    tracker.set_session_messages("s1", [ChatMessage(role="user", content="first question"),
                                        ChatMessage(role="assistant", content="the answer")])
    tracker.set_session_metadata("s1", {"user_id": USER, "agent_name": agent.name, "llm_profile": "default"})
    assert await app_mod._session_service.save_session(agent, USER, "s1", agent.name, "default",
                                                       was_new_session=True)
    tracker.discard_session("s1")
    assert api.manager.changed_on_disk(USER, "s1") is False, "fixture: the claim would read the session again"

    async with _client(api.app) as client:
        response = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)

    assert response.status_code == 200, response.text
    stored = await api.manager.load_session(USER, "s1", bypass_cache=True)
    assert [m["content"] for m in stored["messages"]] == ["first question", "the answer", "follow-up"]


async def test_an_append_whose_save_fails_says_so_and_keeps_nothing(api, monkeypatch):
    """A save that comes back False was answered "appended" -- and the message stayed in memory, for the next
    run's save to write after all: a client that retried had it twice."""
    from agent_system import app as app_mod

    await _stored(api, messages=[{"role": "user", "content": "first question"}])

    async def fails(*args, **kwargs):
        return False

    monkeypatch.setattr(app_mod._session_service, "save_session", fails)
    async with _client(api.app) as client:
        response = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)

    assert response.status_code == 500, response.text
    messages = api.app.state.agent._session_tracker.get_session_messages("s1")
    assert [m.content for m in messages] == ["first question"], "the message stayed for a later save"


def _another_agent(name: str) -> Agent:
    """A second registered agent -- with its own SessionTracker, as every agent has."""
    from agent_system.config.models import (
        AgentConfig,
        AgentSystemConfig,
        LLMModelConfig,
        LLMProfile,
        LLMSystemConfig,
        ToolServerConfig,
    )
    from agent_system.tools.base import ToolServerRegistry

    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4", api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")}, default_profile="normal")
    server_config = ToolServerConfig(type="agent", enabled=True,
                                     agent_config=AgentConfig(max_steps=3, llm_profile="normal"))
    return Agent(name, AgentSystemConfig(llm_system=llm_system), server_config, ToolServerRegistry())


async def test_an_append_goes_to_the_agent_the_session_runs_on(api, monkeypatch):
    """A conversation of openai_api runs on the agent its model names and lives in that agent's SessionTracker.
    Appended through the entry agent's, the message went into a copy read back there, which no run of the
    conversation looks at: the conversation's own turn put it back or saved over it."""
    from agent_system import app as app_mod

    coder = _another_agent("coder")
    registry = api.app.state.tool_registry
    get = registry.get
    monkeypatch.setattr(registry, "get", lambda name: coder if name == "coder" else get(name))
    tracker = coder._session_tracker
    tracker.set_session_messages("s1", [ChatMessage(role="user", content="first question"),
                                        ChatMessage(role="assistant", content="the answer")])
    tracker.set_session_metadata("s1", {"user_id": USER, "agent_name": "coder", "llm_profile": "normal"})
    assert await app_mod._session_service.save_session(coder, USER, "s1", "coder", "normal", was_new_session=True)

    async with _client(api.app) as client:
        response = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)

    assert response.status_code == 200, response.text
    assert [m.content for m in tracker.get_session_messages("s1")] == ["first question", "the answer", "follow-up"]
    assert not api.app.state.agent._session_tracker.has_session("s1"), "written through the entry agent's tracker"


async def test_an_append_hands_its_message_to_a_run_that_took_the_session_meanwhile(api, monkeypatch):
    """Which sessions run is asked before the append claims the session; a run that takes the agent's session lock
    after that got the session read back from under it, or the message written beside it -- and its save, of its
    own message list, dropped it. The append holds that lock itself now, and one a run has gets the message."""
    from agent_system import app as app_mod

    await _stored(api, messages=[{"role": "user", "content": "first question"}])
    tracker = api.app.state.agent._session_tracker
    tracker.register_request("run_1", "s1", {"cancel": asyncio.Event(), "appended": [],
                                             "message_event": asyncio.Event()})
    service = app_mod._session_service
    load = service.load_and_restore_session

    async def read_and_then_a_run_takes_it(*args, **kwargs):  # the claim's read, after the question
        loaded = await load(*args, **kwargs)
        assert await tracker.acquire_session_lock("s1", "run_1")
        return loaded

    monkeypatch.setattr(service, "load_and_restore_session", read_and_then_a_run_takes_it)
    monkeypatch.setattr(api.manager, "changed_on_disk", lambda *args: True)  # the claim reads the file again
    try:
        async with _client(api.app) as client:
            response = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)
        assert tracker.check_session_locked("s1") == (True, "run_1"), "fixture: no run took the session"
        handed = [m.content for m in tracker._active_requests["run_1"]["appended"]]
    finally:
        await tracker.release_session_lock("s1", "run_1")
        tracker.unregister_request("run_1")

    assert response.status_code == 200, response.text
    assert handed == ["follow-up"], "not handed to the run"
    assert [m.content for m in tracker.get_session_messages("s1")] == ["first question"], "written beside the run"


async def test_a_second_append_waits_for_the_first(api):
    """The first append holds the session lock through its save; the second took that for a run finishing and
    was refused (409) instead of waiting a moment."""
    await _stored(api, messages=[{"role": "user", "content": "first question"}])
    tracker = api.app.state.agent._session_tracker
    assert await tracker.acquire_session_lock("s1", "write_1", writer=True)  # the first append, saving

    async def the_first_is_done():
        await asyncio.sleep(0.1)
        await tracker.release_session_lock("s1", "write_1")

    done = asyncio.ensure_future(the_first_is_done())
    async with _client(api.app) as client:
        response = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)
    await done

    assert response.status_code == 200, response.text
    stored = await api.manager.load_session(USER, "s1", bypass_cache=True)
    assert [m["content"] for m in stored["messages"]] == ["first question", "follow-up"]


async def test_a_run_that_starts_while_an_append_saves_waits_for_it(api, monkeypatch):
    """The append holds the session lock through its save. A run of this process that asked for it then -- the
    web chat's, an API turn's -- was refused as if another run had the session."""
    from agent_system import app as app_mod

    await _stored(api, messages=[{"role": "user", "content": "first question"}])
    tracker = api.app.state.agent._session_tracker
    service = app_mod._session_service
    save, saving, go_on = service.save_session, asyncio.Event(), asyncio.Event()

    async def slow_save(*args, **kwargs):
        saving.set()
        await go_on.wait()
        return await save(*args, **kwargs)

    monkeypatch.setattr(service, "save_session", slow_save)
    async with _client(api.app) as client:
        appending = asyncio.ensure_future(
            client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0))
        await asyncio.wait_for(saving.wait(), timeout=10.0)
        starting = asyncio.ensure_future(tracker.acquire_session_lock("s1", "run_1", timeout=5.0))
        await asyncio.sleep(0.05)
        go_on.set()
        response = await appending
    try:
        assert await starting is True, "the run was refused while the append saved"
    finally:
        await tracker.release_session_lock("s1", "run_1")

    assert response.status_code == 200, response.text


async def test_an_append_goes_to_the_agent_whose_turn_settles_the_session(api, monkeypatch):
    """The record names the agent of the last SAVED run. A turn on another agent whose run saved nothing (it
    failed on its way in) puts its copy back over the session -- the append went to the record's agent, and was
    gone with that put back."""
    from agent_system import app as app_mod

    coder = _another_agent("coder")
    registry = api.app.state.tool_registry
    get, names = registry.get, registry.list
    monkeypatch.setattr(registry, "get", lambda name: coder if name == "coder" else get(name))
    monkeypatch.setattr(registry, "list", lambda: [*names(), "coder"])
    await _stored(api, messages=[{"role": "user", "content": "first question"}])  # the record: chat_agent's
    await app_mod._session_service.load_and_restore_session(coder, USER, "s1")
    seen = coder._session_tracker.watch_appends("s1")  # a turn of coder settles it

    try:
        async with _client(api.app) as client:
            response = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)
    finally:
        coder._session_tracker.unwatch_appends("s1", seen)

    assert response.status_code == 200, response.text
    assert [m.content for m in seen] == ["follow-up"], "not appended where the settling turn looks"


async def test_an_append_while_a_run_opens_the_session_is_not_read_over(api, monkeypatch):
    """/run opens the session -- reads it from disk, then puts that into the tracker. An append that wrote and
    saved in between was read over by the stale copy, and the run's save wrote it without the message."""
    from agent_system.servers.agent import result_utils

    await _stored(api, messages=[{"role": "user", "content": "first question"},
                                 {"role": "assistant", "content": "the answer"}])
    load, reading, go_on = api.manager.load_session, asyncio.Event(), asyncio.Event()

    async def a_slow_read(user_id, session_id, *args, **kwargs):
        loaded = await load(user_id, session_id, *args, **kwargs)
        if not reading.is_set():  # the opening's read: done, not yet in the tracker
            reading.set()
            await go_on.wait()
        return loaded

    async def run(agent, task, **kwargs):
        return {"summary": "done"}

    monkeypatch.setattr(result_utils, "collect_final_result", run)
    async with _client(api.app) as client:
        monkeypatch.setattr(api.manager, "load_session", a_slow_read)
        running = asyncio.ensure_future(client.post("/run", json={"task": "go", "session_id": "s1"}, timeout=60.0))
        await asyncio.wait_for(reading.wait(), timeout=10.0)
        monkeypatch.setattr(api.manager, "load_session", load)
        appending = asyncio.ensure_future(
            client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0))
        await asyncio.sleep(0.05)
        go_on.set()
        ran, appended = await running, await appending

    assert ran.status_code == 200 and appended.status_code == 200, (ran.text, appended.text)
    stored = await api.manager.load_session(USER, "s1", bypass_cache=True)
    assert [m["content"] for m in stored["messages"]][-1] == "follow-up", "read over by the opening"


async def test_what_another_process_wrote_is_read_back_after_the_web_ui_showed_the_session(api):
    """The web UI opening a session loads it (GET /api/sessions/{id}) through the same session manager, and that
    load counted as seen: the next append's claim took what another process had written before it for this
    process's own, appended to the stale copy in memory and saved it over the other process's turn."""
    await _stored(api, messages=[{"role": "user", "content": "first question"}])  # written here: seen
    api.app.state.agent._session_tracker.set_session_messages(
        "s1", [ChatMessage(role="user", content="first question")])
    await asyncio.sleep(0.05)  # file times on Windows advance in ~16 ms steps
    woken = SessionManager(storage_path=str(api.manager.storage_path))  # another process continues it
    session = await woken.load_session(USER, "s1")
    session["messages"].append({"role": "assistant", "content": "answer of the woken run"})
    await woken.save_session(session)

    from agent_system.api import session_endpoints

    # The web UI opens it: GET /api/sessions/{id}, through the session manager the app runs on
    shown = await session_endpoints.get_session("s1", current_user=None, session_manager=api.manager,
                                                default_agent=None, tool_registry=None)
    async with _client(api.app) as client:
        response = await client.post("/sessions/s1/append", json={"content": "follow-up"}, timeout=60.0)

    assert [m["content"] for m in shown["messages"]] == ["first question", "answer of the woken run"]
    assert response.status_code == 200, response.text
    stored = await woken.load_session(USER, "s1", bypass_cache=True)
    assert [m["content"] for m in stored["messages"]] == [
        "first question", "answer of the woken run", "follow-up"], "the other process's turn was saved over"


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
