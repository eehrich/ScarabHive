"""Test server-level LLM overrides in bootstrap functionality."""

from agent_system.config.models import AgentConfig, MCPConfig, LLMSystemConfig, LLMModelConfig, LLMProfile
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.bootstrap import bootstrap_servers


def test_server_llm_override():
    """Test that server-level LLM config overrides work correctly."""
    config = AgentConfig(
        llm_system=LLMSystemConfig(
            models={
                "base-model": LLMModelConfig(provider="openai", model="gpt-3.5-turbo", openai_api_key="base-key")
            },
            profiles={
                "normal": LLMProfile(model_ref="base-model")
            },
            default_profile="normal"
        ),
        mcp=MCPConfig(enabled_servers=["override_agent"]),
        servers={
            "override_agent": {
                "type": "agent",
                "default_provider": "ollama",
                "model": "llama3:8b",
                "ollama_url": "http://localhost:11434",
                "description": "Agent with LLM overrides"
            }
        }
    )
    
    registry = MCPRegistry()
    bootstrap_servers(config, registry)
    
    # Verify agent was registered
    agent = registry.get("override_agent")
    assert agent is not None
    
    # Verify the agent has override LLM configuration
    agent_llm_system = agent.agent_config.llm_system
    
    # Should have the override model
    assert "llama3:8b" in agent_llm_system.models
    override_model = agent_llm_system.models["llama3:8b"]
    assert override_model.provider == "ollama"
    assert override_model.model == "llama3:8b"
    assert override_model.ollama_url == "http://localhost:11434"
    
    # Should have created an override profile and set it as default
    assert agent_llm_system.default_profile == "override_agent_override"
    assert "override_agent_override" in agent_llm_system.profiles
    override_profile = agent_llm_system.profiles["override_agent_override"]
    assert override_profile.model_ref == "llama3:8b"


def test_server_no_override():
    """Test that server without overrides inherits base LLM config."""
    config = AgentConfig(
        llm_system=LLMSystemConfig(
            models={
                "base-model": LLMModelConfig(provider="openai", model="gpt-4", openai_api_key="base-key")
            },
            profiles={
                "normal": LLMProfile(model_ref="base-model")
            },
            default_profile="normal"
        ),
        mcp=MCPConfig(enabled_servers=["normal_agent"]),
        servers={
            "normal_agent": {
                "type": "agent",
                "description": "Agent without LLM overrides"
            }
        }
    )
    
    registry = MCPRegistry()
    bootstrap_servers(config, registry)
    
    # Verify agent was registered
    agent = registry.get("normal_agent")
    assert agent is not None
    
    # Verify the agent inherited base LLM configuration
    agent_llm_system = agent.agent_config.llm_system
    
    # Should have the base model
    assert "base-model" in agent_llm_system.models
    base_model = agent_llm_system.models["base-model"]
    assert base_model.provider == "openai"
    assert base_model.model == "gpt-4"
    
    # Should use the normal profile as default
    assert agent_llm_system.default_profile == "normal"


if __name__ == "__main__":
    test_server_llm_override()
    test_server_no_override()
    print("All tests passed!")