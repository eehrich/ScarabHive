"""Who may read the context engineer's figures and stores: a user her own
sessions', an admin all. The panel answered anyone signed in about any
session's compactions and what its stores hold -- the core memory's facts
included."""
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.session_owners import admin, two_users, user, viewed_by

BASE = "/plugins/context_engineer"


@pytest.fixture
def served(tmp_path, monkeypatch):
    from plugins.context_engineer.plugin import PLUGIN_FACTORY
    from plugins.context_engineer.server import ContextEngineerServer

    two_users(tmp_path, monkeypatch)
    monkeypatch.setattr(ContextEngineerServer, "_load_history", lambda self: None)  # data/context_engineer stays unread
    storage = tmp_path / "stores"
    plugin = PLUGIN_FACTORY("context_engineer", SimpleNamespace(),
                            SimpleNamespace(config={"storage_path": str(storage)}, hook_config={}, name="context_engineer"))
    plugin.server._hooks_impl.history_callback = None  # and unwritten
    for session_id in ("s-alice", "s-bob"):
        plugin.server.stats_history.append({"session_id": session_id, "timestamp": time.time(), "tokens_saved": 7})
        (storage / session_id).mkdir(parents=True)
        (storage / session_id / "core_memory.json").write_text(json.dumps({"facts": [
            {"content": f"a fact of {session_id}", "category": "facts", "importance": 0.5,
             "created_at": "2026-09-25T10:00:00"}]}), encoding="utf-8")
    app = FastAPI()
    app.include_router(plugin.get_web_router())
    return TestClient(app), viewed_by(app)


def _facts(client, session_id):
    return [fact["content"] for fact in client.get(f"{BASE}/session?session_id={session_id}").json()["core_memory"]["facts"]]


def test_her_own_session_s_stores_and_not_another_users(served):
    client, viewer = served
    viewer["user"] = user("alice")

    assert _facts(client, "s-alice") == ["a fact of s-alice"]
    assert _facts(client, "s-bob") == [], "another user's core memory was shown"
    # answered as a session that does not exist is, so the answer does not say whether it does
    foreign = client.get(f"{BASE}/session?session_id=s-bob").json()
    viewer["user"] = admin()
    assert foreign == client.get(f"{BASE}/session?session_id=s-nobody").json()

    viewer["user"] = admin()
    assert _facts(client, "s-bob") == ["a fact of s-bob"], "fixture: bob's store holds a fact"


def test_her_own_compactions_and_not_another_users(served):
    client, viewer = served
    viewer["user"] = user("alice")

    def sessions(query=""):
        answer = client.get(f"{BASE}/history{query}")
        return answer.status_code, {event["session_id"] for event in answer.json().get("events", [])}

    assert sessions("?session_id=s-alice") == (200, {"s-alice"})
    assert sessions("?session_id=s-bob") == (200, set())
    assert sessions()[0] == 403

    viewer["user"] = admin()
    assert sessions() == (200, {"s-alice", "s-bob"})
