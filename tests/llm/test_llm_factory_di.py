"""Factory dependency-injection tests: _build_client, the one construction path.

SCOPE, honestly stated: these run against the conftest fake
(registry.build_client is replaced), so they verify that the factory
resolves profiles and forwards the resolved spec — the returned object is
a _FakeLLMClient carrying provider/model from the spec, not a real client.
Real construction is covered by tests/llm/test_llm_provider_registry.py.
"""
from agent_system.llm.factory import _build_client
from agent_system.config.models import (
    AgentSystemConfig, 
    AgentConfig, 
    LLMSystemConfig, 
    LLMModelConfig, 
    LLMProfile,
    ContextConfig,
    NetworkConfig,
    LoggingConfig
)


def make_config():
    """Create a test AgentSystemConfig with LLM configuration."""
    return AgentSystemConfig(
        name="TestSystem",
        version="0.0.0",
        llm_system=LLMSystemConfig(
            models={
                "gpt-test": LLMModelConfig(
                    provider="openai", 
                    model="gpt-test", 
                    api_key="test-key"
                )
            },
            profiles={
                "normal": LLMProfile(model_ref="gpt-test")
            }
        ),
        context=ContextConfig(auto_datetime=False),
        network=NetworkConfig(),
        logging=LoggingConfig()
    )


def make_agent_config(llm_profile: str = "normal"):
    """Create a test AgentConfig."""
    return AgentConfig(
        llm_profile=llm_profile,
        max_steps=20
    )


def test_llmfactory_creates_llm_with_valid_configs():
    """Test that the factory creates an LLM client with valid configurations."""
    config = make_config()
    agent_config = make_agent_config()
    
    llm = _build_client(config, agent_config, ssl_verify=None)
    
    assert llm is not None
    # The client should have the configured model
    assert hasattr(llm, 'model')


def test_llmfactory_respects_agent_profile():
    """Test that the factory uses the profile specified in agent config."""
    config = make_config()
    
    # Add another profile and model
    config.llm_system.models["gpt-turbo"] = LLMModelConfig(
        provider="openai",
        model="gpt-turbo",
        api_key="test-key"
    )
    config.llm_system.profiles["turbo"] = LLMProfile(model_ref="gpt-turbo")
    
    # Create agent config with turbo profile
    agent_config = make_agent_config(llm_profile="turbo")
    
    llm = _build_client(config, agent_config, ssl_verify=None)
    
    assert llm is not None
    assert llm.model == "gpt-turbo"
