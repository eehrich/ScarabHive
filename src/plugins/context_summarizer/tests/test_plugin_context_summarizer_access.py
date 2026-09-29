"""Who may read the summarizer's events: a user her own sessions', an admin all.

An event carries the messages a summarization removed -- a conversation's
text -- and the panel answered anyone signed in about any session, and let
anyone clear the history of all.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.context_summarizer.plugin import PLUGIN_FACTORY
from tests.session_owners import admin, two_users, user, viewed_by

BASE = "/plugins/context_summarizer"


@pytest.fixture
def served(tmp_path, monkeypatch):
    two_users(tmp_path, monkeypatch)
    plugin = PLUGIN_FACTORY("context_summarizer", AgentSystemConfig(), ToolServerConfig())
    for session_id in ("s-alice", "s-bob", None):  # ids 1, 2, 3
        plugin.server._hooks_impl._record({"session_id": session_id, "status": "success",
                                           "before_messages": [{"role": "user", "content": f"said in {session_id}"}],
                                           "after_messages": []})
    assert [event["id"] for event in plugin.server.summarization_history] == [1, 2, 3], "fixture recorded nothing"
    app = FastAPI()
    app.include_router(plugin.get_web_router())
    return TestClient(app), viewed_by(app), plugin


def _sessions(client, query=""):
    answer = client.get(f"{BASE}/history{query}")
    return answer.status_code, {event["session_id"] for event in answer.json().get("events", [])}


def test_her_own_session_and_not_another_users(served):
    client, viewer, _ = served
    viewer["user"] = user("alice")

    assert _sessions(client, "?session_id=s-alice") == (200, {"s-alice"})
    assert _sessions(client, "?session_id=s-bob") == (200, set())
    assert client.get(f"{BASE}/history?session_id=s-bob").json()["stats"]["events"] == 0


def test_every_session_at_once_is_for_an_admin(served):
    client, viewer, _ = served
    viewer["user"] = user("alice")
    assert client.get(f"{BASE}/history").status_code == 403

    viewer["user"] = admin()
    assert _sessions(client) == (200, {"s-alice", "s-bob", None})


def test_an_event_s_messages_are_its_sessions_users(served):
    client, viewer, _ = served
    viewer["user"] = user("alice")

    assert client.get(f"{BASE}/events/1").json()["before_messages"][0]["content"] == "said in s-alice"
    assert client.get(f"{BASE}/events/2").status_code == 404, "another user's conversation was shown"
    assert client.get(f"{BASE}/events/3").status_code == 404, "an event of no session was shown to a user"

    viewer["user"] = admin()
    assert client.get(f"{BASE}/events/2").status_code == 200
    assert client.get(f"{BASE}/events/3").status_code == 200


def test_only_an_admin_clears_everyones_events(served):
    client, viewer, plugin = served
    viewer["user"] = user("alice")
    assert client.post(f"{BASE}/clear").status_code == 403
    assert len(plugin.server.summarization_history) == 3, "a user cleared everyone's events"

    viewer["user"] = admin()
    assert client.post(f"{BASE}/clear").status_code == 200
    assert plugin.server.summarization_history == []
