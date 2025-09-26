"""Tests for web_research_agent plugin."""

import pytest
from plugins.web_research_agent.plugin import PLUGIN_FACTORY


@pytest.mark.asyncio
async def test_plugin_discovery():
    """Test that the plugin can be discovered and instantiated."""
    # Name is implied by folder. No PLUGIN_NAME constant anymore.

    # Test factory instantiation with proper LLM config
    config = {
        "parent_llm": {
            "llm": {"provider": "openai", "model": "gpt-5-nano"},
            "llm_system": {
                "profiles": {
                    "research": {"model_ref": "gpt-5-nano"},
                    "turbo": {"model_ref": "gpt-5-nano"}
                },
                "models": {
                    "gpt-5-nano": {"provider": "openai", "model": "gpt-5-nano"}
                }
            }
        }
    }
    factory = PLUGIN_FACTORY
    server = factory("test_web_research", config, ssl_verify=False)

    assert server.name == "test_web_research"
    assert server.cfg == config
    assert server.ssl_verify is False

    # Test tools (multi-tool format)
    tools = server.get_tools()
    assert isinstance(tools, list)
    assert len(tools) == 4  # research, fact_check, compare_sources, ask
    
    tool_names = [tool["function"]["name"] for tool in tools]
    assert "research" in tool_names
    assert "fact_check" in tool_names
    assert "compare_sources" in tool_names
    assert "ask" in tool_names

    # Test default action
    default_action = server.get_default_action()
    assert default_action == "research"


@pytest.mark.asyncio
async def test_plugin_call():
    """Test basic plugin call functionality."""
    config = {
        "parent_llm": {
            "llm": {"provider": "openai", "model": "gpt-5-nano"},
            "llm_system": {
                "profiles": {
                    "research": {"model_ref": "gpt-5-nano"},
                    "turbo": {"model_ref": "gpt-5-nano"}
                },
                "models": {
                    "gpt-5-nano": {"provider": "openai", "model": "gpt-5-nano"}
                }
            }
        }
    }
    factory = PLUGIN_FACTORY
    server = factory("test_web_research", config, ssl_verify=False)

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
    assert "Missing required parameter 'topic'" in result["error"]
