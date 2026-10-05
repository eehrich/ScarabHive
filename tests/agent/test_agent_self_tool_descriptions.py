"""self_tool_descriptions reach an agent's tools.

SchemaBasedAgent overrides ToolServer.list_tools and skipped the instance's
texts: sysadmin_agent, coder, research_agent and the rest showed the model
basic_agent's generic descriptions.
"""

import logging

from agent_system.config.models import AgentConfig, AgentSystemConfig, ToolServerConfig
from plugins.basic_agent.plugin import PLUGIN_FACTORY


def _agent(name, descriptions):
    config = ToolServerConfig(type="basic_agent", enabled=True, agent_config=AgentConfig(max_steps=5),
                              self_tool_descriptions=descriptions)
    return PLUGIN_FACTORY(name, AgentSystemConfig(), config)


async def test_the_instance_description_reaches_the_tool():
    agent = _agent("ops", {"ops_execute_task": "Run an ops task over SSH."})
    tools = {tool.name: tool.description for tool in await agent.list_tools()}
    assert tools["ops_execute_task"] == "Run an ops task over SSH."


async def test_only_an_own_unknown_key_is_warned_about(caplog):
    # gamedev: {type: coder} inherits coder_execute_task -- not a typo of its own.
    agent = _agent("ops", {"coder_execute_task": "inherited", "ops_exeucte_task": "typo"})
    with caplog.at_level(logging.WARNING, logger="agent_system.tools.base"):
        await agent.list_tools()
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ops_exeucte_task" in m for m in warned)
    assert not any("coder_execute_task" in m for m in warned)
