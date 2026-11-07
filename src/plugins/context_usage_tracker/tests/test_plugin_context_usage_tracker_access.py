"""Who may read the tracker's figures: a user her own sessions', an admin all.

The endpoints answered anyone signed in about any session id -- or every
session at once, ids included -- and let anyone clear the history for all.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system import app_state
from agent_system.auth.models import UserRole
from plugins.context_usage_tracker.plugin import ContextUsageTrackerPlugin
from tests.session_owners import two_users, user, viewed_by

PREFIX = "/plugins/context_usage_tracker"


@pytest.fixture
def served(tmp_path, monkeypatch):
    two_users(tmp_path, monkeypatch)

    plugin = ContextUsageTrackerPlugin(name="context_usage_tracker", system_config={},
                                       server_config={"storage_path": str(tmp_path / "usage.json")})
    tracker = plugin.web_factory.tracker
    tracker.record_usage(agent_id="a", agent_name="chat", session_id="s-alice", total_tokens=111)
    tracker.record_usage(agent_id="b", agent_name="chat", session_id="s-bob", total_tokens=222)
    assert {c["session_id"] for c in tracker.get_history()} == {"s-alice", "s-bob"}, "fixture recorded nothing"

    app = FastAPI()
    app.include_router(plugin.get_web_router())  # its paths carry /plugins/<name> already
    return TestClient(app), viewed_by(app), tracker


def _sessions_in(client, query=""):
    response = client.get(f"{PREFIX}/history{query}")
    return response.status_code, {call["session_id"] for call in response.json().get("history", [])}


def test_a_user_sees_her_own_session(served):
    client, viewer, _ = served
    viewer["user"] = user("alice")

    assert _sessions_in(client, "?session_id=s-alice") == (200, {"s-alice"})
    usage = client.get(f"{PREFIX}/usage?session_id=s-alice").json()
    assert usage["statistics"], "her own figures went missing"


def test_another_users_session_shows_nothing(served):
    client, viewer, _ = served
    viewer["user"] = user("alice")

    assert _sessions_in(client, "?session_id=s-bob") == (200, set())
    usage = client.get(f"{PREFIX}/usage?session_id=s-bob").json()
    assert usage == {"latest": {}, "agents": {}, "statistics": {}}, usage


def test_every_session_at_once_is_for_an_admin(served):
    client, viewer, _ = served
    viewer["user"] = user("alice")
    assert client.get(f"{PREFIX}/history").status_code == 403
    assert client.get(f"{PREFIX}/usage").status_code == 403

    viewer["user"] = user("admin", UserRole.ADMIN)
    assert _sessions_in(client) == (200, {"s-alice", "s-bob"})


def test_an_admin_sees_any_one_session_too(served):
    """The panel opened from a session's header names it: an admin asking for
    one session is not held to her own."""
    client, viewer, _ = served
    viewer["user"] = user("admin", UserRole.ADMIN)

    assert _sessions_in(client, "?session_id=s-bob") == (200, {"s-bob"})


def test_a_run_that_names_no_user_is_held_against_the_store(served, monkeypatch):
    """A running session whose tracker names no user says nothing about whose
    it is -- the session store answers."""
    from agent_system.services.background_job_manager import BackgroundJobManager

    client, viewer, _ = served

    async def running(self):
        return {"s-alice": {"user_id": None}, "s-bob": {"user_id": None}}
    monkeypatch.setattr(BackgroundJobManager, "active_sessions", running)

    viewer["user"] = user("alice")
    assert _sessions_in(client, "?session_id=s-alice") == (200, {"s-alice"})
    assert _sessions_in(client, "?session_id=s-bob") == (200, set())


def test_nobody_signed_in_is_nobody_s_owner(served):
    client, viewer, _ = served
    viewer["user"] = None

    assert _sessions_in(client, "?session_id=s-alice") == (200, set())


def test_the_first_turn_of_her_new_session_is_hers(served, monkeypatch):
    """Not on disk before its first save -- the run that has it knows whose it is."""
    from agent_system.services.background_job_manager import BackgroundJobManager

    client, viewer, tracker = served
    tracker.record_usage(agent_id="a", agent_name="chat", session_id="s-fresh", total_tokens=5)

    async def running(self):
        return {"s-fresh": {"user_id": "alice"}}
    monkeypatch.setattr(BackgroundJobManager, "active_sessions", running)

    viewer["user"] = user("alice")
    assert _sessions_in(client, "?session_id=s-fresh") == (200, {"s-fresh"})
    viewer["user"] = user("bobby")
    assert _sessions_in(client, "?session_id=s-fresh") == (200, set()), "another user's running session showed"


def test_with_authentication_off_everything_is_shown(served):
    from types import SimpleNamespace

    client, viewer, _ = served
    client.app.state.config = SimpleNamespace(auth=SimpleNamespace(enabled=False))
    viewer["user"] = None

    assert _sessions_in(client) == (200, {"s-alice", "s-bob"})
    assert _sessions_in(client, "?session_id=s-bob") == (200, {"s-bob"})


def test_without_a_session_store_it_says_so(served, monkeypatch):

    client, viewer, _ = served
    monkeypatch.setattr(app_state, "session_service", None)
    viewer["user"] = user("alice")

    assert client.get(f"{PREFIX}/history?session_id=s-alice").status_code == 503


def test_only_an_admin_clears_everyones_figures(served):
    client, viewer, tracker = served
    viewer["user"] = user("alice")
    assert client.post(f"{PREFIX}/clear").status_code == 403
    assert len(tracker.get_history()) == 2, "a user cleared everyone's figures"

    viewer["user"] = user("admin", UserRole.ADMIN)
    assert client.post(f"{PREFIX}/clear").status_code == 200
    assert tracker.get_history() == []


def test_the_clear_waits_off_the_loop(served, monkeypatch):
    """It waits in line behind every write queued before it, each of which may be waiting out a lock."""
    import asyncio

    client, viewer, tracker = served
    on_a_loop = []
    clear = tracker.clear_history

    def watched():
        try:
            asyncio.get_running_loop()
            on_a_loop.append(True)
        except RuntimeError:
            on_a_loop.append(False)
        clear()

    monkeypatch.setattr(tracker, "clear_history", watched)
    viewer["user"] = user("admin", UserRole.ADMIN)

    assert client.post(f"{PREFIX}/clear").status_code == 200
    assert on_a_loop == [False], f"the clear waited on the event loop: {on_a_loop}"
    assert tracker.get_history() == []


def test_a_run_session_shows_the_calls_of_the_sessions_below_it(served):
    """A state machine's run session makes no call of its own: its agents' sessions hang below it, and their
    calls are what /usage shows for it -- read under its owner, also when an admin asks."""
    import asyncio

    client, viewer, tracker = served
    sessions = app_state.session_service.session_manager
    for session_id, parent in (("sg_alice_run", None), ("alice-agent", "sg_alice_run")):
        asyncio.run(sessions.save_session(asyncio.run(sessions.create_session(
            user_id="alice", session_id=session_id, agent_name="a", llm_profile="p", parent_session_id=parent))))
    tracker.record_usage(agent_id="agent", agent_name="coder", session_id="alice-agent", total_tokens=7,
                         context_window=1000, cost=0.5, request_id="run_001")

    for asker in (user("alice"), user("admin", UserRole.ADMIN)):
        viewer["user"] = asker
        usage = client.get(f"{PREFIX}/usage?session_id=sg_alice_run").json()
        assert usage["statistics"]["totals"]["cost"] == 0.5, (asker.username, usage)
        assert set(usage["agents"]) == {"agent"}
        assert usage["latest"] == {}, "the run's own last call is none: its context is not its agent's"
        assert _sessions_in(client, "?session_id=sg_alice_run") == (200, {"alice-agent"})

    viewer["user"] = user("bob")
    assert client.get(f"{PREFIX}/usage?session_id=sg_alice_run").json()["statistics"] == {}
