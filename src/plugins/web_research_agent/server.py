"""
WebResearchAgent - Specialized agent for web research tasks.
Combines DuckDuckGo search with web scraping capabilities.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agent_system.config.models import AgentConfig, MCPConfig, LLMConfig
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.agent.server import Agent
from agent_system.servers.bootstrap import bootstrap_servers

logger = logging.getLogger(__name__)


def create_web_research_agent(
    name: str = "web_researcher",
    config: dict | None = None,
    ssl_verify: bool = True
) -> Agent:
    """
    Create a specialized WebResearchAgent.

    This agent combines DuckDuckGo search with web scraping to perform
    comprehensive web research tasks.

    Args:
        name: Name for the web research agent
        config: Optional configuration dict
        ssl_verify: SSL verification setting

    Returns:
        Agent configured for web research
    """
    # Create specialized agent configuration
    # Allow server-provided LLM overrides (e.g., default_provider/model) via `config`
    server_cfg = config or {}
    llm_kwargs: dict = {}
    # accept either 'default_provider' or 'provider' keys from server config
    if server_cfg.get("default_provider"):
        llm_kwargs["provider"] = server_cfg.get("default_provider")
    elif server_cfg.get("provider"):
        llm_kwargs["provider"] = server_cfg.get("provider")
    if server_cfg.get("model"):
        llm_kwargs["model"] = server_cfg.get("model")
    if server_cfg.get("openai_api_key"):
        llm_kwargs["openai_api_key"] = server_cfg.get("openai_api_key")
    if server_cfg.get("ollama_url"):
        llm_kwargs["ollama_url"] = server_cfg.get("ollama_url")
    if server_cfg.get("ollama_mode"):
        llm_kwargs["ollama_mode"] = server_cfg.get("ollama_mode")
    if server_cfg.get("request_timeout") is not None:
        llm_kwargs["request_timeout"] = server_cfg.get("request_timeout")

    research_llm = LLMConfig(**llm_kwargs) if llm_kwargs else LLMConfig()

    research_config = AgentConfig(
        llm=research_llm,
        mcp=MCPConfig(enabled_servers=["duckduckgo_search", "web_scraper"]),
        servers={
            "duckduckgo_search": {
                "type": "duckduckgo_search"
            },
            "web_scraper": {
                "type": "web_scraper"
            }
        },
        max_steps=8,  # More steps for complex research tasks
        network={"ssl_verify": ssl_verify}
    )

    # Create registry and bootstrap the research tools
    research_registry = MCPRegistry()
    bootstrap_servers(research_config, research_registry)

    # Create the agent directly with research-specific configuration
    agent_config = config or {}
    agent_config.setdefault("description",
        "Specialized web research agent that can search the web and scrape content from websites")

    research_agent = Agent(name, research_config, research_registry, agent_config, ssl_verify)

    logger.info("Created WebResearchAgent '%s' with tools: %s",
                name, research_registry.list())

    return research_agent


class WebResearchAgent(Agent):
    """
    Specialized Agent for web research tasks.

    This agent is pre-configured with:
    - DuckDuckGo search capabilities
    - Web scraping functionality
    - Optimized prompts for research tasks
    """

    def __init__(self, name: str = "web_researcher", config: dict | None = None, ssl_verify: bool = True):
        """
        Initialize WebResearchAgent.

        Args:
            name: Name for the agent
            config: Optional configuration dict
            ssl_verify: SSL verification setting
        """
        # Create specialized agent configuration for research
        server_cfg = config or {}
        llm_kwargs: dict = {}
        if server_cfg.get("default_provider"):
            llm_kwargs["provider"] = server_cfg.get("default_provider")
        elif server_cfg.get("provider"):
            llm_kwargs["provider"] = server_cfg.get("provider")
        if server_cfg.get("model"):
            llm_kwargs["model"] = server_cfg.get("model")
        if server_cfg.get("openai_api_key"):
            llm_kwargs["openai_api_key"] = server_cfg.get("openai_api_key")
        if server_cfg.get("ollama_url"):
            llm_kwargs["ollama_url"] = server_cfg.get("ollama_url")
        if server_cfg.get("ollama_mode"):
            llm_kwargs["ollama_mode"] = server_cfg.get("ollama_mode")
        if server_cfg.get("request_timeout") is not None:
            llm_kwargs["request_timeout"] = server_cfg.get("request_timeout")

        research_llm = LLMConfig(**llm_kwargs) if llm_kwargs else LLMConfig()

        research_config = AgentConfig(
            llm=research_llm,
            mcp=MCPConfig(enabled_servers=["duckduckgo_search", "web_scraper"]),
            servers={
                "duckduckgo_search": {"type": "duckduckgo_search"},
                "web_scraper": {"type": "web_scraper"}
            },
            max_steps=8,  # More steps for complex research tasks
            network={"ssl_verify": ssl_verify}
        )

        # Create registry and bootstrap the research tools
        research_registry = MCPRegistry()
        bootstrap_servers(research_config, research_registry)

        # Set default description for research agent
        agent_config = config or {}
        agent_config.setdefault("description",
            "Specialized web research agent that can search the web and scrape content from websites")

        # Initialize as Agent directly - this is the radical change!
        super().__init__(name, research_config, research_registry, agent_config, ssl_verify)

        logger.info("Created WebResearchAgent '%s' with tools: %s",
                    name, research_registry.list())

    async def research(self, topic: str, max_results: int = 5) -> Dict[str, Any]:
        """
        Perform comprehensive research on a topic.

        Args:
            topic: Research topic/query
            max_results: Maximum number of search results to process

        Returns:
            Research results with sources and content
        """
        research_prompt = f"""
        Perform comprehensive research on: {topic}

        Instructions:
        1. First, search for recent information about "{topic}" using DuckDuckGo
        2. From the search results, identify the {max_results} most relevant sources
        3. Scrape content from those sources to get detailed information
        4. Synthesize the findings into a comprehensive research summary

        Please provide:
        - Key findings and insights
        - Important facts and data points
        - Different perspectives or viewpoints
        - Source URLs for verification
        """

        return await self.call("run", {"task": research_prompt})

    async def fact_check(self, claim: str) -> Dict[str, Any]:
        """
        Fact-check a specific claim by researching multiple sources.

        Args:
            claim: Claim to fact-check

        Returns:
            Fact-check results with evidence
        """
        fact_check_prompt = f"""
        Fact-check this claim: "{claim}"

        Instructions:
        1. Search for information about this specific claim
        2. Look for authoritative sources (news, academic, official sites)
        3. Scrape content from credible sources
        4. Analyze the evidence for and against the claim

        Please provide:
        - Verification status (True/False/Partially True/Unverified)
        - Supporting evidence with sources
        - Contradicting evidence if any
        - Context and nuances
        """

        return await self.call("run", {"task": fact_check_prompt})

    async def compare_sources(self, topic: str, source_urls: list[str]) -> Dict[str, Any]:
        """
        Compare information from specific sources on a topic.

        Args:
            topic: Topic to compare across sources
            source_urls: List of URLs to scrape and compare

        Returns:
            Comparison analysis
        """
        sources_text = "\n".join([f"- {url}" for url in source_urls])

        compare_prompt = f"""
        Compare how different sources cover this topic: {topic}

        Scrape content from these specific sources:
        {sources_text}

        Instructions:
        1. Scrape content from each provided URL
        2. Extract information relevant to "{topic}" from each source
        3. Compare and contrast the different perspectives/information
        4. Identify agreements, disagreements, and unique insights

        Please provide:
        - Summary from each source
        - Key similarities and differences
        - Bias or perspective analysis
        - Most comprehensive/reliable source assessment
        """

        return await self.call("run", {"task": compare_prompt})

    def get_schema(self) -> Dict[str, Any]:
        """
        Override to provide enhanced schema with research-specific actions.

        Returns:
            Enhanced OpenAI function schema for web research
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Advanced web research agent for comprehensive topic research, fact-checking, and source comparison. Use for: detailed research on specific topics, fact-checking claims, comparing multiple sources, analyzing contradictory information, or when standard search is insufficient and you need thorough analysis.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["run", "execute", "ask", "research", "fact_check", "compare_sources"],
                            "description": "Action to perform: 'research' for comprehensive topic research, 'fact_check' for verifying claims, 'compare_sources' for analyzing multiple sources, 'run/execute/ask' for general queries"
                        },
                        "task": {
                            "type": "string",
                            "description": "General task/query for run/execute/ask actions"
                        },
                        "topic": {
                            "type": "string",
                            "description": "Research topic for comprehensive research action (use with action='research')"
                        },
                        "claim": {
                            "type": "string",
                            "description": "Claim to fact-check for fact_check action (use with action='fact_check')"
                        },
                        "source_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "URLs to compare for compare_sources action (use with action='compare_sources')"
                        },
                        "max_results": {
                            "type": "integer",
                            "default": 5,
                            "description": "Maximum results for research action (1-10)"
                        }
                    },
                    "required": ["action"],
                },
            },
        }

    async def call(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Override call to handle research-specific actions.

        Args:
            action: Action to perform
            params: Action parameters

        Returns:
            Action result
        """
        if action == "research":
            topic = params.get("topic")
            if not topic:
                return {
                    "status": "error",
                    "error": "Missing required parameter 'topic' for research action"
                }
            max_results = params.get("max_results", 5)
            return await self.research(topic, max_results)

        elif action == "fact_check":
            claim = params.get("claim")
            if not claim:
                return {
                    "status": "error",
                    "error": "Missing required parameter 'claim' for fact_check action"
                }
            return await self.fact_check(claim)

        elif action == "compare_sources":
            source_urls = params.get("source_urls")
            topic = params.get("topic")
            if not topic or not source_urls:
                return {
                    "status": "error",
                    "error": "Missing required parameters 'topic' and 'source_urls' for compare_sources action"
                }
            return await self.compare_sources(topic, source_urls)

        else:
            # Fall back to parent implementation for standard actions
            return await super().call(action, params)

    def get_default_action(self) -> str:
        return "research"
