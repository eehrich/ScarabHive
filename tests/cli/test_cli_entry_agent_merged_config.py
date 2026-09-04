"""The CLI's fallback build of the entry agent must use the MERGED server
config -- default_config and the ``type:`` chain applied, like bootstrap does
for every other agent -- not the raw ``plugins.servers[name]`` entry.
"""
from __future__ import annotations

import pytest

from agent_system.agent_cli import _build_entry_agent
from agent_system.config.models import (
    AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
    MCPConfig, PluginsConfig,
)
from agent_system.mcp.base import MCPRegistry


def _config(*, max_steps_in_default_config: int) -> AgentSystemConfig:
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
            profiles={"normal": LLMProfile(model_ref="m")},
            default_profile="normal",
        ),
        plugins=PluginsConfig(
            default_config=MCPConfig(agent_config=AgentConfig(max_steps=max_steps_in_default_config)),
            servers={
                # the raw entry says nothing about max_steps -- only the merge does
                "probe": MCPConfig(type="agent", enabled=True,
                                   agent_config=AgentConfig(llm_profile="normal")),
            },
        ),
    )


def test_entry_agent_is_built_from_the_merged_config():
    merged_value = 77
    assert merged_value != AgentConfig().max_steps, "fixture: pick a value that is not the field default"
    registry = MCPRegistry()

    agent = _build_entry_agent("probe", _config(max_steps_in_default_config=merged_value), registry, None)

    assert agent.agent_config.max_steps == merged_value, (
        "the entry agent was built from the raw server entry, not the merged config")
    assert registry.get("probe") is agent


def test_unknown_entry_agent_exits_with_a_listing(capsys):
    with pytest.raises(SystemExit):
        _build_entry_agent("nope", _config(max_steps_in_default_config=5), MCPRegistry(), None)
    assert "Available agents" in capsys.readouterr().err


def test_a_tool_server_name_still_exits_instead_of_becoming_an_agent():
    """The "not an agent" gate has to read the RAW entry.

    plugins.default_config carries an agent_config (real config: every server
    merges one in), so a gate on the merged config waves through any tool
    server name and silently runs the task under a bare Agent of that name.
    Measured on the real config: 96 of 219 servers have no raw agent_config,
    all of them tool servers.
    """
    config = _config(max_steps_in_default_config=5)
    config.plugins.servers["file_ops"] = MCPConfig(type="file_ops", enabled=False)
    from agent_system.config.settings import get_mcp_config_by_name
    merged = get_mcp_config_by_name("file_ops", config)
    assert merged and merged.agent_config, "fixture: the merge must supply an agent_config here"

    with pytest.raises(SystemExit):
        _build_entry_agent("file_ops", config, MCPRegistry(), None)
