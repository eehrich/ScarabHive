"""LLMFactory dependency-injection tests.

SCOPE, honestly stated: these run against the conftest fake
(registry.build_client is replaced), so they verify that the factory
resolves profiles and forwards the resolved spec — the returned object is
a _FakeLLMClient carrying provider/model from the spec, not a real client.
Real construction is covered by tests/llm/test_llm_provider_registry.py.
"""
from agent_system.llm.factory import LLMFactory
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


def test_llmfactory_returns_none_if_no_config():
    """Test that LLMFactory returns None when no config is provided."""
    f = LLMFactory(None, None)
    assert f.create() is None


def test_llmfactory_returns_none_if_no_agent_config():
    """Test that LLMFactory returns None when only system config is provided."""
    config = make_config()
    f = LLMFactory(config, None)
    assert f.create() is None


def test_llmfactory_creates_llm_with_valid_configs():
    """Test that LLMFactory creates an LLM client with valid configurations."""
    config = make_config()
    agent_config = make_agent_config()
    
    f = LLMFactory(config, agent_config)
    llm = f.create()
    
    assert llm is not None
    # The client should have the configured model
    assert hasattr(llm, 'model')


def test_llmfactory_respects_agent_profile():
    """Test that LLMFactory uses the profile specified in agent config."""
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
    
    f = LLMFactory(config, agent_config)
    llm = f.create()
    
    assert llm is not None
    assert llm.model == "gpt-turbo"
