"""
Tests for WebResearchAgent functionality.
"""
import pytest
from unittest.mock import AsyncMock

from plugins.web_research_agent.server import WebResearchAgent, create_web_research_agent
from agent_system.servers.agent.server import Agent


@pytest.fixture
def test_llm_config():
    """Fixture providing LLM configuration for web research agent tests."""
    return {
        "parent_llm": {
            "llm": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "context_window": 4000
            },
            "llm_system": {
                "default_provider": "openai",
                "default_model": "gpt-4o-mini",
                "models": {
                    "gpt-4o-mini": {
                        "provider": "openai",
                        "model": "gpt-4o-mini",
                        "context_window": 4000
                    }
                },
                "profiles": {
                    "web_research": {
                        "model_ref": "gpt-4o-mini",
                        "description": "Web research profile"
                    }
                }
            },
            "agent_llm_profiles": {
                "web_research_agent": "web_research"
            }
        }
    }


class TestWebResearchAgent:
    """Test WebResearchAgent functionality."""
    
    def test_create_web_research_agent(self, test_llm_config):
        """Test web research agent creation."""
        agent = create_web_research_agent("test_researcher", test_llm_config)
        
        assert isinstance(agent, Agent)  # WebResearchAgent is now Agent directly
        assert agent.name == "test_researcher"
        assert "web research agent" in agent.config.get("description", "").lower()
        
    def test_create_web_research_agent_custom_config(self, test_llm_config):
        """Test web research agent with custom configuration."""
        config = {**test_llm_config, "description": "Custom research agent"}
        agent = create_web_research_agent("custom_researcher", config)
        
        assert agent.description == "Custom research agent"
        assert agent.name == "custom_researcher"
        
    def test_web_research_agent_initialization(self, test_llm_config):
        """Test WebResearchAgent initialization."""
        agent = WebResearchAgent("test_web_researcher", test_llm_config)
        
        assert isinstance(agent, Agent)  # WebResearchAgent extends Agent directly
        assert agent.name == "test_web_researcher"
        assert "web research" in agent.description.lower()
        
    def test_web_research_agent_custom_name_and_config(self, test_llm_config):
        """Test WebResearchAgent with custom name and config."""
        config = {**test_llm_config, "description": "Specialized research bot"}
        agent = WebResearchAgent("research_bot", config)
        
        assert agent.name == "research_bot"
        assert agent.description == "Specialized research bot"
        
    def test_get_enhanced_schema(self, test_llm_config):
        """Test that WebResearchAgent has MCP tools for research actions."""
        agent = WebResearchAgent("test_researcher", test_llm_config)
        tools = agent.get_tools()
        
        assert isinstance(tools, list)
        assert len(tools) == 4  # Four specialized research tools
        
        # Check for specialized tools
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "web_research_agent" in tool_names
        assert "fact_check_agent" in tool_names
        assert "source_analysis_agent" in tool_names  
        assert "research_assistant_agent" in tool_names
        
        # Check that web_research_agent tool has expected parameters
        web_tool = next(t for t in tools if t["function"]["name"] == "web_research_agent")
        props = web_tool["function"]["parameters"]["properties"]
        assert "topic" in props
        assert "max_results" in props
        
        # Check that fact_check_agent tool has expected parameters
        fact_tool = next(t for t in tools if t["function"]["name"] == "fact_check_agent")
        props = fact_tool["function"]["parameters"]["properties"]
        assert "claim" in props
        
    @pytest.mark.asyncio
    async def test_research_action_success(self, test_llm_config):
        """Test successful research action."""
        agent = WebResearchAgent("test_researcher", test_llm_config)
        
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
    async def test_fact_check_action_success(self, test_llm_config):
        """Test successful fact-check action."""
        agent = WebResearchAgent("fact_checker", test_llm_config)
        
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
    async def test_compare_sources_action_success(self, test_llm_config):
        """Test successful compare sources action."""
        agent = WebResearchAgent("source_comparer", test_llm_config)
        
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
    async def test_call_research_action(self, test_llm_config):
        """Test call method with research action."""
        agent = WebResearchAgent("test_agent", test_llm_config)
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.return_value = {"summary": "research done"}
        
        result = await agent.call("web_research_agent", {"topic": "quantum computing", "max_results": 7})
        
        assert result["status"] == "success"
        agent._run_with_progress.assert_called_once()
        
    @pytest.mark.asyncio
    async def test_call_fact_check_action(self, test_llm_config):
        """Test call method with fact_check action."""
        agent = WebResearchAgent("fact_checker", test_llm_config)
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.return_value = {"summary": "fact checked"}
        
        result = await agent.call("fact_check_agent", {"claim": "Test claim"})
        
        assert result["status"] == "success"
        agent._run_with_progress.assert_called_once()
        
    @pytest.mark.asyncio
    async def test_call_compare_sources_action(self, test_llm_config):
        """Test call method with compare_sources action."""
        agent = WebResearchAgent("comparer", test_llm_config)
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.return_value = {"summary": "sources compared"}
        
        params = {
            "topic": "renewable energy",
            "source_urls": ["https://site1.com", "https://site2.com"]
        }
        result = await agent.call("source_analysis_agent", params)
        
        assert result["status"] == "success"
        agent._run_with_progress.assert_called_once()
        
    @pytest.mark.asyncio
    async def test_call_standard_action_fallback(self, test_llm_config):
        """Test that standard actions fall back to parent implementation."""
        agent = WebResearchAgent("standard_agent", test_llm_config)
        
        # Mock the agent's run_events method instead
        original_run_events = agent.run_events
        
        async def mock_run_events(task, request_id=None, session_id=None):
            yield {"type": "final", "summary": "standard task done"}
            yield {"type": "end"}
        
        agent.run_events = mock_run_events
        
        try:
            from agent_system.servers.agent.utils import collect_final_result
            result = await collect_final_result(agent, "general task")
            assert result["summary"] == "standard task done"
        finally:
            # Restore original method
            agent.run_events = original_run_events
            
    @pytest.mark.asyncio
    async def test_call_research_missing_topic(self, test_llm_config):
        """Test research action with missing topic parameter."""
        agent = WebResearchAgent("error_agent", test_llm_config)
        
        result = await agent.call("web_research_agent", {})
        
        assert result["status"] == "error"
        assert "Missing required parameter 'topic'" in result["error"]
        
    @pytest.mark.asyncio
    async def test_call_run_action_routing(self, test_llm_config):
        """Test that run action with task parameter routes to research method."""
        agent = WebResearchAgent("task_router", test_llm_config)
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.return_value = {"summary": "research done via routing"}
        
        # Test run action with task parameter (how main agent calls web_research_agent)
        result = await agent.call("research_assistant_agent", {"task": "research about quantum computing"})
        
        assert result["status"] == "success"
        assert result["agent"] == "task_router"
        
        # Verify _run_with_progress was called (indicating routing to research method)
        agent._run_with_progress.assert_called_once()
        call_args = agent._run_with_progress.call_args[0]
        assert "research about quantum computing" in call_args[0]  # task in prompt
        assert "Researching 'research about quantum computing'" == call_args[1]  # operation_name
        
    @pytest.mark.asyncio
    async def test_call_fact_check_missing_claim(self, test_llm_config):
        """Test fact_check action with missing claim parameter."""
        agent = WebResearchAgent("error_agent", test_llm_config)
        
        result = await agent.call("fact_check_agent", {})
        
        assert result["status"] == "error"
        assert "Missing required parameter 'claim'" in result["error"]
        
    @pytest.mark.asyncio
    async def test_call_compare_sources_missing_params(self, test_llm_config):
        """Test compare_sources action with missing parameters."""
        agent = WebResearchAgent("error_agent", test_llm_config)
        
        # Missing both topic and source_urls
        result = await agent.call("source_analysis_agent", {})
        
        assert result["status"] == "error"
        assert "Missing required parameters" in result["error"]
        
        # Missing source_urls
        result = await agent.call("source_analysis_agent", {"topic": "test"})
        
        assert result["status"] == "error"
        assert "Missing required parameters" in result["error"]
        
        # Missing topic
        result = await agent.call("source_analysis_agent", {"source_urls": ["http://example.com"]})
        
        assert result["status"] == "error"
        assert "Missing required parameters" in result["error"]
        
    @pytest.mark.asyncio
    async def test_research_with_agent_exception(self, test_llm_config):
        """Test research action when underlying agent raises exception."""
        agent = WebResearchAgent("error_agent", test_llm_config)
        agent._run_with_progress = AsyncMock()
        agent._run_with_progress.side_effect = RuntimeError("Agent failed")
        
        result = await agent.research("test topic")
        
        assert result["status"] == "error"
        assert "Agent failed" in result["error"]
        
    def test_specialized_agent_has_research_tools(self, test_llm_config):
        """Test that the specialized agent is configured with research tools."""
        # This test verifies the agent has the right tools configured
        # We can't easily test the actual bootstrap without integration test
        # But we can verify the factory function sets up the right configuration
        
        agent = create_web_research_agent("tool_test", test_llm_config)
        
        # The agent should be an Agent with research tools configured
        assert isinstance(agent, Agent)
        assert hasattr(agent, 'registry')  # Agent has registry
        
        # Check that the agent was created with research-focused description
        assert "web research" in agent.description.lower()


class TestWebResearchAgentIntegration:
    """Integration tests for WebResearchAgent with real components."""
    
    def test_agent_tools_configuration(self, test_llm_config):
        """Test that WebResearchAgent has correct tools configured."""
        agent = create_web_research_agent("integration_test", test_llm_config)
        
        # Check that the underlying agent has the expected tools
        tools = agent.registry.list()
        assert "duckduckgo_search" in tools
        assert "web_scraper" in tools
        assert len(tools) == 2  # Only research tools
        
    def test_agent_configuration_values(self, test_llm_config):
        """Test that WebResearchAgent has correct configuration."""
        agent = create_web_research_agent("config_test", test_llm_config)
        
        # Check max_steps is configured for complex research
        assert agent.agent_config.max_steps == 8
        
        # Check enabled servers
        enabled = agent.agent_config.mcp.enabled_servers
        assert "duckduckgo_search" in enabled
        assert "web_scraper" in enabled
