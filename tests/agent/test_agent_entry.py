"""entry_agent: the one rule for "the agent a run starts on, by name".

The API, agent-cli, agent-run, the chat's /agent and the writer plugins each
had their own copy; they differed in what they rewired, what they gated on and
what they said. These tests pin the shared rule.
"""
from __future__ import annotations

import pytest

from agent_system.config.models import (
    AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
    PluginsConfig, ToolServerConfig,
)
from agent_system.servers.agent.entry import NotAnAgent, agent_entry_names, entry_agent
from agent_system.tools.base import ToolServerRegistry

DEFAULT_ONLY_TEMPLATE = "from-the-default-block.md"


def _config() -> AgentSystemConfig:
    """default_config and the entry differ on purpose: a build from either
    half alone gets max_steps or the template wrong."""
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
            profiles={"normal": LLMProfile(model_ref="m")},
            default_profile="normal",
        ),
        plugins=PluginsConfig(
            default_config=ToolServerConfig(agent_config=AgentConfig(
                max_steps=7, system_template=DEFAULT_ONLY_TEMPLATE)),
            servers={
                "probe": ToolServerConfig(type="agent", enabled=True,
                                          agent_config=AgentConfig(llm_profile="normal", max_steps=99)),
                "tools_only": ToolServerConfig(type="file_ops", enabled=True),
            },
        ),
    )


def test_a_fresh_build_uses_the_merged_config_and_is_registered():
    registry = ToolServerRegistry()

    agent = entry_agent("probe", _config(), registry)

    assert agent.agent_config.max_steps == 99, "the entry's own value was lost"
    assert agent.agent_config.system_template == DEFAULT_ONLY_TEMPLATE, "built from the raw entry alone"
    assert registry.get("probe") is agent


def test_a_registered_agent_is_reused_and_rewired():
    config = _config()
    built = entry_agent("probe", config, ToolServerRegistry(), session_service="first")
    later = ToolServerRegistry()
    later.register("probe", built)

    kept = entry_agent("probe", config, later)
    assert kept is built, "a registered agent was built again"
    assert kept.registry is later, "still wired to the registry that built it"
    assert kept._session_service == "first", "no service given must keep the one it has"

    entry_agent("probe", config, later, session_service="second")
    assert built._session_service == "second", "a given service did not reach the agent"


def test_a_registered_server_that_is_no_agent_stays_registered():
    registry = ToolServerRegistry()
    tool_server = object()
    registry.register("probe", tool_server)

    with pytest.raises(NotAnAgent, match="registered as object, not an Agent"):
        entry_agent("probe", _config(), registry)
    assert registry.get("probe") is tool_server


def test_the_gate_is_the_raw_entry_not_the_merged_one():
    """default_config gives every MERGED entry an agent_config -- a gate on it
    would build an agent for any tool server."""
    config = _config()
    assert "tools_only" not in agent_entry_names(config)

    with pytest.raises(NotAnAgent, match="'tools_only' is a tool server") as refused:
        entry_agent("tools_only", config, ToolServerRegistry())
    assert refused.value.available == ["probe"]
    assert "Available agents:\n  probe" in str(refused.value)


def test_an_unknown_name_names_the_agents_there_are():
    with pytest.raises(NotAnAgent, match="Agent 'nowhere' not found") as refused:
        entry_agent("nowhere", _config(), ToolServerRegistry())
    assert "probe" in str(refused.value)
    assert isinstance(refused.value, ValueError), "callers (agent-run, writer plugins) catch ValueError"
