"""
Integration test for Agent bootstrap functionality.
"""
import pytest

from agent_system.config.models import AgentConfig, MCPConfig
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.bootstrap import bootstrap_servers
from agent_system.servers.agent.server import Agent


class TestAgentBootstrap:
    """Test SubAgent integration with bootstrap system."""
    
    def test_bootstrap_sub_agent(self):
        """Test that agent type can be bootstrapped."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["test_sub"]),
            servers={
                "test_sub": {
                    "type": "agent",
                    "description": "Test agent"
                }
            }
        )
        
        registry = MCPRegistry()
        
        # Bootstrap should create the sub-agent
        bootstrap_servers(config, registry)
        
        # Verify agent was registered
        assert "test_sub" in registry.list()
        server = registry.get("test_sub")
        assert isinstance(server, Agent)
        assert server.name == "test_sub"
        assert server.config.get("description") == "Test agent"
        
    def test_bootstrap_sub_agent_default_description(self):
        """Test agent bootstrap with default description."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["my_sub"]),
            servers={
                "my_sub": {
                    "type": "agent"
                }
            }
        )
        
        registry = MCPRegistry()
        bootstrap_servers(config, registry)
        
        server = registry.get("my_sub")
        assert isinstance(server, Agent)
        assert server.config.get("description") == "Agent: my_sub"
        
    def test_bootstrap_mixed_servers_with_sub_agent(self):
        """Test bootstrap with mix of regular servers and agents."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["datetime", "test_sub", "duckduckgo_search"]),
            servers={
                "test_sub": {
                    "type": "agent",
                    "description": "Test agent"
                },
                "datetime": {
                    "type": "datetime"
                },
                "duckduckgo_search": {
                    "type": "duckduckgo_search"
                }
            }
        )
        
        registry = MCPRegistry()
        bootstrap_servers(config, registry)
        
        # Should have all three servers
        servers = registry.list()
        assert len(servers) == 3
        assert "datetime" in servers
        assert "test_sub" in servers
        assert "duckduckgo_search" in servers
        
        # Agent should be correct type
        agent = registry.get("test_sub")
        assert isinstance(agent, Agent)
