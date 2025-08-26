"""
Integration tests for WebResearchAgent bootstrap functionality.
"""
import pytest

from agent_system.config.models import AgentConfig, MCPConfig
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.bootstrap import bootstrap_servers
from agent_system.agent.web_research_agent import WebResearchAgent


class TestWebResearchAgentBootstrap:
    """Test WebResearchAgent integration with bootstrap system."""
    
    def test_bootstrap_web_research_agent(self):
        """Test that web_research_agent type can be bootstrapped."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["researcher"]),
            servers={
                "researcher": {
                    "type": "web_research_agent",
                    "description": "Test web research agent"
                }
            }
        )
        
        registry = MCPRegistry()
        
        # Bootstrap should create the web research agent
        bootstrap_servers(config, registry)
        
        # Verify web research agent was registered
        assert "researcher" in registry.list()
        server = registry.get("researcher")
        assert isinstance(server, WebResearchAgent)
        assert server.name == "researcher"
        assert server.description == "Test web research agent"
        
    def test_bootstrap_web_research_agent_default_description(self):
        """Test web research agent bootstrap with default description."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["web_bot"]),
            servers={
                "web_bot": {
                    "type": "web_research_agent"
                }
            }
        )
        
        registry = MCPRegistry()
        bootstrap_servers(config, registry)
        
        server = registry.get("web_bot")
        assert isinstance(server, WebResearchAgent)
        assert "web research agent" in server.description.lower()
        
    def test_bootstrap_mixed_agents_with_web_research(self):
        """Test bootstrap with mix of different agent types including web research."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["datetime", "researcher", "basic_sub", "duckduckgo_search"]),
            servers={
                "researcher": {
                    "type": "web_research_agent",
                    "description": "Research specialist"
                },
                "basic_sub": {
                    "type": "sub_agent"
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
        
        # Should have all servers
        servers = registry.list()
        assert len(servers) == 4
        assert "datetime" in servers
        assert "researcher" in servers  
        assert "basic_sub" in servers
        assert "duckduckgo_search" in servers
        
        # Web research agent should be correct type
        research_agent = registry.get("researcher")
        assert isinstance(research_agent, WebResearchAgent)
        assert research_agent.description == "Research specialist"
        
    def test_web_research_agent_has_correct_tools(self):
        """Test that bootstrapped WebResearchAgent has research tools."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["research_agent"]),
            servers={
                "research_agent": {
                    "type": "web_research_agent"
                }
            }
        )
        
        registry = MCPRegistry()
        bootstrap_servers(config, registry)
        
        agent = registry.get("research_agent")
        assert isinstance(agent, WebResearchAgent)
        
        # Check that underlying agent has research tools
        tools = agent.agent.registry.list()
        assert "duckduckgo_search" in tools
        assert "web_scraper" in tools
        
    def test_web_research_agent_schema_has_specialized_actions(self):
        """Test that bootstrapped WebResearchAgent has enhanced schema."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["smart_researcher"]),
            servers={
                "smart_researcher": {
                    "type": "web_research_agent"
                }
            }
        )
        
        registry = MCPRegistry()
        bootstrap_servers(config, registry)
        
        agent = registry.get("smart_researcher")
        schema = agent.get_schema()
        
        # Should have specialized research actions
        actions = schema["function"]["parameters"]["properties"]["action"]["enum"]
        assert "research" in actions
        assert "fact_check" in actions
        assert "compare_sources" in actions
