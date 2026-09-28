"""The agent's session lock around a run's end (Agent._finalize_request) and its refusal (Agent.run_events).

A request of this process that finds the session's lock free reads the session from disk
(SessionService.open_for_run). The run let go of the lock before its last save: a web chat opening in that gap read
the session without the run's last exchange, and its own run saved over it. The lock goes once the conversation is
on disk now -- also when that save is cut short -- and a run refused at the lock says so in a way its caller can
tell (``error_type``), so the caller saves nothing either.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_system.config.settings import load_settings
from agent_system.servers.agent.server import SESSION_LOCKED, Agent
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from agent_system.tools.base import ToolServerRegistry

CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


class AnswersAtOnce:
    """An LLM override that answers without tools and does not stream."""

    def supports_streaming(self) -> bool:
        return False

    async def chat(self, messages, tools_schema=None):
        return {"assistant": {"content": "planner response", "tool_calls": []}, "usage": {"total_tokens": 10}}

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        return {"assistant": {"content": "the answer"}, "usage": {"total_tokens": 5}}


@pytest.fixture
def agent(tmp_path) -> Agent:
    from agent_system.config.models import AgentConfig, ToolServerConfig

    system_config = load_settings(str(CONFIG))
    server_config = ToolServerConfig(type="agent", enabled=True,
                                     agent_config=getattr(system_config, "agent_config", None) or AgentConfig())
    agent = Agent("lock_order_agent", system_config, server_config, ToolServerRegistry())
    agent._session_service = SessionService(SessionManager(str(tmp_path)), checkpoint_interval_seconds=0)
    agent._session_tracker.set_session_metadata("s1", {"user_id": "u1", "agent_name": agent.name,
                                                       "llm_profile": "normal"})
    return agent


async def test_the_last_save_happens_while_the_run_holds_its_session_lock(agent):
    tracker, service = agent._session_tracker, agent._session_service
    seen, save = [], service.save_session

    async def watched(*args, **kwargs):
        seen.append(tracker.check_session_locked("s1"))
        return await save(*args, **kwargs)

    service.save_session = watched
    async for _ in agent.run_events("hello", request_id="r1", session_id="s1", llm_override=AnswersAtOnce()):
        pass

    assert seen, "fixture: the run saved nothing"
    assert seen[-1] == (True, "r1"), "the last save came after the run let go of the session lock"
    assert tracker.check_session_locked("s1") == (False, None), "the run kept the lock"


async def test_a_run_cancelled_in_its_last_save_lets_go_of_the_lock(agent):
    """Cancelled there (a client that left), the run must not keep the session locked for the life of the process:
    every later run of it would be refused."""
    tracker, service = agent._session_tracker, agent._session_service
    saving, save = asyncio.Event(), service.save_session

    async def hangs(*args, **kwargs):
        saving.set()
        await asyncio.Event().wait()
        return await save(*args, **kwargs)

    service.save_session = hangs

    async def run() -> None:
        async for _ in agent.run_events("hello", request_id="r1", session_id="s1", llm_override=AnswersAtOnce()):
            pass

    task = asyncio.ensure_future(run())
    await asyncio.wait_for(saving.wait(), 30)
    assert tracker.check_session_locked("s1") == (True, "r1"), "fixture: the save ran without the lock"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tracker.check_session_locked("s1") == (False, None), "a cancelled save left the session locked"


async def test_a_run_refused_at_the_lock_says_so(agent):
    tracker = agent._session_tracker
    assert await tracker.acquire_session_lock("s1", "another_run"), "fixture: the lock was not taken"
    try:
        events = [event async for event in agent.run_events("hello", request_id="r2", session_id="s1",
                                                            llm_override=AnswersAtOnce())]
    finally:
        await tracker.release_session_lock("s1", "another_run")

    errors = [event for event in events if event.get("type") == "error"]
    assert [event.get("error_type") for event in errors] == [SESSION_LOCKED], events
    assert events[-1]["type"] == "end"
