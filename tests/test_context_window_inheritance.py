#!/usr/bin/env python3
"""Test script to verify web_research_agent context window inheritance."""

import sys
sys.path.insert(0, 'src')

from plugins.web_research_agent.server import create_web_research_agent, WebResearchAgent


def test_context_window_inheritance():
    """Test that web_research_agent inherits context window from config."""

    # Test 1: Default context window (should be 32768)
    agent1 = create_web_research_agent("test1")
    print(f"Default agent context window: {agent1.agent_config.llm.context_window}")

    # Test 2: Custom context window via config
    config_with_context = {
        "context_window": 128000,
        "model": "gpt-4",
        "provider": "openai"
    }
    agent2 = create_web_research_agent("test2", config_with_context)
    print(f"Agent with custom context window: {agent2.agent_config.llm.context_window}")

    # Test 3: Context window via parent_llm
    config_with_parent = {
        "parent_llm": {
            "context_window": 400000,
            "model": "gpt-4",
            "provider": "openai"
        }
    }
    agent3 = create_web_research_agent("test3", config_with_parent)
    print(f"Agent with parent_llm context window: {agent3.agent_config.llm.context_window}")

    # Test 4: WebResearchAgent class directly
    agent4 = WebResearchAgent("test4", {"context_window": 256000})
    print(f"WebResearchAgent direct with context window: {agent4.agent_config.llm.context_window}")

    # Verify expected values
    assert agent1.agent_config.llm.context_window == 32768, f"Expected 32768, got {agent1.agent_config.llm.context_window}"
    assert agent2.agent_config.llm.context_window == 128000, f"Expected 128000, got {agent2.agent_config.llm.context_window}"
    assert agent3.agent_config.llm.context_window == 400000, f"Expected 400000, got {agent3.agent_config.llm.context_window}"
    assert agent4.agent_config.llm.context_window == 256000, f"Expected 256000, got {agent4.agent_config.llm.context_window}"

    print("✅ All context window inheritance tests passed!")


if __name__ == "__main__":
    test_context_window_inheritance()