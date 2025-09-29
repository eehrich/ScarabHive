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
                        "normal": {
                            "model_ref": model,
                            "description": "Normal profile"
                        },
                        "fast": {
                            "model_ref": model,
                            "description": "Fast profile"
                        },
                        "web_research": {
                            "model_ref": model,
                            "description": "Web research profile"
                        }
                    },
                    "default_profile": "normal"
                },
                "agent_llm_profiles": {
                    "test1": "normal",
                    "test2": "normal",
                    "web_research_agent": "normal"
                }
            }
        }

    # Test 1: Default context window (32768)
    config1 = create_llm_config(32768)
    agent1 = create_web_research_agent("test1", config1)
    # Check context window from the model configuration  
    default_profile = agent1.agent_config.llm_system.default_profile
    model_ref = agent1.agent_config.llm_system.profiles[default_profile].model_ref
    context_window1 = agent1.agent_config.llm_system.models[model_ref].context_window
    print(f"Default agent context window: {context_window1}")

    # Test 2: Custom context window (128000)
    config2 = create_llm_config(128000)
    agent2 = create_web_research_agent("test2", config2)
    default_profile2 = agent2.agent_config.llm_system.default_profile
    model_ref2 = agent2.agent_config.llm_system.profiles[default_profile2].model_ref
    context_window2 = agent2.agent_config.llm_system.models[model_ref2].context_window
    print(f"Agent with custom context window: {context_window2}")

    # Test 3: Different context window (400000)
    config3 = create_llm_config(400000)
    agent3 = create_web_research_agent("test3", config3)
    default_profile3 = agent3.agent_config.llm_system.default_profile
    model_ref3 = agent3.agent_config.llm_system.profiles[default_profile3].model_ref
    context_window3 = agent3.agent_config.llm_system.models[model_ref3].context_window
    print(f"Agent with parent_llm context window: {context_window3}")

    # Test 4: WebResearchAgent class directly with different context window
    config4 = create_llm_config(256000)
    agent4 = WebResearchAgent("test4", config4)
    default_profile4 = agent4.agent_config.llm_system.default_profile
    model_ref4 = agent4.agent_config.llm_system.profiles[default_profile4].model_ref
    context_window4 = agent4.agent_config.llm_system.models[model_ref4].context_window
    print(f"WebResearchAgent direct with context window: {context_window4}")

    # Verify expected values
    assert context_window1 == 32768, f"Expected 32768, got {context_window1}"
    assert context_window2 == 128000, f"Expected 128000, got {context_window2}"
    assert context_window3 == 400000, f"Expected 400000, got {context_window3}"
    assert context_window4 == 256000, f"Expected 256000, got {context_window4}"

    print("✅ All context window inheritance tests passed!")


if __name__ == "__main__":
    test_context_window_inheritance()