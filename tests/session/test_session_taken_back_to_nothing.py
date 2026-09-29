"""A session whose whole conversation was taken back (/undo) is still the session it was.

The store writes it empty -- otherwise its record keeps the dropped turn and the
next ``--session <id>`` brings it back -- while a session that never held a
message is still not created. And its start hooks, which may have side
effects, do not run a second time.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolServerConfig,
)
from agent_system.llm.models import ChatMessage
from agent_system.tools.base import ToolServerRegistry
from agent_system.servers.agent.components.session_tracking import SessionTracker
from agent_system.servers.agent.server import Agent
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService

TURN = [ChatMessage(role="user", content="the first question"), ChatMessage(role="assistant", content="an answer")]


class TestTheStore:
    @pytest.mark.asyncio
    async def test_a_stored_session_taken_back_to_nothing_is_written_empty(self, tmp_path):
        store = SessionManager(storage_path=str(tmp_path))
        tracker = SessionTracker()
        agent = SimpleNamespace(_session_tracker=tracker, agent_config=None)
        service = SessionService(store, checkpoint_interval_seconds=0)
        tracker.set_session_messages("s1", list(TURN))
        assert await service.save_session(agent, "ada", "s1", "chat", "normal", was_new_session=True)

        tracker.set_session_messages("s1", [])
        assert await service.save_session(agent, "ada", "s1", "chat", "normal", was_new_session=False)

        stored = await store.load_session("ada", "s1")
        assert stored["messages"] == []
        assert stored["title"] == "the first question", "the session keeps its name"

    @pytest.mark.asyncio
    async def test_a_stored_session_this_process_never_held_is_not_wiped(self, tmp_path):
        """An agent that saves a session it never loaded must not blank the record."""
        store = SessionManager(storage_path=str(tmp_path))
        writer = SessionTracker()
        writer.set_session_messages("s1", list(TURN))
        service = SessionService(store, checkpoint_interval_seconds=0)
        assert await service.save_session(SimpleNamespace(_session_tracker=writer, agent_config=None),
                                          "ada", "s1", "chat", "normal", was_new_session=True)
        stranger = SessionTracker()
        stranger.set_session_messages("s1", [])

        saved = await service.save_session(SimpleNamespace(_session_tracker=stranger, agent_config=None),
                                           "ada", "s1", "chat", "normal", was_new_session=False)

        assert saved is False
        assert len((await store.load_session("ada", "s1"))["messages"]) == 2

    @pytest.mark.asyncio
    async def test_a_session_that_never_held_a_message_is_not_created(self, tmp_path):
        store = SessionManager(storage_path=str(tmp_path))
        tracker = SessionTracker()
        agent = SimpleNamespace(_session_tracker=tracker, agent_config=None)
        tracker.set_session_messages("s2", [])

        saved = await SessionService(store, checkpoint_interval_seconds=0).save_session(
            agent, "ada", "s2", "chat", "normal", was_new_session=True)

        assert saved is False
        assert await store._find_session_owner_async("s2") is None


class _LLM:
    model = "stub/model"

    def supports_streaming(self) -> bool:
        return False

    async def chat_tools(self, messages, tools, cancellation_token=None, status_scope=None):
        return {"assistant": {"role": "assistant", "content": "an answer"}, "finish_reason": "stop"}


def _agent() -> Agent:
    llm_system = LLMSystemConfig(
        models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4", api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="gpt-4")},
        default_profile="normal",
    )
    config = ToolServerConfig(type="agent", enabled=True, agent_config=AgentConfig(max_steps=1))
    agent = Agent("test_agent", AgentSystemConfig(llm_system=llm_system), config, ToolServerRegistry())
    agent.llm = _LLM()
    agent._hook_manager.execute_session_start_hooks = AsyncMock(return_value=None)
    return agent


async def _turn(agent: Agent, session_id: str) -> None:
    events = [event async for event in agent.run_events("a question", session_id=session_id)]
    assert [e["summary"] for e in events if e.get("type") == "final"] == ["an answer"], events


def _started(agent: Agent) -> list:
    return [call.args[0] for call in agent._hook_manager.execute_session_start_hooks.call_args_list]


class TestTheStartHooks:
    @pytest.mark.asyncio
    async def test_they_run_once_per_session_even_after_it_was_taken_back_to_nothing(self):
        agent = _agent()
        await _turn(agent, "s1")
        assert agent._session_tracker.get_session_messages("s1"), "the fixture kept no history"
        await _turn(agent, "s1")
        agent._session_tracker.set_session_messages("s1", [])  # /undo of the only exchange
        await _turn(agent, "s1")

        await _turn(agent, "s2")

        assert _started(agent) == ["s1", "s2"]

    @pytest.mark.asyncio
    async def test_a_loaded_history_is_a_started_session(self):
        agent = _agent()
        agent._session_tracker.set_session_messages("s3", list(TURN))  # resumed from the store

        await _turn(agent, "s3")

        assert _started(agent) == []

    @pytest.mark.asyncio
    async def test_a_discarded_session_leaves_nothing_behind(self):
        """Stateless calls discard their throwaway session after every call; the mark must go with it."""
        agent = _agent()
        tracker = agent._session_tracker
        await _turn(agent, "tmp")
        tracker.set_session_template_vars("tmp", {"x": 1})

        tracker.discard_session("tmp")

        assert "tmp" not in tracker._started
        assert "tmp" not in tracker._held
        assert "tmp" not in tracker._session_template_vars
        await _turn(agent, "tmp")
        assert _started(agent) == ["tmp", "tmp"]

    @pytest.mark.asyncio
    async def test_a_deleted_session_starts_anew(self):
        agent = _agent()
        await _turn(agent, "gone")

        assert agent._session_tracker.delete_session("gone")

        assert "gone" not in agent._session_tracker._held
        await _turn(agent, "gone")
        assert _started(agent) == ["gone", "gone"]

    @pytest.mark.asyncio
    async def test_clearing_the_tracker_forgets_every_session(self):
        agent = _agent()
        await _turn(agent, "s1")

        agent._session_tracker.clear()

        assert not agent._session_tracker._started and not agent._session_tracker._held
