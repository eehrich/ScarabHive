"""Tests for web_research_agent plugin."""

import pytest
from plugins.web_research_agent.plugin import PLUGIN_FACTORY


@pytest.mark.asyncio
async def test_plugin_discovery():
    """Test that the plugin can be discovered and instantiated.

    Use locally built config objects to avoid relying on shared test fixtures
    that can introduce global state between tests.
    """
    from agent_system.config.models import AgentSystemConfig, MCPConfig, AgentConfig

    # Create fresh configs for isolation
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="web_research_agent", enabled=True, agent_config=AgentConfig())

    # Test factory instantiation with modern signature
    factory = PLUGIN_FACTORY
    server = factory("test_web_research", system_config, mcp_config)

    assert server.name == "test_web_research"
    
    # Test tools (multi-tool format)
    tools = server.get_tools()
    assert isinstance(tools, list)
    assert len(tools) >= 1
    
    tool_names = [tool["function"]["name"] for tool in tools]
    # Validate a subset of expected tools are present
    assert any("web_research" in n for n in tool_names), f"Unexpected tools: {tool_names}"


@pytest.mark.asyncio
async def test_plugin_call():
    """Test basic plugin call functionality using isolated configs."""
    from agent_system.config.models import AgentSystemConfig, MCPConfig, AgentConfig

    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(type="web_research_agent", enabled=True, agent_config=AgentConfig())

    factory = PLUGIN_FACTORY
    server = factory("test_web_research", system_config, mcp_config)

    # Test error handling for missing parameters
    result = await server.call("web_research_agent", {})
    assert result["status"] == "error"
    assert "Missing required parameter" in result["error"]

    # Test fact_check error handling
    result = await server.call("fact_check_agent", {})
    assert result["status"] == "error"
    assert "Missing required parameter" in result["error"]

    # Test compare_sources error handling
    result = await server.call("source_analysis_agent", {})
    assert result["status"] == "error"
    assert "Missing required parameter" in result["error"]
