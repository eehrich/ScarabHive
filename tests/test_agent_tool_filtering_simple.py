import pytest
from unittest.mock import Mock

from agent_system.config.models import AgentSystemConfig, LLMSystemConfig, MCPConfig, AgentConfig, ToolConfig
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.agent.server import Agent

@pytest.mark.asyncio
async def test_filter_no_patterns_all_available(monkeypatch):
    # Create minimal system config with LLM system
    system_config = Mock(spec=AgentSystemConfig)
    system_config.llm_system = LLMSystemConfig(
        models={"gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"}},
        profiles={"default": {"model_ref": "gpt-4o-mini"}}
    )
    
    # AgentConfig with NO tools.allowed list -> deny all
    agent_config = AgentConfig(llm_profile="default")
    mcp_config = MCPConfig(type="basic_agent", enabled=True, agent_config=agent_config)
    
    registry = MCPRegistry()
    agent = Agent("test_agent", system_config, mcp_config, registry)

    # Neue Policy: Keine tools.allowed -> keine Tools erlaubt
    filtered = await agent.list_allowed_tool_servers()  # type: ignore[attr-defined]
    assert filtered == []

@pytest.mark.asyncio
async def test_filter_patterns():
    # Create minimal system config
    system_config = Mock(spec=AgentSystemConfig)
    system_config.llm_system = LLMSystemConfig(
        models={"gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"}},
        profiles={"default": {"model_ref": "gpt-4o-mini"}}
    )
    
    # AgentConfig with specific tool patterns
    tool_config = ToolConfig(
        allowed=[
            "web_scraper/*",          # ganze Plugin Tools
            "duckduckgo_search",      # plugin Short-Hand
            "weather.get_forecast",   # einzelnes externes Tool
            "datetime.*"               # alle datetime.*
        ]
    )
    agent_config = AgentConfig(llm_profile="default", tools=tool_config)
    mcp_config = MCPConfig(type="basic_agent", enabled=True, agent_config=agent_config)
    
    registry = MCPRegistry()
    agent = Agent("test_agent", system_config, mcp_config, registry)

    tools = ["web_scraper", "web_scraper.scrape", "duckduckgo_search", "weather.get_forecast", "weather.get_temperature", "datetime.get_time", "datetime.other", "other"]
    effective = agent._filter_available_tools(tools, agent_config.tools.allowed)  # type: ignore[attr-defined]
    assert "duckduckgo_search" in effective
    assert "web_scraper" in effective
    assert "web_scraper.scrape" in effective
    assert "weather.get_forecast" in effective
    assert "weather.get_temperature" not in effective
    assert "datetime.get_time" in effective and "datetime.other" in effective
    assert "other" not in effective

@pytest.mark.asyncio
async def test_is_tool_allowed_edge_cases():
    # Create minimal system config
    system_config = Mock(spec=AgentSystemConfig)
    system_config.llm_system = LLMSystemConfig(
        models={"gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"}},
        profiles={"default": {"model_ref": "gpt-4o-mini"}}
    )
    
    # AgentConfig with wildcard to allow all tools
    tool_config = ToolConfig(allowed=["*"])
    agent_config = AgentConfig(llm_profile="default", tools=tool_config)
    mcp_config = MCPConfig(type="basic_agent", enabled=True, agent_config=agent_config)
    
    registry = MCPRegistry()
    agent = Agent("test_agent", system_config, mcp_config, registry)
    assert agent._is_tool_allowed("anything", ["*"])  # type: ignore[attr-defined]
