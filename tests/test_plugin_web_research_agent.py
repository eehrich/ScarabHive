"""Tests for web_research_agent plugin."""

import pytest
from plugins.web_research_agent.plugin import PLUGIN_NAME, PLUGIN_FACTORY


def test_plugin_discovery():
    """Test that the plugin can be discovered and instantiated."""
    assert PLUGIN_NAME == "web_research_agent"

    # Test factory instantiation
    factory = PLUGIN_FACTORY
    server = factory("test_web_research", {}, ssl_verify=False)

    assert server.name == "test_web_research"
    assert server.cfg == {}
    assert server.ssl_verify is False

    # Test schema
    schema = server.get_schema()
    assert isinstance(schema, dict)
    assert schema["type"] == "function"
    assert "function" in schema
    assert schema["function"]["name"] == "test_web_research"

    # Test default action
    default_action = server.get_default_action()
    assert default_action == "research"


@pytest.mark.asyncio
async def test_plugin_call():
    """Test basic plugin call functionality."""
    factory = PLUGIN_FACTORY
    server = factory("test_web_research", {}, ssl_verify=False)

    # Test error handling for missing parameters
    result = await server.call("research", {})
    assert result["status"] == "error"
    assert "Missing required parameter 'topic'" in result["error"]

    # Test fact_check error handling
    result = await server.call("fact_check", {})
    assert result["status"] == "error"
    assert "Missing required parameter 'claim'" in result["error"]

    # Test compare_sources error handling
    result = await server.call("compare_sources", {})
    assert result["status"] == "error"
    assert "Missing required parameters" in result["error"]
