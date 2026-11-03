"""A session's live state goes once its last run and background job have ended (LiveStateMixin._release_live_state).

It was never dropped at all: only a discarded session or eviction took it, so a finished run's message list
and tool schemas stayed for the life of the process, and a compaction tool called between runs read them
instead of the session as it is now. Not at the run's own end when a background job wraps it: the job runs
on past the run, and while it does the session load (api/session_live.serve_live_state) counts the run as
in flight and serves this state with the run's event count -- found gone, the load served the turn off disk
with no count and the chat, joining the job, showed it twice.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_system.api.session_live import serve_live_state
from agent_system.config.settings import load_settings
from agent_system.services.background_job_manager import get_background_job_manager, reset_background_job_manager
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry

CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


class AnswersAtOnce:
    """An LLM override that answers without tools and does not stream."""

    def supports_streaming(self) -> bool:
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        return {"assistant": {"content": "the answer"}, "usage": {"total_tokens": 5}}


@pytest.fixture
def agent() -> Agent:
    from agent_system.config.models import AgentConfig, ToolServerConfig

    system_config = load_settings(str(CONFIG))
    server_config = ToolServerConfig(type="agent", enabled=True,
                                     agent_config=getattr(system_config, "agent_config", None) or AgentConfig())
    agent = Agent("live_release_agent", system_config, server_config, ToolServerRegistry())
    agent._session_tracker.set_session_metadata("s1", {"user_id": "u1", "agent_name": agent.name,
                                                       "llm_profile": "normal"})
    return agent


@pytest.fixture(autouse=True)
def _jobs():
    reset_background_job_manager()
    yield
    reset_background_job_manager()


async def _run(agent: Agent, request_id: str = "r1"):
    async for event in agent.run_events("hello", request_id=request_id, session_id="s1",
                                        llm_override=AnswersAtOnce()):
        yield event


async def test_a_run_without_a_job_lets_go_of_its_live_state_at_its_end(agent):
    seen = []
    async for event in _run(agent):
        if event.get("type") == "final":
            seen.append(agent.get_live_messages("s1") is not None)

    assert seen == [True], "fixture: the run had no live state while it ran"
    assert agent.get_live_messages("s1") is None, "the finished run's live state stayed"
    assert agent.get_live_tools_schema("s1") is None


async def test_a_job_keeps_the_live_state_until_it_ends(agent):
    """The run is over (lock let go of), its job is not: the session load still serves the live state, with
    the run's event count -- the turn-shown-twice protection. Once the job has ended, the state goes."""
    finishing = asyncio.Event()
    run_over = asyncio.Event()

    async def runner():
        async for event in _run(agent):
            yield event
        run_over.set()
        await finishing.wait()   # the job runs on past the run: post-processing, a slow reader

    job = await get_background_job_manager().create_job(
        request_id="r1", user_id="u1", agent_name=agent.name, session_id="s1", agent_runner=runner)
    await asyncio.wait_for(run_over.wait(), 30)

    assert agent._session_tracker.check_session_locked("s1") == (False, None), "fixture: the run kept the lock"
    assert agent.get_live_messages("s1") is not None, "the live state went while the run's job runs"
    session: dict = {"messages": []}
    await serve_live_state(session, "s1", agent)
    assert [m["content"] for m in session["messages"]][-2:] == ["hello", "the answer"]
    assert session["live_events_seen"] == job.events_emitted

    finishing.set()
    await job.task
    await asyncio.sleep(0)   # the job task's done callbacks
    assert agent.get_live_messages("s1") is None, "the live state stayed after the session's last job ended"


async def test_a_newer_run_of_the_session_keeps_its_live_state(agent):
    """The old job ends while the session's next run holds the lock: what is there is that run's."""
    finishing = asyncio.Event()
    run_over = asyncio.Event()

    async def runner():
        async for event in _run(agent):
            yield event
        run_over.set()
        await finishing.wait()

    job = await get_background_job_manager().create_job(
        request_id="r1", user_id="u1", agent_name=agent.name, session_id="s1", agent_runner=runner)
    await asyncio.wait_for(run_over.wait(), 30)
    assert await agent._session_tracker.acquire_session_lock("s1", "r2"), "fixture: the next run got no lock"
    try:
        finishing.set()
        await job.task
        await asyncio.sleep(0)
        assert agent.get_live_messages("s1") is not None, "the old job's end took the next run's live state"
    finally:
        await agent._session_tracker.release_session_lock("s1", "r2")
