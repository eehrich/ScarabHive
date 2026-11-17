"""A session the API wakes runs its woken turn in the API, as a job (api/woken_runs.py).

Rung from the API, a wake used to start ``agent-cli run --woken``, a process of its
own: the chat could not follow it, stop it or write to it, and it outlived the API.
Here the real presence (lock files under tmp_path) hands the session to the API's
waker; the agent's run is the only fake. It holds the session per request as the
agent loop does (servers/agent/mixins/run.py), so the stop rules see the run.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from agent_system import app_state
from agent_system.core.session_presence import presence as presence_mod
from agent_system.core.cancellation import get_cancellation_manager
from agent_system.core.request_context import run_is_attended
from agent_system.core.session_presence import presence_for, wake_depth
from agent_system.servers.agent.server import Agent
from agent_system.services.background_job_manager import get_background_job_manager

pytestmark = pytest.mark.anyio

USER = "wakeuser"


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
    """The real app over a sessions directory of the test's own, with its waker in place --
    and no process a wake could start."""
    from agent_system import app as app_mod
    from agent_system.api import woken_runs

    _disable_auth(monkeypatch)
    monkeypatch.setenv("AGENT_SESSION_STORAGE_PATH", str(tmp_path))

    def no_process(*args, **kwargs):
        raise AssertionError("a wake rung in the API started a process of its own")

    monkeypatch.setattr(presence_mod, "spawn_wake", no_process)
    app = app_mod.build_app()
    assert app_state.session_service.session_manager.storage_path == tmp_path
    monkeypatch.setattr(app_state, "shutdown_event", asyncio.Event())
    yield app
    woken_runs.close()


def _a_session(app) -> str:
    """A stored session of USER on the entry agent, with input waiting for it."""
    session_id = f"s{uuid.uuid4().hex[:10]}"
    manager = app_state.session_service.session_manager
    path = manager._get_session_path(USER, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": session_id, "user_id": USER,
                                "agent_name": app.state.context.agent.name, "title": "woken", "messages": [],
                                "created_at": "2026-11-17T00:00:00", "updated_at": "2026-11-17T00:00:00"}),
                    encoding="utf-8")
    return session_id


def _a_turn_that_waits(monkeypatch, presence):
    """The agent's run: holds the session and takes a cancellation token as the agent loop does, records what
    it saw, and waits for ``go`` -- or for its token, which is how a Stop reaches a run."""
    go = asyncio.Event()
    seen: list[dict] = []

    async def run_events(self, task, request_id=None, session_id=None, **kwargs):
        token = get_cancellation_manager().create_token(request_id)
        presence.hold(session_id, USER, self.name, run=request_id)
        try:
            seen.append({"request_id": request_id, "depth": wake_depth(), "role": getattr(task, "role", None),
                         "attended": run_is_attended(request_id)})
            yield {"type": "start", "session_id": session_id, "request_id": request_id}
            waits = [asyncio.ensure_future(go.wait()), asyncio.ensure_future(token.wait_for_cancellation())]
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for wait in waits:
                wait.cancel()
            if token.is_cancelled:
                seen.append({"stopped": True})
                yield {"type": "cancelled", "request_id": request_id}
            else:
                yield {"type": "final", "summary": "done", "request_id": request_id}
            yield {"type": "end", "request_id": request_id}
        finally:
            presence.release(session_id, USER)
            get_cancellation_manager().unregister_request(request_id)

    monkeypatch.setattr(Agent, "run_events", run_events)
    return go, seen


async def _wait_for(condition, what, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(what)


async def _active(session_id):
    from agent_system.api.session_endpoints import list_active_sessions

    owns = SimpleNamespace(belongs_to=lambda user_id, sid: True)
    return (await list_active_sessions(ids=session_id, current_user=SimpleNamespace(username=USER),
                                       session_manager=owns))["active"].get(session_id)


async def test_a_woken_turn_is_a_job_the_chat_follows_and_stops(api, monkeypatch):
    from agent_system.api import woken_runs

    presence = presence_for(api.state.context.config)
    session_id = _a_session(api)
    go, seen = _a_turn_that_waits(monkeypatch, presence)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test"):
        woken_runs.install(api)
        woken_runs.ready()
        state, note = await asyncio.to_thread(presence.notify, session_id, USER)
        assert (state, note) == ("woke_session", "")

        async def attachable():
            entry = await _active(session_id)
            return bool(seen and entry and entry.get("attachable") and entry.get("request_id"))
        await _wait_for(attachable, "the woken turn never showed up as a job the chat can attach to")
        entry = await _active(session_id)
        assert not entry.get("elsewhere"), entry
        assert seen and seen[0]["request_id"] == entry["request_id"]
        assert seen[0]["role"] == "developer", "the woken turn is not the run speaking (wake_message)"
        assert seen[0]["depth"] == 1, "the woken turn does not carry its depth in the wake chain"
        assert seen[0]["attended"], "a question of the woken turn would reach nobody who follows it"
        assert presence.held_here(session_id, USER)
        assert not presence.pending(session_id, USER), "the woken turn left its input marked as waiting"

        job = await get_background_job_manager().get_job(entry["request_id"])
        assert any(event.get("type") == "start" for event in job.events),             "the woken turn's events do not reach the job the chat follows"

        # The chat's Stop.
        await get_background_job_manager().cancel_job(entry["request_id"])

        async def let_go():
            return not presence.held_here(session_id, USER)
        await _wait_for(let_go, "the woken turn went on after its Stop")
        assert {"stopped": True} in seen, "the Stop did not reach the woken turn"
        assert presence.status(session_id, USER) is None, "the lock file outlived the woken turn"
        lock = presence._lock_path(session_id, USER)
        assert lock.with_suffix(".stopped").exists(), "a stopped woken turn left its session to be woken again"


async def test_a_turn_that_cannot_run_lets_go_and_does_not_wake_the_session_again(api, monkeypatch):
    from agent_system.api import run_start, woken_runs

    presence = presence_for(api.state.context.config)
    session_id = _a_session(api)
    tries = []

    def refused(*args, **kwargs):
        tries.append(args)
        raise HTTPException(status_code=404, detail="no such agent")

    monkeypatch.setattr(run_start, "get_agent_with_overrides", refused)
    woken_runs.install(api)
    woken_runs.ready()
    assert (await asyncio.to_thread(presence.notify, session_id, USER))[0] == "woke_session"

    async def let_go():
        return bool(tries) and not presence.held_here(session_id, USER)
    await _wait_for(let_go, "a woken turn that could not run kept the session")
    await asyncio.sleep(0.2)
    assert len(tries) == 1, "letting go of a failed turn woke the session into the same failure again"
    assert not presence.pending(session_id, USER)
    assert presence.status(session_id, USER) is None


async def test_a_ring_while_the_api_stops_wakes_nobody_and_the_input_waits(api):
    from agent_system.api import woken_runs

    presence = presence_for(api.state.context.config)
    session_id = _a_session(api)
    woken_runs.install(api)
    woken_runs.ready()
    app_state.shutdown_event.set()
    state, note = await asyncio.to_thread(presence.notify, session_id, USER)
    assert (state, note) == ("queued", woken_runs.SHUTTING_DOWN)
    assert presence.pending(session_id, USER), "the input lost its marker; the next run would not be told"
    assert not presence.held_here(session_id, USER)
    assert presence.status(session_id, USER) is None


async def test_a_turn_rung_before_the_app_is_ready_waits_for_it(api, monkeypatch):
    from agent_system.api import woken_runs

    presence = presence_for(api.state.context.config)
    session_id = _a_session(api)
    go, seen = _a_turn_that_waits(monkeypatch, presence)
    woken_runs.install(api)
    assert (await asyncio.to_thread(presence.notify, session_id, USER))[0] == "woke_session"
    await asyncio.sleep(0.3)
    assert not seen, "a woken turn ran before the app was ready"
    assert presence.held_here(session_id, USER), "the session was let go while its turn waited"
    woken_runs.ready()

    async def ran():
        return bool(seen)
    await _wait_for(ran, "the woken turn never ran once the app was ready")
    go.set()

    async def let_go():
        return not presence.held_here(session_id, USER)
    await _wait_for(let_go, "the session stayed held after its woken turn ended")


async def test_input_taken_meanwhile_starts_no_turn(api, monkeypatch):
    from agent_system.api import woken_runs

    presence = presence_for(api.state.context.config)
    session_id = _a_session(api)
    go, seen = _a_turn_that_waits(monkeypatch, presence)
    woken_runs.install(api)
    assert (await asyncio.to_thread(presence.notify, session_id, USER))[0] == "woke_session"
    presence.take_pending(session_id, USER)   # a run of the user's took it before the turn started
    woken_runs.ready()

    async def let_go():
        return not presence.held_here(session_id, USER)
    await _wait_for(let_go, "the session stayed held although its turn had nothing to do")
    assert not seen, "a woken turn ran for input that was taken already"
