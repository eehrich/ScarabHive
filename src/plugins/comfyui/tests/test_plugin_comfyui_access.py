"""Whose ComfyUI jobs the panel lists and cancels: a user her own sessions' (with their sub-agents), an admin all.

The panel listed every job to anyone signed in and let anyone cancel any of them; it had no session view at all,
so the chat's "open with panel" never offered it.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system import app_state
from agent_system.auth.models import UserRole
from plugins.comfyui.server import ComfyUIServer
from tests.session_owners import two_users, user, viewed_by

PREFIX = "/plugins/comfyui"


@pytest.fixture
def served(tmp_path, monkeypatch):
    two_users(tmp_path, monkeypatch)
    manager = app_state.session_service.session_manager
    asyncio.run(manager.create_session(user_id="alice", session_id="s-alice-sub", agent_name="painter",
                                       parent_session_id="s-alice"))
    for session_id, parent in (("s-alice-chat", None), ("s-alice-chat-sub", "s-alice-chat"), ("s-alice-idle", None)):
        asyncio.run(manager.create_session(user_id="alice", session_id=session_id, agent_name="chat",
                                           parent_session_id=parent))
    asyncio.run(manager.create_session(user_id="bob", session_id="s-bob-idle", agent_name="chat"))

    config = MagicMock()
    config.host, config.port, config.servers = "gpu.test", 8188, None
    config.timeout_seconds, config.cleanup_age_hours, config.workflows = 5, 0, []
    config.output_dir = str(tmp_path / "outputs")
    config.workflow_files_dir = str(tmp_path / "workflows")
    server = ComfyUIServer("comfyui", MagicMock(), config)
    server._startup_sync_done = True
    server.client.ping = AsyncMock(return_value={"status": "online", "queue_pending": 0, "queue_running": 0})
    server._get_aggregated_queue = AsyncMock(return_value=None)  # no server to bring the tracker up to date with
    server._cancel_job = AsyncMock(return_value={"status": "cancelled", "prompt_id": "x"})
    tracker = server.job_tracker
    for prompt_id, session_id in (("a-own", "s-alice"), ("a-sub", "s-alice-sub"), ("b-own", "s-bob"),
                                  ("pipeline", None), ("chat-sub", "s-alice-chat-sub")):
        tracker.register_job(prompt_id, "flux", "Flux", {}, server_url="http://gpu.test:8188", session_id=session_id)

    app = FastAPI()
    app.include_router(server.get_web_router())
    return TestClient(app), viewed_by(app), server


def _jobs(client, query=""):
    response = client.get(f"{PREFIX}/jobs{query}")
    body = response.json()
    return response.status_code, {job["prompt_id"] for job in body.get("active", [])}, body.get("stats", {}).get("total")


def test_a_session_shows_its_jobs_and_its_sub_agents(served):
    client, viewer, _ = served
    viewer["user"] = user("alice")

    assert _jobs(client, "?session_id=s-alice") == (200, {"a-own", "a-sub"}, 2)


def test_another_users_session_shows_no_jobs(served):
    client, viewer, _ = served
    viewer["user"] = user("alice")

    assert _jobs(client, "?session_id=s-bob") == (200, set(), 0)


def test_every_session_at_once_is_for_an_admin(served):
    client, viewer, _ = served
    viewer["user"] = user("alice")
    assert _jobs(client)[0] == 403

    viewer["user"] = user("root", UserRole.ADMIN)
    assert _jobs(client) == (200, {"a-own", "a-sub", "b-own", "pipeline", "chat-sub"}, 5)
    assert _jobs(client, "?session_id=s-alice") == (200, {"a-own", "a-sub"}, 2), "an admin sees alice's tree"


@pytest.mark.parametrize("prompt_id, status", [("a-sub", 200), ("b-own", 404), ("pipeline", 404)])
def test_a_user_cancels_only_the_jobs_of_her_sessions(served, prompt_id, status):
    client, viewer, _ = served
    viewer["user"] = user("alice")

    assert client.post(f"{PREFIX}/jobs/{prompt_id}/cancel").status_code == status


@pytest.mark.parametrize("session_id, offered", [
    ("s-alice", True),          # a job of its own
    ("s-alice-chat", True),     # none of its own, but its sub-agent's
    ("s-alice-idle", False),    # no job anywhere below it
    ("s-bob-idle", True),       # another user's (an admin's view): offered, finding its owner is a scan
])
def test_the_chat_offers_the_panel_for_a_session_with_jobs(served, session_id, offered):
    _, _, server = served

    assert asyncio.run(server.has_session_data(session_id, "alice")) is offered


def test_a_session_id_no_session_can_have_shows_nothing_and_logs_nothing(served, caplog):
    """Asked again every five seconds: a warning each time would fill the log."""
    client, viewer, _ = served
    viewer["user"] = user("root", UserRole.ADMIN)

    with caplog.at_level("WARNING", logger="plugins.comfyui.web_endpoints"):
        assert _jobs(client, "?session_id=..%2Fs-alice") == (200, set(), 0)
    assert not caplog.records, [r.getMessage() for r in caplog.records]
