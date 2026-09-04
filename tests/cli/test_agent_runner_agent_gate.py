"""``create_and_register_agent`` must refuse a name that is not an agent.

Two ways it used to say yes: the gate read ``mcp_config.agent_config`` from
the MERGED config, and plugins.default_config carries one, so every tool
server passed it. And a name already held by a tool server was registered
over, taking that server out of the registry for the rest of the process --
in ``agent-run`` and in the writer audio pipeline, which both build agents
through this function.
"""
from __future__ import annotations

import pytest

from agent_system.cli_utils.agent_runner import create_and_register_agent
from agent_system.config.models import (
    AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
    MCPConfig, PluginsConfig,
)
from agent_system.mcp.base import MCPRegistry

pytestmark = pytest.mark.anyio


def _config() -> AgentSystemConfig:
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
            profiles={"normal": LLMProfile(model_ref="m")},
            default_profile="normal",
        ),
        plugins=PluginsConfig(
            default_config=MCPConfig(agent_config=AgentConfig(max_steps=55)),
            servers={
                "real_agent": MCPConfig(type="agent", enabled=True,
                                        agent_config=AgentConfig(llm_profile="normal")),
                "file_ops": MCPConfig(type="file_ops", enabled=True),
            },
        ),
    )


async def test_an_agent_is_built_from_the_merged_config():
    config = _config()
    registry = MCPRegistry()

    agent = await create_and_register_agent(config, registry, "real_agent")

    assert agent.agent_config.max_steps == 55, "built from the raw entry, not the merged config"
    assert registry.get("real_agent") is agent


async def test_a_tool_server_name_is_refused():
    config = _config()
    from agent_system.config.settings import get_mcp_config_by_name
    merged = get_mcp_config_by_name("file_ops", config)
    assert merged and merged.agent_config, "fixture: the merge must supply an agent_config here"

    with pytest.raises(ValueError, match="tool server"):
        await create_and_register_agent(config, MCPRegistry(), "file_ops")


async def test_a_registered_non_agent_is_not_overwritten():
    registry = MCPRegistry()
    tool_server = object()
    registry.register("file_ops", tool_server)

    with pytest.raises(ValueError, match="not an Agent"):
        await create_and_register_agent(_config(), registry, "file_ops")

    assert registry.get("file_ops") is tool_server


async def test_an_unknown_name_still_lists_the_available_agents():
    with pytest.raises(ValueError, match="Available agents") as err:
        await create_and_register_agent(_config(), MCPRegistry(), "nowhere")
    assert "real_agent" in str(err.value)
