"""An agent asking what it can do must see hybrid plugins too.

_list_usable_tools_with_details only asked servers for get_tools(). That is the
LEGACY fallback -- ToolSchemaBuilder tries the modern async list_tools() FIRST
and only then get_tools(). Every hybrid plugin (all sub_agent_manager instances
among them) implements list_tools() and NOT get_tools(), so they were silently
skipped: the LLM schema contained the tool while the agent's own inventory said
it did not exist.
"""
import pytest

from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    MCPConfig,
    ToolConfig,
)
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.agent.server import Agent


class _MCPToolLike:
    """Shape of what list_tools() returns (MCPTool)."""

    def __init__(self, name, description="", input_schema=None):
        self.name = name
        self.description = description
        # The real MCPTool always carries one; the schema builder reads it
        # unconditionally, so a fake without it vanishes from the result.
        self.input_schema = input_schema or {"type": "object", "properties": {}}


class _HybridServer:
    """Async list_tools(), deliberately NO get_tools() -- like the real
    SubAgentManagerHybridPlugin."""

    def __init__(self, tools):
        self._tools = tools

    async def list_tools(self):
        return self._tools


class _LegacyServer:
    """Only the sync get_tools() -- must keep working."""

    def __init__(self, tools):
        self._tools = tools

    def get_tools(self):
        return self._tools


def _llm_system():
    return LLMSystemConfig(
        models={"m": LLMModelConfig(provider="openai", model="m", api_key="k")},
        profiles={"normal": LLMProfile(model_ref="m")},
        default_profile="normal",
    )


def _agent_with(registry, allowed):
    agent_config = AgentConfig(max_steps=1,
                               tools=ToolConfig(allowed=allowed))
    mcp_config = MCPConfig(type="agent", enabled=True, agent_config=agent_config)
    return Agent("test_agent", AgentSystemConfig(llm_system=_llm_system()),
                 mcp_config, registry)


@pytest.mark.asyncio
async def test_hybrid_plugin_tools_are_listed():
    registry = MCPRegistry()
    registry.register("some_sam", _HybridServer(
        [_MCPToolLike("some_sam_manage_sub_agent", "spawn sub-agents")]))
    agent = _agent_with(registry, ["some_sam/*"])

    tools = await agent._list_usable_tools_with_details({})
    names = [t["name"] for t in tools]
    assert "some_sam_manage_sub_agent" in names
    assert tools[0]["description"] == "spawn sub-agents"


@pytest.mark.asyncio
async def test_legacy_get_tools_still_works():
    registry = MCPRegistry()
    registry.register("old_plugin", _LegacyServer([
        {"type": "function",
         "function": {"name": "old_plugin_do", "description": "legacy"}}]))
    agent = _agent_with(registry, ["old_plugin/*"])

    tools = await agent._list_usable_tools_with_details({})
    assert [t["name"] for t in tools] == ["old_plugin_do"]
    assert tools[0]["description"] == "legacy"


@pytest.mark.asyncio
async def test_both_kinds_appear_together():
    """The mixed case is the real one -- and the one that under-reported."""
    registry = MCPRegistry()
    registry.register("hybrid", _HybridServer([_MCPToolLike("hybrid_tool")]))
    registry.register("legacy", _LegacyServer([
        {"type": "function", "function": {"name": "legacy_tool", "description": ""}}]))
    agent = _agent_with(registry, ["hybrid/*", "legacy/*"])

    names = {t["name"] for t in await agent._list_usable_tools_with_details({})}
    assert names == {"hybrid_tool", "legacy_tool"}


@pytest.mark.asyncio
async def test_a_broken_server_does_not_hide_the_others():
    class _Broken:
        async def list_tools(self):
            raise RuntimeError("kaputt")

    registry = MCPRegistry()
    registry.register("broken", _Broken())
    registry.register("fine", _HybridServer([_MCPToolLike("fine_tool")]))
    agent = _agent_with(registry, ["broken/*", "fine/*"])

    names = [t["name"] for t in await agent._list_usable_tools_with_details({})]
    assert names == ["fine_tool"]


@pytest.mark.asyncio
async def test_allowlist_still_filters():
    registry = MCPRegistry()
    registry.register("wanted", _HybridServer([_MCPToolLike("wanted_tool")]))
    registry.register("unwanted", _HybridServer([_MCPToolLike("unwanted_tool")]))
    agent = _agent_with(registry, ["wanted/*"])

    names = [t["name"] for t in await agent._list_usable_tools_with_details({})]
    assert names == ["wanted_tool"]


@pytest.mark.asyncio
async def test_broken_list_tools_falls_back_to_get_tools():
    """A failing list_tools() must not LOSE the server when the legacy source
    still works -- the silent skip was exactly how this class of bug hid."""
    class _BrokenModernWorkingLegacy:
        async def list_tools(self):
            raise TypeError("kaputtes async")

        def get_tools(self):
            return [{"type": "function",
                     "function": {"name": "rescued_tool", "description": "via legacy"}}]

    registry = MCPRegistry()
    registry.register("wobbly", _BrokenModernWorkingLegacy())
    agent = _agent_with(registry, ["wobbly/*"])

    tools = await agent._list_usable_tools_with_details({})
    assert [t["name"] for t in tools] == ["rescued_tool"]


@pytest.mark.asyncio
async def test_a_listing_that_fails_says_so():
    """Every failure used to come back as [] -- read by the chat and the web
    as "this agent has no tools (tools.allowed is empty)"."""
    agent = _agent_with(MCPRegistry(), ["*"])

    async def broken():
        raise RuntimeError("discovery broke")

    agent.list_usable_tools = broken
    with pytest.raises(RuntimeError, match="discovery broke"):
        await agent._list_usable_tools_with_details({})
