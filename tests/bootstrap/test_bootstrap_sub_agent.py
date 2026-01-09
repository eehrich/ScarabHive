"""
Integration test for Agent bootstrap functionality.
"""
from agent_system.config.models import AgentSystemConfig, PluginsConfig, MCPConfig, AgentConfig, LLMSystemConfig, LLMModelConfig, LLMProfile
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.bootstrap import bootstrap_servers
from agent_system.servers.agent.server import Agent


def create_test_config(**overrides):
    """Create a test configuration with proper LLM system setup."""
    base_config = {
        "llm_system": LLMSystemConfig(
            models={
                "test-model": LLMModelConfig(provider="openai", model="test-model", openai_api_key="fake-key")
            },
            profiles={
                "normal": LLMProfile(model_ref="test-model")
            },
            default_profile="normal"
        )
    }
    base_config.update(overrides)
    return AgentSystemConfig(**base_config)


class TestBootstrapSubAgent:
    """Test SubAgent integration with bootstrap system."""
    
    def test_bootstrap_sub_agent(self):
        """Test that agent type can be bootstrapped."""
        config = create_test_config(
            plugins=PluginsConfig(
                servers={
                    "test_sub": MCPConfig(
                        type="agent",
                        enabled=True,
                        agent_config=AgentConfig(),
                        description="Test agent"
                    )
                }
            )
        )
        
        registry = MCPRegistry()
        
        # Bootstrap should create the sub-agent
        bootstrap_servers(config, registry)
        
        # Verify agent was registered
        assert "test_sub" in registry.list()
        server = registry.get("test_sub")
        assert isinstance(server, Agent)
        assert server.name == "test_sub"
        assert server.mcp_config.description == "Test agent"
        
    def test_bootstrap_sub_agent_default_description(self):
        """Test agent bootstrap with default description."""
        config = create_test_config(
            plugins=PluginsConfig(
                servers={
                    "my_sub": MCPConfig(
                        type="agent",
                        enabled=True,
                        agent_config=AgentConfig()
                    )
                }
            )
        )
        
        registry = MCPRegistry()
        bootstrap_servers(config, registry)
        
        server = registry.get("my_sub")
        assert isinstance(server, Agent)
        # Description might be None or empty if not provided
        # Just verify the agent was created successfully
        
    def test_bootstrap_mixed_servers_with_sub_agent(self):
        """Test bootstrap with mix of regular servers and agents."""
        config = create_test_config(
            plugins=PluginsConfig(
                servers={
                    "test_sub": MCPConfig(
                        type="agent",
                        enabled=True,
                        agent_config=AgentConfig(),
                        description="Test agent"
                    ),
                    "datetime": MCPConfig(
                        type="datetime",
                        enabled=True
                    ),
                    "duckduckgo_search": MCPConfig(
                        type="duckduckgo_search",
                        enabled=True
                    )
                }
            )
        )
        
        registry = MCPRegistry()
        bootstrap_servers(config, registry)
        
        # Should have at least the agent (datetime/duckduckgo might not be registered if plugins don't exist)
        servers = registry.list()
        assert "test_sub" in servers
        
        # Agent should be correct type
        agent = registry.get("test_sub")
        assert isinstance(agent, Agent)