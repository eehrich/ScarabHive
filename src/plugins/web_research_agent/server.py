"""
WebResearchAgent - Specialized agent for web research tasks.
Combines DuckDuckGo search with web scraping capabilities.
"""
from __future__ import annotations

import logging
from typing import Any, Dict
from pathlib import Path

from agent_system.config.models import AgentConfig, MCPConfig, LLMConfig
from agent_system.mcp.base import MCPRegistry
from agent_system.servers.agent.server import Agent
from agent_system.servers.bootstrap import bootstrap_servers
from agent_system.mcp.status import (
    publish_status,
    PHASE_START,
    PHASE_PROGRESS,
    PHASE_END,
    PHASE_ERROR,
)

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
    # Backwards compatibility: some tests expect a plain dict of original config
    # accessible as .cfg (mirroring legacy plugin servers).
    try:  # pragma: no cover - defensive
        setattr(research_agent, "cfg", server_cfg)
    except Exception:
        pass

    logger.info("Created WebResearchAgent '%s' with tools: %s",
                name, research_registry.list())

    return research_agent


class WebResearchAgent(Agent):
    """Specialized Agent for web research tasks (search + scraping)."""

    def __init__(self, name: str = "web_research_agent", config: dict | None = None, ssl_verify: bool = True):
        server_cfg = config or {}

        llm_kwargs: dict[str, Any] = {}
        provider = server_cfg.get("default_provider") or server_cfg.get("provider")
        if provider:
            llm_kwargs["provider"] = provider
        for key in ("model", "openai_api_key", "ollama_url", "ollama_mode", "request_timeout"):
            if server_cfg.get(key) is not None:
                llm_kwargs[key] = server_cfg.get(key)

        # Inherit missing LLM fields from a provided parent/global config dict (if caller
        # passed a reference containing top-level llm info under 'parent_llm'). This avoids
        # forcing duplication of openai_api_key or model in server-specific config.
        parent_llm = server_cfg.get("parent_llm") if isinstance(server_cfg, dict) else None
        if isinstance(parent_llm, dict):  # expected shape: {'provider':..., 'model':..., 'openai_api_key':...}
            for field in ("provider", "model", "openai_api_key", "ollama_url", "ollama_mode", "request_timeout"):
                if field not in llm_kwargs and parent_llm.get(field) is not None:
                    llm_kwargs[field] = parent_llm[field]

        research_llm = LLMConfig(**llm_kwargs) if llm_kwargs else LLMConfig()
        research_config = AgentConfig(
            llm=research_llm,
            mcp=MCPConfig(enabled_servers=["duckduckgo_search", "web_scraper"]),
            servers={
                "duckduckgo_search": {"type": "duckduckgo_search"},
                "web_scraper": {"type": "web_scraper"},
            },
            max_steps=8,
            network={"ssl_verify": ssl_verify},
        )

        research_registry = MCPRegistry()
        bootstrap_servers(research_config, research_registry)

        agent_config = config or {}
        agent_config.setdefault(
            "description",
            "Specialized web research agent that can search the web and scrape content from websites",
        )

        super().__init__(name, research_config, research_registry, agent_config, ssl_verify)
        self.cfg = server_cfg  # legacy compatibility expected by tests
        logger.info("Created WebResearchAgent '%s' with tools: %s", name, research_registry.list())

    async def research(self, topic: str, max_results: int = 5, request_id: str = None) -> Dict[str, Any]:
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
        # publish research start
        try:
            await publish_status(self.name, f"Research started: {topic}", request_id=request_id, phase=PHASE_START)
        except Exception:
            pass
        try:
            res = await self.call("run", {"task": research_prompt})
            try:
                await publish_status(self.name, f"Research completed: {topic}", request_id=request_id, phase=PHASE_END)
            except Exception:
                pass
            return res
        except Exception as e:
            try:
                await publish_status(self.name, f"Research failed: {str(e)}", request_id=request_id, level="error", phase=PHASE_ERROR)
            except Exception:
                pass
            raise

    async def fact_check(self, claim: str, request_id: str = None) -> Dict[str, Any]:
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
        try:
            await publish_status(self.name, f"Fact-check started: {claim[:50]}...", request_id=request_id, phase=PHASE_START)
        except Exception:
            pass
        try:
            res = await self.call("run", {"task": fact_check_prompt})
            try:
                await publish_status(self.name, f"Fact-check completed", request_id=request_id, phase=PHASE_END)
            except Exception:
                pass
            return res
        except Exception as e:
            try:
                await publish_status(self.name, f"Fact-check failed: {str(e)}", request_id=request_id, level="error", phase=PHASE_ERROR)
            except Exception:
                pass
            raise

    async def compare_sources(self, topic: str, source_urls: list[str], request_id: str = None) -> Dict[str, Any]:
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
        try:
            await publish_status(self.name, f"Compare sources started: {topic}", request_id=request_id, phase=PHASE_START)
        except Exception:
            pass
        try:
            res = await self.call("run", {"task": compare_prompt})
            try:
                await publish_status(self.name, f"Compare sources completed", request_id=request_id, phase=PHASE_END)
            except Exception:
                pass
            return res
        except Exception as e:
            try:
                await publish_status(self.name, f"Compare sources failed: {str(e)}", request_id=request_id, level="error", phase=PHASE_ERROR)
            except Exception:
                pass
            raise

    def get_schema(self) -> Dict[str, Any]:
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema = load_schema_from_dir(Path(__file__).parent, template_vars={"name": self.name})
        if not schema:
            raise RuntimeError("Missing required schema.yaml for web_research_agent plugin")
        return schema

    async def call(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
        # Extract request_id for status correlation
        request_id = params.get("request_id") or params.get("requestId")
        
        if action == "research":
            topic = params.get("topic")
            if not topic:
                return {"status": "error", "error": "Missing required parameter 'topic' for research action"}
            max_results = params.get("max_results", 5)
            return await self.research(topic, max_results, request_id)
        if action == "fact_check":
            claim = params.get("claim")
            if not claim:
                return {"status": "error", "error": "Missing required parameter 'claim' for fact_check action"}
            return await self.fact_check(claim, request_id)
        if action == "compare_sources":
            source_urls = params.get("source_urls")
            topic = params.get("topic")
            if not topic or not source_urls:
                return {"status": "error", "error": "Missing required parameters 'topic' and 'source_urls' for compare_sources action"}
            return await self.compare_sources(topic, source_urls, request_id)
        return await super().call(action, params)

    def get_default_action(self) -> str:
        return "research"
