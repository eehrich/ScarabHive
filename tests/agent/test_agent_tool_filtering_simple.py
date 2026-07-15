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
    # list_usable_tools now returns (tools, blocked_patterns)
    filtered, _, blocked_patterns = await agent.list_usable_tools()  # type: ignore[attr-defined]
    assert filtered == []
    # blocked_patterns can be None or empty list when no blocked patterns configured
    assert blocked_patterns is None or blocked_patterns == []

def test_filter_patterns():
    """Discovery-Stufen-Semantik des geteilten Matchers (server_matches_patterns)
    — ehemals über das entfernte Agent._filter_usable_tools getestet (G5)."""
    from agent_system.servers.agent.tool_schema_builder import server_matches_patterns

    patterns = [
        "web_scraper/*",          # ganze Plugin Tools
        "duckduckgo_search",      # plugin Short-Hand
        "weather.get_forecast",   # einzelnes externes Tool
        "datetime.*"              # alle datetime.*
    ]

    tools = ["web_scraper", "web_scraper.scrape", "duckduckgo_search", "weather.get_forecast", "weather.get_temperature", "datetime.get_time", "datetime.other", "other"]
    effective = [t for t in tools if server_matches_patterns(t, patterns)]
    assert "duckduckgo_search" in effective
    assert "web_scraper" in effective
    # STRIKT: dotted External-Namen brauchen die Dot-Form ("web_scraper.*");
    # "server/*" deckt nur den Server selbst (Pass-Through), nicht dessen
    # dotted Externals — Discovery-Gate-Semantik (siehe server_matches_patterns).
    assert "web_scraper.scrape" not in effective
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


@pytest.mark.asyncio
async def test_blocked_tools_returned_with_allowed():
    """Test that blocked_patterns is returned correctly alongside allowed tools."""
    # Create minimal system config
    system_config = Mock(spec=AgentSystemConfig)
    system_config.llm_system = LLMSystemConfig(
        models={"gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"}},
        profiles={"default": {"model_ref": "gpt-4o-mini"}}
    )
    
    # AgentConfig with both allowed and blocked tools
    tool_config = ToolConfig(
        allowed=["writer_graph/*", "web_scraper/*"],
        blocked=["writer_graph/writer_graph_batch_link"]
    )
    agent_config = AgentConfig(llm_profile="default", tools=tool_config)
    mcp_config = MCPConfig(type="basic_agent", enabled=True, agent_config=agent_config)
    
    registry = MCPRegistry()
    agent = Agent("test_agent", system_config, mcp_config, registry)
    
    # list_usable_tools should return blocked_patterns
    tools, _, blocked_patterns = await agent.list_usable_tools()  # type: ignore[attr-defined]
    
    # Blocked patterns should be returned for later application
    assert blocked_patterns is not None
    assert "writer_graph/writer_graph_batch_link" in blocked_patterns


@pytest.mark.asyncio
async def test_empty_allowed_list_denies_all_tools():
    """Test that allowed=[] explicitly denies all tools (security by default)."""
    system_config = Mock(spec=AgentSystemConfig)
    system_config.llm_system = LLMSystemConfig(
        models={"gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"}},
        profiles={"default": {"model_ref": "gpt-4o-mini"}}
    )
    
    # Explicitly set allowed to empty list
    tool_config = ToolConfig(allowed=[])
    agent_config = AgentConfig(llm_profile="default", tools=tool_config)
    mcp_config = MCPConfig(type="basic_agent", enabled=True, agent_config=agent_config)
    
    registry = MCPRegistry()
    agent = Agent("test_agent", system_config, mcp_config, registry)
    
    # Should return empty list - no tools allowed
    filtered, _, _ = await agent.list_usable_tools()  # type: ignore[attr-defined]
    assert filtered == []


@pytest.mark.asyncio
async def test_none_allowed_denies_all_tools():
    """Test that allowed=None (not configured) denies all tools by default."""
    system_config = Mock(spec=AgentSystemConfig)
    system_config.llm_system = LLMSystemConfig(
        models={"gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"}},
        profiles={"default": {"model_ref": "gpt-4o-mini"}}
    )
    
    # No tools config at all
    agent_config = AgentConfig(llm_profile="default")
    mcp_config = MCPConfig(type="basic_agent", enabled=True, agent_config=agent_config)
    
    registry = MCPRegistry()
    agent = Agent("test_agent", system_config, mcp_config, registry)
    
    # Should return empty list - security by default
    filtered, _, _ = await agent.list_usable_tools()  # type: ignore[attr-defined]
    assert filtered == []


@pytest.mark.asyncio
async def test_wildcard_allows_all_tools():
    """Test that allowed=["*"] explicitly allows all tools."""
    system_config = Mock(spec=AgentSystemConfig)
    system_config.llm_system = LLMSystemConfig(
        models={"gpt-4o-mini": {"provider": "openai", "model": "gpt-4o-mini"}},
        profiles={"default": {"model_ref": "gpt-4o-mini"}}
    )
    
    # Wildcard to allow all
    tool_config = ToolConfig(allowed=["*"])
    agent_config = AgentConfig(llm_profile="default", tools=tool_config)
    mcp_config = MCPConfig(type="basic_agent", enabled=True, agent_config=agent_config)
    
    registry = MCPRegistry()
    
    # Register a mock tool server
    mock_server = Mock()
    mock_server.schema = {"tools": [{"name": "test_tool"}, {"name": "another_tool"}]}
    registry.register("mock_server", mock_server)
    
    agent = Agent("test_agent", system_config, mcp_config, registry)
    
    # Should include the mock_server when wildcard is used
    filtered, _, _ = await agent.list_usable_tools()  # type: ignore[attr-defined]
    assert "mock_server" in filtered
