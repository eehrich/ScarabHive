"""Test utilities for AgentSystem tests.

This module provides common utilities and fixtures for testing.
"""

from agent_system.config.models import AgentConfig, LLMSystemConfig, LLMModelConfig, LLMProfile


def create_test_config(**kwargs):
    """Create a test configuration with the new LLM system structure.
    
    Args:
        **kwargs: Additional configuration parameters to override defaults.
        
    Returns:
        AgentConfig: A properly configured test config.
    """
    # Default test configuration
    default_config = {
        "llm_system": LLMSystemConfig(
            models={
                "test-model": LLMModelConfig(provider="openai", model="test-model", openai_api_key="fake-key")
            },
            profiles={
                "normal": LLMProfile(model_ref="test-model")
            },
            default_profile="normal"
        ),
        "max_steps": 10
    }
    
    # Update with any provided overrides
    default_config.update(kwargs)
    
    return AgentConfig(**default_config)


def create_minimal_test_config():
    """Create a minimal test configuration for simple tests."""
    return create_test_config(max_steps=1)


def create_extended_test_config(**kwargs):
    """Create an extended test configuration with more models and profiles."""
    return AgentConfig(
        llm_system=LLMSystemConfig(
            models={
                "gpt-3.5-turbo": LLMModelConfig(provider="openai", model="gpt-3.5-turbo", openai_api_key="fake-key"),
                "gpt-4": LLMModelConfig(provider="openai", model="gpt-4", openai_api_key="fake-key"),
                "test-model": LLMModelConfig(provider="openai", model="test-model", openai_api_key="fake-key")
            },
            profiles={
                "normal": LLMProfile(model_ref="gpt-3.5-turbo"),
                "fast": LLMProfile(model_ref="gpt-3.5-turbo"),
                "smart": LLMProfile(model_ref="gpt-4"),
                "test": LLMProfile(model_ref="test-model")
            },
            default_profile="normal"
        ),
        **kwargs
    )