#!/usr/bin/env python3
"""Test script to verify web_research_agent context window inheritance."""

import sys
sys.path.insert(0, 'src')

from plugins.web_research_agent.server import create_web_research_agent, WebResearchAgent


def test_context_window_inheritance():
    """Test that web_research_agent inherits context window from config."""

    # Helper function to create proper LLM config structure
    def create_llm_config(context_window, model="gpt-4o-mini", provider="openai"):
        return {
            "parent_llm": {
                "llm": {
                    "provider": provider,
                    "model": model,
                    "context_window": context_window
                },
                "llm_system": {
                    "default_provider": provider,
                    "default_model": model,
                    "models": {
                        model: {
                            "provider": provider,
                            "model": model,
                            "context_window": context_window
                        }
                    },
                    "profiles": {
                        "web_research": {
                            "model_ref": model,
                            "description": "Web research profile"
                        }
                    }
                },
                "agent_llm_profiles": {
                    "web_research_agent": "web_research"
                }
            }
        }

    # Test 1: Default context window (32768)
    config1 = create_llm_config(32768)
    agent1 = create_web_research_agent("test1", config1)
    print(f"Default agent context window: {agent1.agent_config.llm.context_window}")

    # Test 2: Custom context window (128000)
    config2 = create_llm_config(128000)
    agent2 = create_web_research_agent("test2", config2)
    print(f"Agent with custom context window: {agent2.agent_config.llm.context_window}")

    # Test 3: Different context window (400000)
    config3 = create_llm_config(400000)
    agent3 = create_web_research_agent("test3", config3)
    print(f"Agent with parent_llm context window: {agent3.agent_config.llm.context_window}")

    # Test 4: WebResearchAgent class directly with different context window
    config4 = create_llm_config(256000)
    agent4 = WebResearchAgent("test4", config4)
    print(f"WebResearchAgent direct with context window: {agent4.agent_config.llm.context_window}")

    # Verify expected values
    assert agent1.agent_config.llm.context_window == 32768, f"Expected 32768, got {agent1.agent_config.llm.context_window}"
    assert agent2.agent_config.llm.context_window == 128000, f"Expected 128000, got {agent2.agent_config.llm.context_window}"
    assert agent3.agent_config.llm.context_window == 400000, f"Expected 400000, got {agent3.agent_config.llm.context_window}"
    assert agent4.agent_config.llm.context_window == 256000, f"Expected 256000, got {agent4.agent_config.llm.context_window}"

    print("✅ All context window inheritance tests passed!")


if __name__ == "__main__":
    test_context_window_inheritance()