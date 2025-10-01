"""Tests for web_research_agent custom prompt loading."""

import pytest
from pathlib import Path

from plugins.web_research_agent.plugin import PLUGIN_FACTORY


@pytest.mark.asyncio
async def test_web_research_agent_uses_plugin_prompt(mock_system_config, mock_mcp_config):
    """Test that the agent loads its plugin-specific prompt."""
    # Create the agent using the modern factory
    factory = PLUGIN_FACTORY
    agent = factory("web_test", mock_system_config, mock_mcp_config)

    # Get custom system prompt (should load from plugin's prompts/system_prompt.yaml)
    custom_prompt = agent.get_custom_system_prompt({})
    
    assert custom_prompt is not None, "Plugin prompt not loaded"
    assert isinstance(custom_prompt, str), "Plugin prompt should be a string"
    
    # Ensure it contains an indicative phrase from the plugin prompt
    assert 'focused web research agent' in custom_prompt.lower()
    assert 'cite sources' in custom_prompt.lower()


def test_global_prompt_unchanged():
    """Ensure the global prompt file does not contain plugin-specific wording."""
    p = Path('config/prompts/system_prompt.yaml')
    assert p.exists()
    txt = p.read_text(encoding='utf-8')
    # Global prompt should not contain web research specific instructions
    assert 'focused web research agent' not in txt.lower()
    assert 'cite sources' not in txt.lower()
