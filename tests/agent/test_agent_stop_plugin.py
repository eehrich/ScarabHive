"""Stopping an agent leaves the process's state and a running request's state alone.

Agents are plugins (``basic_agent``, the writer pipelines), so the framework
asks them to stop through ``capabilities.stop_plugin`` -- at app shutdown,
from ``shutdown_tools()``. Their teardown used to be ``Agent.shutdown()``, a
name nothing called, and it did two things that must not happen there:

* it shut down the tool integration if this agent had been the first to
  initialize it. That integration is the module-level singleton every agent
  of the process uses; its owner is the process entry point, which calls
  ``shutdown_tools()`` on its way out (tests/cli/test_agent_run_session_defaults.py
  pins that for agent-run, the one entry point that did not). Reached, it
  would have stopped every plugin in the process from one agent's stop, and
  from ``shutdown_all`` it would have gone round into ``shutdown_all`` again.
* it cleared the session tracker -- the conversations in memory AND the
  session locks. ``shutdown_tools()`` does not wait for background jobs, so a
  run still in flight at shutdown would have lost the lock that keeps a second
  request off its session. Clearing memory the process is about to give back
  buys nothing.

An agent owns nothing that needs a teardown, so it has none. These tests pin
that stopping one is harmless, whichever name a future hook gets.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolServerConfig,
)
from agent_system.plugins import capabilities
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry


def _agent() -> Agent:
    llm = LLMSystemConfig(
        models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake-key")},
        profiles={"normal": LLMProfile(model_ref="m")},
        default_profile="normal",
    )
    server_config = ToolServerConfig(
        type="agent", enabled=True, agent_config=AgentConfig(llm_profile="normal"))
    return Agent("an_agent", AgentSystemConfig(llm_system=llm), server_config,
                 ToolServerRegistry())


@pytest.mark.asyncio
async def test_stopping_an_agent_leaves_the_shared_integration_alone():
    """The integration belongs to the process, however it came to be set up."""
    agent = _agent()
    shared = MagicMock()
    shared.shutdown = AsyncMock()
    shared.plugin_registry.shutdown_all = AsyncMock()
    agent._tool_integration_manager.tool_integration = shared

    await capabilities.stop_plugin(agent)

    shared.shutdown.assert_not_awaited()
    shared.plugin_registry.shutdown_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopping_an_agent_leaves_a_running_request_its_session(monkeypatch):
    """The tracker holds the session locks of runs that may still be going."""
    agent = _agent()
    cleared = []
    monkeypatch.setattr(agent._session_tracker, "clear", lambda: cleared.append(True))

    await capabilities.stop_plugin(agent)

    assert not cleared, "stopping the agent wiped the session state of its runs"


def test_no_agent_teardown_lives_under_a_name_nothing_calls():
    """`shutdown` was the name nothing called; it must not come back."""
    assert not hasattr(Agent, "shutdown"), (
        "Agent has a `shutdown` again -- capabilities.stop_plugin never calls it")
