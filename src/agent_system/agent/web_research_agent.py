"""
WebResearchAgent - Specialized agent for web research tasks.
Combines DuckDuckGo search with web scraping capabilities.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from ..config.models import AgentConfig, MCPConfig
from ..mcp.base import MCPRegistry
from .core import Agent
from .sub_agent import SubAgent
from ..servers.bootstrap import bootstrap_servers

logger = logging.getLogger(__name__)


def create_web_research_agent(
    name: str = "web_researcher", 
    config: dict | None = None,
    ssl_verify: bool = True
) -> SubAgent:
    """
    Create a specialized WebResearchAgent.
    
    This agent combines DuckDuckGo search with web scraping to perform
    comprehensive web research tasks.
    
    Args:
        name: Name for the web research agent
        config: Optional configuration dict
        ssl_verify: SSL verification setting
        
    Returns:
        SubAgent configured for web research
    """
    # Create specialized agent configuration
    research_config = AgentConfig(
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
    
    # Create the underlying agent
    research_agent = Agent("web_research_core", research_config, research_registry)
    
    # Create SubAgent wrapper with research-specific configuration
    agent_config = config or {}
    agent_config.setdefault("description", 
        "Specialized web research agent that can search the web and scrape content from websites")
    
    sub_agent = SubAgent(name, research_agent, agent_config, ssl_verify)
    
    logger.info("Created WebResearchAgent '%s' with tools: %s", 
                name, research_registry.list())
    
    return sub_agent


class WebResearchAgent(SubAgent):
    """
    Specialized SubAgent for web research tasks.
    
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
        # Ensure config has the research description if not provided
        config = config or {}
        if "description" not in config:
            config["description"] = "Specialized web research agent that can search the web and scrape content from websites"
            
        # Create the research-capable agent
        sub_agent = create_web_research_agent(name, config, ssl_verify)
        
        # Initialize as SubAgent
        super().__init__(name, sub_agent.agent, config, ssl_verify)
        
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
        
    def get_schema(self) -> dict[str, Any]:
        """
        Return enhanced schema for WebResearchAgent with specialized actions.
        
        Returns:
            OpenAI function schema dict with research-specific actions
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["run", "execute", "ask", "research", "fact_check", "compare_sources"],
                            "description": "Action to perform (run/execute/ask for general tasks, research/fact_check/compare_sources for specialized research)"
                        },
                        "task": {
                            "type": "string", 
                            "description": "The task/query/prompt to execute (for run/execute/ask actions)"
                        },
                        "topic": {
                            "type": "string",
                            "description": "Research topic (for research action)"
                        },
                        "claim": {
                            "type": "string", 
                            "description": "Claim to fact-check (for fact_check action)"
                        },
                        "source_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "URLs to compare (for compare_sources action)"
                        },
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "default": 5,
                            "description": "Maximum number of results to process"
                        }
                    },
                    "required": [],  # No required params, depends on action
                },
            },
        }
        
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """
        Enhanced call method with specialized research actions.
        
        Args:
            tool: The action to execute
            params: Parameters for the action
            
        Returns:
            Action result
        """
        # Handle specialized research actions
        if tool == "research":
            topic = params.get("topic")
            if not topic:
                return {
                    "status": "error",
                    "error": "Missing required parameter 'topic' for research action"
                }
            max_results = params.get("max_results", 5)
            return await self.research(topic, max_results)
            
        elif tool == "fact_check":
            claim = params.get("claim")
            if not claim:
                return {
                    "status": "error", 
                    "error": "Missing required parameter 'claim' for fact_check action"
                }
            return await self.fact_check(claim)
            
        elif tool == "compare_sources":
            topic = params.get("topic")
            source_urls = params.get("source_urls", [])
            if not topic or not source_urls:
                return {
                    "status": "error",
                    "error": "Missing required parameters 'topic' and 'source_urls' for compare_sources action"
                }
            return await self.compare_sources(topic, source_urls)
            
        # Fall back to parent implementation for standard actions
        return await super().call(tool, params)
