"""The message debugger's data is an admin's: the raw LLM traffic of every
session and user. Anyone signed in used to read it, clear it and prune it."""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from tests.session_owners import admin, user, viewed_by

BASE = "/plugins/message_debugger"
ENDPOINTS = [("get", "/turns"), ("get", "/turns/1"), ("get", "/llm-requests"), ("get", "/llm-requests/1"),
             ("get", "/stats"), ("delete", "/clear"), ("post", "/prune?vacuum=false")]


@pytest.fixture
def served(tmp_path):
    from plugins.message_debugger.database import MessageDebuggerDB
    from plugins.message_debugger.plugin import MessageDebuggerHybridPlugin
    from plugins.message_debugger.web_endpoints import MessageDebuggerWebFactory

    server_config = ToolServerConfig()
    server_config.config = {"db_path": str(tmp_path / "plugin.db")}
    plugin = MessageDebuggerHybridPlugin("message_debugger", AgentSystemConfig(), server_config)
    db = MessageDebuggerDB(db_path=str(tmp_path / "debug.db"))
    db.insert_turn(time.time() * 1000, "pre_llm", agent_name="chat", session_id="s-alice", message_count=1)
    db.insert_llm_request(timestamp_ms=time.time() * 1000, direction="request", agent_name="chat",
                          request_id="r1", session_id="s-alice", provider="openai", model="m",
                          payload={"messages": []})
    app = FastAPI()
    app.include_router(MessageDebuggerWebFactory(db=db, name="message_debugger", server=plugin).get_web_router())
    return TestClient(app), viewed_by(app), db


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_a_user_gets_nothing_of_it(served, method, path):
    client, viewer, db = served
    viewer["user"] = user("alice")  # even of her own session: the raw traffic is the operator's

    assert getattr(client, method)(f"{BASE}{path}").status_code == 403
    assert db.count_turns() == 1, "a user's request changed the captured data"


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_an_admin_gets_it(served, method, path):
    client, viewer, _ = served
    viewer["user"] = admin()

    assert getattr(client, method)(f"{BASE}{path}").status_code == 200
