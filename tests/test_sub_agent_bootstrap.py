"""
Integration test for SubAgent bootstrap functionality.
"""
import pytest

from agent_system.config.models import AgentConfig, MCPConfig
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.bootstrap import bootstrap_servers
from agent_system.agent.sub_agent import SubAgent


class TestSubAgentBootstrap:
    """Test SubAgent integration with bootstrap system."""
    
    def test_bootstrap_sub_agent(self):
        """Test that sub_agent type can be bootstrapped."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["test_sub"]),
            servers={
                "test_sub": {
                    "type": "sub_agent",
                    "description": "Test sub-agent"
                }
            }
        )
        
        registry = MCPRegistry()
        
        # Bootstrap should create the sub-agent
        bootstrap_servers(config, registry)
        
        # Verify sub-agent was registered
        assert "test_sub" in registry.list()
        server = registry.get("test_sub")
        assert isinstance(server, SubAgent)
        assert server.name == "test_sub"
        assert server.description == "Test sub-agent"
        
    def test_bootstrap_sub_agent_default_description(self):
        """Test sub-agent bootstrap with default description."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["my_sub"]),
            servers={
                "my_sub": {
                    "type": "sub_agent"
                }
            }
        )
        
        registry = MCPRegistry()
        bootstrap_servers(config, registry)
        
        server = registry.get("my_sub")
        assert isinstance(server, SubAgent)
        assert server.description == "Sub-agent: my_sub"
        
    def test_bootstrap_mixed_servers_with_sub_agent(self):
        """Test bootstrap with mix of regular servers and sub-agents."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["datetime", "test_sub", "duckduckgo_search"]),
            servers={
                "test_sub": {
                    "type": "sub_agent",
                    "description": "Test sub-agent"
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
        
        # Sub-agent should be correct type
        sub_agent = registry.get("test_sub")
        assert isinstance(sub_agent, SubAgent)
