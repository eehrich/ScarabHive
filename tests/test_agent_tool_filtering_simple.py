import pytest

from agent_system.config.models import AgentConfig, LLMSystemConfig
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.agent.server import Agent

@pytest.mark.asyncio
async def test_filter_no_patterns_all_available(monkeypatch):
    # Minimal LLM system config (dummy)
    llm_system = {
        "models": {"gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"}},
        "profiles": {"default": {"model_ref": "gpt-4o-mini"}},
        "default_profile": "default"
    }
    cfg = AgentConfig(llm_system=LLMSystemConfig(**llm_system))
    registry = MCPRegistry()
    agent = Agent("test_agent", cfg, registry, {})

    # Neue Policy: Keine allowed_tools -> keine Tools erlaubt
    filtered = await agent.list_allowed_tool_servers()  # type: ignore[attr-defined]
    assert filtered == []

@pytest.mark.asyncio
async def test_filter_patterns():
    llm_system = {
        "models": {"gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"}},
        "profiles": {"default": {"model_ref": "gpt-4o-mini"}},
        "default_profile": "default"
    }
    cfg = AgentConfig(llm_system=LLMSystemConfig(**llm_system), allowed_tools=[
        "web_scraper/*",          # ganze Plugin Tools
        "duckduckgo_search",      # plugin Short-Hand
        "weather.get_forecast",   # einzelnes externes Tool
        "datetime.*"               # alle datetime.*
    ])
    registry = MCPRegistry()
    agent = Agent("test_agent", cfg, registry, {})

    tools = ["web_scraper", "web_scraper.scrape", "duckduckgo_search", "weather.get_forecast", "weather.get_temperature", "datetime.get_time", "datetime.other", "other"]
    effective = agent._filter_available_tools(tools, cfg.allowed_tools)  # type: ignore[attr-defined]
    assert "duckduckgo_search" in effective
    assert "web_scraper" in effective
    assert "web_scraper.scrape" in effective
    assert "weather.get_forecast" in effective
    assert "weather.get_temperature" not in effective
    assert "datetime.get_time" in effective and "datetime.other" in effective
    assert "other" not in effective

@pytest.mark.asyncio
async def test_is_tool_allowed_edge_cases():
    llm_system = {
        "models": {"gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"}},
        "profiles": {"default": {"model_ref": "gpt-4o-mini"}},
        "default_profile": "default"
    }
    cfg = AgentConfig(llm_system=LLMSystemConfig(**llm_system), allowed_tools=["*"])
    registry = MCPRegistry()
    agent = Agent("test_agent", cfg, registry, {})
    assert agent._is_tool_allowed("anything", ["*"])  # type: ignore[attr-defined]
