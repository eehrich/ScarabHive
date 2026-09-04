"""The API's fallback build of the entry agent, like the CLI's, must use the
MERGED server config -- and must not push a registered server out of the
registry to do it.
"""
from __future__ import annotations

import pytest

from agent_system.app import _build_entry_agent
from agent_system.config.models import (
    AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
    MCPConfig, PluginsConfig,
)
from agent_system.mcp.base import MCPRegistry


DEFAULT_ONLY_TEMPLATE = "from-the-default-block.md"


def _config(*, max_steps_in_default_config: int, own_max_steps: int | None = 99) -> AgentSystemConfig:
    """The two halves of the merge are deliberately DIFFERENT.

    default_config carries a value the server entry does not (the template) and
    a max_steps the entry overrides. A build from default_config alone gets the
    wrong max_steps; a build from the raw entry alone misses the template. Both
    mistakes are therefore visible.
    """
    return AgentSystemConfig(
        default_agent="probe",
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
            profiles={"normal": LLMProfile(model_ref="m")},
            default_profile="normal",
        ),
        plugins=PluginsConfig(
            default_config=MCPConfig(agent_config=AgentConfig(
                max_steps=max_steps_in_default_config, system_template=DEFAULT_ONLY_TEMPLATE)),
            servers={
                "probe": MCPConfig(type="agent", enabled=True,
                                   agent_config=AgentConfig(llm_profile="normal",
                                                            max_steps=own_max_steps)),
            },
        ),
    )


def test_the_entry_agent_is_built_from_the_merged_config():
    default_steps, own_steps = 77, 99
    assert own_steps != AgentConfig().max_steps, "fixture: pick a value that is not the field default"
    registry = MCPRegistry()
    config = _config(max_steps_in_default_config=default_steps, own_max_steps=own_steps)

    agent = _build_entry_agent("probe", config, registry, None)

    assert agent.agent_config.max_steps == own_steps, (
        f"got {agent.agent_config.max_steps} -- the entry agent was built from "
        f"plugins.default_config alone, its own server entry was ignored")
    assert agent.agent_config.system_template == DEFAULT_ONLY_TEMPLATE, (
        "built from the raw entry alone: what only default_config carries never arrived")
    assert registry.get("probe") is agent


def test_a_registered_server_of_that_name_is_not_replaced(caplog):
    """Reachable whenever default_agent names something that is registered but
    is not an Agent: registering over it would take that server out of the
    registry for the rest of the process."""
    registry = MCPRegistry()
    tool_server = object()
    registry.register("probe", tool_server)

    agent = _build_entry_agent("probe", _config(max_steps_in_default_config=9), registry, None)

    assert registry.get("probe") is tool_server, "the registered server was overwritten"
    assert agent.name == "probe", "the caller still needs an entry agent to run with"
    assert "collides" in caplog.text.lower(), "the collision must be visible, not silent"


@pytest.mark.parametrize("servers", [{}, None])
def test_an_unknown_name_falls_back_to_default_config(servers):
    """No entry for the name: the default block is the last thing left, and it
    still has to produce a usable agent (this is the API, it cannot exit)."""
    config = _config(max_steps_in_default_config=42)
    if servers is None:
        config.plugins.servers = {}
    else:
        config.plugins.servers = servers

    agent = _build_entry_agent("nowhere", config, MCPRegistry(), None)

    assert agent.agent_config.max_steps == 42
