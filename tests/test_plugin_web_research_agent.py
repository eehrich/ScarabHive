"""Tests for web_research_agent plugin."""

import pytest
from plugins.web_research_agent.plugin import PLUGIN_FACTORY


@pytest.mark.asyncio
async def test_plugin_discovery(mock_system_config, mock_mcp_config):
    """Test that the plugin can be discovered and instantiated."""
    # Test factory instantiation with modern signature
    factory = PLUGIN_FACTORY
    server = factory("test_web_research", mock_system_config, mock_mcp_config)

    assert server.name == "test_web_research"
    
    # Test tools (multi-tool format)
    tools = server.get_tools()
    assert isinstance(tools, list)
    assert len(tools) == 4  # web_research, verify_claim, analyze_sources, intelligent_research
    
    tool_names = [tool["function"]["name"] for tool in tools]
    assert "web_research_agent" in tool_names
    assert "fact_check_agent" in tool_names
    assert "source_analysis_agent" in tool_names
    assert "research_assistant_agent" in tool_names


@pytest.mark.asyncio
async def test_plugin_call(mock_system_config, mock_mcp_config):
    """Test basic plugin call functionality."""
    factory = PLUGIN_FACTORY
    server = factory("test_web_research", mock_system_config, mock_mcp_config)

    # Test error handling for missing parameters
    result = await server.call("web_research_agent", {})
    assert result["status"] == "error"
    assert "Missing required parameter 'topic'" in result["error"]

    # Test fact_check error handling
    result = await server.call("fact_check_agent", {})
    assert result["status"] == "error"
    assert "Missing required parameter 'claim'" in result["error"]

    # Test compare_sources error handling
    result = await server.call("source_analysis_agent", {})
    assert result["status"] == "error"
    assert "Missing required parameter 'topic'" in result["error"]
