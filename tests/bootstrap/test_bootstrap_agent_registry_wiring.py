"""A config agent's tool executor must hold the SHARED registry.

``make_agent_plugin_factory`` built every agent with a throwaway
``ToolServerRegistry()``; bootstrap then replaced ``inst.registry`` -- but the
``ToolExecutionManager`` had already captured the empty one in its
constructor (``tool_execution.py``: "Legacy registry (empty for now)"), and
its fallbacks resolve tools through exactly that attribute.
"""
from __future__ import annotations

from agent_system.config.models import (
    AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
    ToolServerConfig, PluginsConfig,
)
from agent_system.tools.base import ToolServerRegistry
from agent_system.servers.bootstrap import bootstrap_servers


def _config() -> AgentSystemConfig:
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
            profiles={"normal": LLMProfile(model_ref="m")},
            default_profile="normal",
        ),
        plugins=PluginsConfig(servers={
            "probe_agent": ToolServerConfig(type="basic_agent", enabled=True,
                                     agent_config=AgentConfig(llm_profile="normal")),
            "probe_direct": ToolServerConfig(type="agent", enabled=True,
                                      agent_config=AgentConfig(llm_profile="normal")),
        }),
    )


def test_plugin_agents_are_built_with_the_shared_registry():
    registry = ToolServerRegistry()
    bootstrap_servers(_config(), registry)

    agent = registry.get("probe_agent")
    assert agent.registry is registry
    assert agent._tool_execution_manager.registry is registry, (
        "the tool executor still holds the factory's throwaway registry")


def test_direct_agents_are_built_with_the_shared_registry_too():
    """The ``type: agent`` branch built its agent with a private
    ``ToolServerRegistry()`` and never wired the shared one at all."""
    registry = ToolServerRegistry()
    bootstrap_servers(_config(), registry)

    agent = registry.get("probe_direct")
    assert agent.registry is registry
    assert agent._tool_execution_manager.registry is registry
