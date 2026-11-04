"""A client that disconnects mid-run still releases the session's presence hold.

The release is the last thing in the run's last ``finally``, and that finally runs under the client's cancel. With
an ``await`` in front of the release (``asyncio.to_thread``), the cancel took the await before the call started:
measured with a real StreamingResponse, 1 of 30 disconnects released, and the session counted as held until the
process restarted -- no wake, and every other process answered SessionBusy.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from starlette.responses import StreamingResponse

from agent_system.config.settings import load_settings
from agent_system.servers.agent.mixins import run as run_module
from agent_system.servers.agent.server import Agent
from agent_system.services.background_job_manager import reset_background_job_manager
from agent_system.tools.base import ToolServerRegistry

CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


class NeverAnswers:
    """An LLM override the run waits on until the client is gone."""

    def __init__(self):
        self.called = asyncio.Event()

    def supports_streaming(self) -> bool:
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        self.called.set()
        await asyncio.sleep(60)


class Presence:
    def __init__(self):
        self.held, self.released = [], []

    def hold(self, session_id, user_id, agent_name, run=None):
        self.held.append(session_id)
        return True

    def release(self, session_id, user_id):
        self.released.append(session_id)

    def take_pending(self, session_id, user_id):
        return []


@pytest.fixture(autouse=True)
def _jobs():
    reset_background_job_manager()
    yield
    reset_background_job_manager()


@pytest.fixture
def agent() -> Agent:
    from agent_system.config.models import AgentConfig, ToolServerConfig

    system_config = load_settings(str(CONFIG))
    server_config = ToolServerConfig(type="agent", enabled=True,
                                     agent_config=getattr(system_config, "agent_config", None) or AgentConfig())
    agent = Agent("presence_release_agent", system_config, server_config, ToolServerRegistry())
    agent._session_tracker.set_session_metadata("s1", {"user_id": "u1", "agent_name": agent.name,
                                                       "llm_profile": "normal"})
    return agent


async def _client_goes_mid_run(agent: Agent, request_id: str) -> None:
    llm = NeverAnswers()

    async def body():
        async for event in agent.run_events("hello", request_id=request_id, session_id="s1", llm_override=llm):
            yield f"data: {event.get('type')}\n\n"

    receives = 0

    async def receive():
        nonlocal receives
        receives += 1
        if receives == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        await llm.called.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    await StreamingResponse(body())({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)


async def test_a_client_gone_mid_run_releases_the_hold(agent, monkeypatch):
    presence = Presence()
    monkeypatch.setattr(run_module, "presence_for", lambda config: presence)

    for attempt in range(5):  # the await lost this race 29 times of 30, not always
        presence.held.clear()
        presence.released.clear()
        await _client_goes_mid_run(agent, f"r{attempt}")
        assert presence.held == ["s1"], "fixture: the run did not hold the session"
        assert presence.released == ["s1"], f"attempt {attempt}: the session stayed held"
        assert not agent._presence_holds
