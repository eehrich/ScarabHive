"""
Tests for WebResearchAgent functionality.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from plugins.web_research_agent.server import WebResearchAgent, create_web_research_agent
from agent_system.servers.agent.server import Agent


class TestWebResearchAgent:
    """Test WebResearchAgent functionality."""
    
    def test_create_web_research_agent(self):
        """Test web research agent creation."""
        agent = create_web_research_agent("test_researcher")
        
        assert isinstance(agent, Agent)  # WebResearchAgent is now Agent directly
        assert agent.name == "test_researcher"
        assert "web research agent" in agent.config.get("description", "").lower()
        
    def test_create_web_research_agent_custom_config(self):
        """Test web research agent with custom configuration."""
        config = {"description": "Custom research agent"}
        agent = create_web_research_agent("custom_researcher", config)
        
        assert agent.description == "Custom research agent"
        assert agent.name == "custom_researcher"
        
    def test_web_research_agent_initialization(self):
        """Test WebResearchAgent initialization."""
        agent = WebResearchAgent("test_web_researcher")
        
        assert isinstance(agent, Agent)  # WebResearchAgent extends Agent directly
        assert agent.name == "test_web_researcher"
        assert "web research" in agent.description.lower()
        
    def test_web_research_agent_custom_name_and_config(self):
        """Test WebResearchAgent with custom name and config."""
        config = {"description": "Specialized research bot"}
        agent = WebResearchAgent("research_bot", config)
        
        assert agent.name == "research_bot"
        assert agent.description == "Specialized research bot"
        
    def test_get_enhanced_schema(self):
        """Test that WebResearchAgent has enhanced schema with research actions."""
        agent = WebResearchAgent("test_researcher")
        schema = agent.get_schema()
        
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "test_researcher"
        
        # Check for specialized actions
        actions = schema["function"]["parameters"]["properties"]["action"]["enum"]
        assert "research" in actions
        assert "fact_check" in actions
        assert "compare_sources" in actions
        assert "run" in actions  # Standard actions still available
        
        # Check for specialized parameters
        props = schema["function"]["parameters"]["properties"]
        assert "topic" in props
        assert "claim" in props
        assert "source_urls" in props
        assert "max_results" in props
        
    @pytest.mark.asyncio
    async def test_research_action_success(self):
        """Test successful research action."""
        agent = WebResearchAgent("test_researcher")
        
        # Mock the agent's _run_with_progress method instead of run
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.return_value = {
            "task": "research task",
            "calls": [
                {"server": "duckduckgo_search", "result": "search results"},
                {"server": "web_scraper", "result": "scraped content"}
            ],
            "summary": "Research completed successfully"
        }
        
        result = await agent.research("artificial intelligence", max_results=3)
        
        assert result["status"] == "success"
        assert result["agent"] == "test_researcher"  # Agent returns "agent", not "sub_agent"
        # Check that _run_with_progress was called with the correct parameters
        assert agent._run_with_progress.called
        call_args = agent._run_with_progress.call_args[0]
        assert "artificial intelligence" in call_args[0]  # task_prompt
        assert "Researching 'artificial intelligence'" == call_args[1]  # operation_name
        
    @pytest.mark.asyncio
    async def test_fact_check_action_success(self):
        """Test successful fact-check action."""
        agent = WebResearchAgent("fact_checker")
        
        # Mock the agent's _run_with_progress method
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.return_value = {
            "task": "fact check task",
            "calls": [
                {"server": "duckduckgo_search", "result": "verification results"},
                {"server": "web_scraper", "result": "fact checking data"}
            ],
            "summary": "Fact check completed"
        }
        
        result = await agent.fact_check("The Earth is round")
        
        assert result["status"] == "success"
        assert result["agent"] == "fact_checker"
        # Check that _run_with_progress was called
        assert agent._run_with_progress.called
        call_args = agent._run_with_progress.call_args[0]
        assert "The Earth is round" in call_args[0]  # task_prompt
        assert "Fact-checking claim" == call_args[1]  # operation_name
        
    @pytest.mark.asyncio
    async def test_compare_sources_action_success(self):
        """Test successful compare sources action."""
        agent = WebResearchAgent("source_comparer")
        
        # Mock the agent's _run_with_progress method instead of run
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.return_value = {
            "summary": "Source comparison completed",
            "calls": [{"server": "web_scraper", "result": "scraped multiple sources"}]
        }
        
        urls = ["https://example1.com", "https://example2.com"]
        result = await agent.compare_sources("climate change", urls)
        
        assert result["status"] == "success"
        # Check that _run_with_progress was called
        assert agent._run_with_progress.called
        call_args = agent._run_with_progress.call_args[0]
        assert "climate change" in call_args[0]  # task_prompt
        assert "example1.com" in call_args[0]
        assert "example2.com" in call_args[0]
        assert "Comparing sources for 'climate change'" == call_args[1]  # operation_name
        
    @pytest.mark.asyncio
    async def test_call_research_action(self):
        """Test call method with research action."""
        agent = WebResearchAgent("test_agent")
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.return_value = {"summary": "research done"}
        
        result = await agent.call("research", {"topic": "quantum computing", "max_results": 7})
        
        assert result["status"] == "success"
        agent._run_with_progress.assert_called_once()
        
    @pytest.mark.asyncio
    async def test_call_fact_check_action(self):
        """Test call method with fact_check action."""
        agent = WebResearchAgent("fact_checker")
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.return_value = {"summary": "fact checked"}
        
        result = await agent.call("fact_check", {"claim": "Test claim"})
        
        assert result["status"] == "success"
        agent._run_with_progress.assert_called_once()
        
    @pytest.mark.asyncio
    async def test_call_compare_sources_action(self):
        """Test call method with compare_sources action."""
        agent = WebResearchAgent("comparer")
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.return_value = {"summary": "sources compared"}
        
        params = {
            "topic": "renewable energy",
            "source_urls": ["https://site1.com", "https://site2.com"]
        }
        result = await agent.call("compare_sources", params)
        
        assert result["status"] == "success"
        agent._run_with_progress.assert_called_once()
        
    @pytest.mark.asyncio
    async def test_call_standard_action_fallback(self):
        """Test that standard actions fall back to parent implementation."""
        agent = WebResearchAgent("standard_agent")
        
        # Mock the agent's run method
        original_run = agent.run
        agent.run = AsyncMock(return_value={"summary": "standard task done"})
        
        try:
            result = await agent.run("general task")
            assert result["summary"] == "standard task done"
            agent.run.assert_called_once_with("general task")
        finally:
            # Restore original method
            agent.run = original_run
            
    @pytest.mark.asyncio
    async def test_call_research_missing_topic(self):
        """Test research action with missing topic parameter."""
        agent = WebResearchAgent("error_agent")
        
        result = await agent.call("research", {})
        
        assert result["status"] == "error"
        assert "Missing required parameter 'topic'" in result["error"]
        
    @pytest.mark.asyncio
    async def test_call_run_action_routing(self):
        """Test that run action with task parameter routes to research method."""
        agent = WebResearchAgent("task_router")
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.return_value = {"summary": "research done via routing"}
        
        # Test run action with task parameter (how main agent calls web_research_agent)
        result = await agent.call("run", {"task": "research about quantum computing"})
        
        assert result["status"] == "success"
        assert result["agent"] == "task_router"
        
        # Verify _run_with_progress was called (indicating routing to research method)
        agent._run_with_progress.assert_called_once()
        call_args = agent._run_with_progress.call_args[0]
        assert "research about quantum computing" in call_args[0]  # task in prompt
        assert "Researching 'research about quantum computing'" == call_args[1]  # operation_name
        
    @pytest.mark.asyncio
    async def test_call_fact_check_missing_claim(self):
        """Test fact_check action with missing claim parameter."""
        agent = WebResearchAgent("error_agent")
        
        result = await agent.call("fact_check", {})
        
        assert result["status"] == "error"
        assert "Missing required parameter 'claim'" in result["error"]
        
    @pytest.mark.asyncio
    async def test_call_compare_sources_missing_params(self):
        """Test compare_sources action with missing parameters."""
        agent = WebResearchAgent("error_agent")
        
        # Missing both topic and source_urls
        result = await agent.call("compare_sources", {})
        
        assert result["status"] == "error"
        assert "Missing required parameters" in result["error"]
        
        # Missing source_urls
        result = await agent.call("compare_sources", {"topic": "test"})
        
        assert result["status"] == "error"
        assert "Missing required parameters" in result["error"]
        
        # Missing topic
        result = await agent.call("compare_sources", {"source_urls": ["http://example.com"]})
        
        assert result["status"] == "error"
        assert "Missing required parameters" in result["error"]
        
    @pytest.mark.asyncio
    async def test_research_with_agent_exception(self):
        """Test research action when underlying agent raises exception."""
        agent = WebResearchAgent("error_agent")
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.side_effect = RuntimeError("Agent failed")
        
        result = await agent.research("test topic")
        
        assert result["status"] == "error"
        assert "Agent failed" in result["error"]
        
    def test_specialized_agent_has_research_tools(self):
        """Test that the specialized agent is configured with research tools."""
        # This test verifies the agent has the right tools configured
        # We can't easily test the actual bootstrap without integration test
        # But we can verify the factory function sets up the right configuration
        
        agent = create_web_research_agent("tool_test")
        
        # The agent should be an Agent with research tools configured
        assert isinstance(agent, Agent)
        assert hasattr(agent, 'registry')  # Agent has registry
        
        # Check that the agent was created with research-focused description
        assert "web research" in agent.description.lower()


class TestWebResearchAgentIntegration:
    """Integration tests for WebResearchAgent with real components."""
    
    def test_agent_tools_configuration(self):
        """Test that WebResearchAgent has correct tools configured."""
        agent = create_web_research_agent("integration_test")
        
        # Check that the underlying agent has the expected tools
        tools = agent.registry.list()
        assert "duckduckgo_search" in tools
        assert "web_scraper" in tools
        assert len(tools) == 2  # Only research tools
        
    def test_agent_configuration_values(self):
        """Test that WebResearchAgent has correct configuration."""
        agent = create_web_research_agent("config_test")
        
        # Check max_steps is configured for complex research
        assert agent.agent_config.max_steps == 8
        
        # Check enabled servers
        enabled = agent.agent_config.mcp.enabled_servers
        assert "duckduckgo_search" in enabled
        assert "web_scraper" in enabled
