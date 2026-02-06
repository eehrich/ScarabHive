"""Test server-level LLM overrides in bootstrap functionality."""

from agent_system.config.models import AgentSystemConfig, PluginsConfig, MCPConfig, AgentConfig, LLMSystemConfig, LLMModelConfig, LLMProfile
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.bootstrap import bootstrap_servers


def test_server_llm_override():
    """Test that server-level LLM config overrides work correctly."""
    config = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={
                "base-model": LLMModelConfig(provider="openai", model="gpt-3.5-turbo", openai_api_key="base-key")
            },
            profiles={
                "normal": LLMProfile(model_ref="base-model")
            },
            default_profile="normal"
        ),
        plugins=PluginsConfig(
            servers={
                "override_agent": MCPConfig(
                    type="agent",
                    enabled=True,
                    agent_config=AgentConfig(),
                    default_provider="ollama",
                    model="llama3:8b",
                    ollama_url="http://localhost:11434",
                    description="Agent with LLM overrides"
                )
            }
        )
    )
    
    registry = MCPRegistry()
    bootstrap_servers(config, registry)
    
    # Verify agent was registered
    agent = registry.get("override_agent")
    assert agent is not None
    
    # Verify the agent has access to override config through mcp_config
    assert agent.mcp_config.default_provider == "ollama"
    assert agent.mcp_config.model == "llama3:8b"
    assert agent.mcp_config.ollama_url == "http://localhost:11434"
    
    # Agent should still have access to global llm_system through system_config
    assert "base-model" in agent.system_config.llm_system.models


def test_server_no_override():
    """Test that server without overrides inherits base LLM config."""
    config = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={
                "base-model": LLMModelConfig(provider="openai", model="gpt-4", openai_api_key="base-key")
            },
            profiles={
                "normal": LLMProfile(model_ref="base-model")
            },
            default_profile="normal"
        ),
        plugins=PluginsConfig(
            servers={
                "normal_agent": MCPConfig(
                    type="agent",
                    enabled=True,
                    agent_config=AgentConfig(),
                    description="Agent without LLM overrides"
                )
            }
        )
    )
    
    registry = MCPRegistry()
    bootstrap_servers(config, registry)
    
    # Verify agent was registered
    agent = registry.get("normal_agent")
    assert agent is not None
    
    # Verify the agent inherited base LLM configuration through system_config
    assert "base-model" in agent.system_config.llm_system.models
    base_model = agent.system_config.llm_system.models["base-model"]
    assert base_model.provider == "openai"
    assert base_model.model == "gpt-4"
    
    # Agent should use the normal profile (via agent_config.llm_profile)
    assert agent.agent_config.llm_profile == "normal"


if __name__ == "__main__":
    test_server_llm_override()
    test_server_no_override()
    print("All tests passed!")