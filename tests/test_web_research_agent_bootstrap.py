"""
Integration tests for WebResearchAgent bootstrap functionality.
"""
import pytest

from agent_system.config.models import AgentConfig, MCPConfig, LLMSystemConfig, LLMModelConfig, LLMProfile
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.bootstrap import bootstrap_servers
from plugins.web_research_agent.plugin import PLUGIN_FACTORY  # wrapper removed; using direct server


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
            },
            network={"ssl_verify": True},
            llm_system=LLMSystemConfig(
                models={"gpt-4o-mini": LLMModelConfig(provider="openai", model="gpt-4o-mini")},
                profiles={"web_research": LLMProfile(model_ref="gpt-4o-mini", description="Web research profile")},
                default_profile="web_research"
            ),
            agent_llm_profiles={"web_research_agent": "web_research"}
        )

        registry = MCPRegistry()
        bootstrap_servers(config, registry)

        assert "researcher" in registry.list()
        server = registry.get("researcher")
        assert server.__class__.__name__ == "WebResearchAgent"
        assert server.name == "researcher"

    def test_bootstrap_web_research_agent_default_description(self):
        """Test web research agent bootstrap with default description."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["web_bot"]),
            servers={
                "web_bot": {"type": "web_research_agent"}
            },
            network={"ssl_verify": True},
            llm_system=LLMSystemConfig(
                models={"gpt-4o-mini": LLMModelConfig(provider="openai", model="gpt-4o-mini")},
                profiles={"web_research": LLMProfile(model_ref="gpt-4o-mini", description="Web research profile")},
                default_profile="web_research"
            ),
            agent_llm_profiles={"web_research_agent": "web_research"}
        )

        registry = MCPRegistry()
        bootstrap_servers(config, registry)

        server = registry.get("web_bot")
        assert server.__class__.__name__ == "WebResearchAgent"
        assert "web research agent" in server.description.lower()

    def test_bootstrap_mixed_agents_with_web_research(self):
        """Test bootstrap with mix of different agent types including web research."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["datetime", "researcher", "basic_sub", "duckduckgo_search"]),
            servers={
                "researcher": {"type": "web_research_agent", "description": "Research specialist"},
                "basic_sub": {"type": "sub_agent"},  # ignored (no plugin)
                "datetime": {"type": "datetime"},
                "duckduckgo_search": {"type": "duckduckgo_search"}
            },
            network={"ssl_verify": True},
            llm_system=LLMSystemConfig(
                models={"gpt-4o-mini": LLMModelConfig(provider="openai", model="gpt-4o-mini")},
                profiles={"web_research": LLMProfile(model_ref="gpt-4o-mini", description="Web research profile")},
                default_profile="web_research"
            ),
            agent_llm_profiles={"web_research_agent": "web_research"}
        )

        registry = MCPRegistry()
        bootstrap_servers(config, registry)

        servers = registry.list()
        assert len(servers) == 3  # datetime, researcher, duckduckgo_search
        assert {"datetime", "researcher", "duckduckgo_search"}.issubset(set(servers))

        research_agent = registry.get("researcher")
        assert research_agent.__class__.__name__ == "WebResearchAgent"
        assert research_agent.description == "Research specialist"

    def test_web_research_agent_has_correct_tools(self):
        """Test that bootstrapped WebResearchAgent has research tools."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["research_agent"]),
            servers={
                "research_agent": {"type": "web_research_agent"}
            },
            network={"ssl_verify": True},
            llm_system=LLMSystemConfig(
                models={"gpt-4o-mini": LLMModelConfig(provider="openai", model="gpt-4o-mini")},
                profiles={"web_research": LLMProfile(model_ref="gpt-4o-mini", description="Web research profile")},
                default_profile="web_research"
            ),
            agent_llm_profiles={"web_research_agent": "web_research"}
        )

        registry = MCPRegistry()
        bootstrap_servers(config, registry)

        agent = registry.get("research_agent")
        assert agent.__class__.__name__ == "WebResearchAgent"

        tools = agent.get_tools()
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "web_research_agent" in tool_names
        assert "fact_check_agent" in tool_names

    def test_web_research_agent_schema_has_specialized_actions(self):
        """Test that bootstrapped WebResearchAgent has enhanced schema."""
        config = AgentConfig(
            mcp=MCPConfig(enabled_servers=["smart_researcher"]),
            servers={
                "smart_researcher": {"type": "web_research_agent"}
            },
            network={"ssl_verify": True},
            llm_system=LLMSystemConfig(
                models={"gpt-4o-mini": LLMModelConfig(provider="openai", model="gpt-4o-mini")},
                profiles={"web_research": LLMProfile(model_ref="gpt-4o-mini", description="Web research profile")},
                default_profile="web_research"
            ),
            agent_llm_profiles={"web_research_agent": "web_research"}
        )

        registry = MCPRegistry()
        bootstrap_servers(config, registry)

        agent = registry.get("smart_researcher")
        tools = agent.get_tools()
        tool_names = [tool["function"]["name"] for tool in tools]
        assert {"web_research_agent", "fact_check_agent", "source_analysis_agent"}.issubset(set(tool_names))
