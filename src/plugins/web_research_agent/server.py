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
from agent_system.context.agent_tracker import update_agent_context_usage

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
    if server_cfg.get("context_window") is not None:
        llm_kwargs["context_window"] = server_cfg.get("context_window")

    # Inherit missing LLM fields from a provided parent/global config dict (if caller
    # passed a reference containing top-level llm info under 'parent_llm'). This avoids
    # forcing duplication of openai_api_key or model in server-specific config.
    parent_llm = server_cfg.get("parent_llm") if isinstance(server_cfg, dict) else None
    if isinstance(parent_llm, dict):  # expected shape: {'provider':..., 'model':..., 'openai_api_key':...}
        for field in ("provider", "model", "openai_api_key", "ollama_url", "ollama_mode", "request_timeout", "context_window"):
            if field not in llm_kwargs and parent_llm.get(field) is not None:
                llm_kwargs[field] = parent_llm[field]

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
        for key in ("model", "openai_api_key", "ollama_url", "ollama_mode", "request_timeout", "context_window"):
            if server_cfg.get(key) is not None:
                llm_kwargs[key] = server_cfg.get(key)

        # Inherit missing LLM fields from a provided parent/global config dict (if caller
        # passed a reference containing top-level llm info under 'parent_llm'). This avoids
        # forcing duplication of openai_api_key or model in server-specific config.
        parent_llm = server_cfg.get("parent_llm") if isinstance(server_cfg, dict) else None
        if isinstance(parent_llm, dict):  # expected shape: {'provider':..., 'model':..., 'openai_api_key':...}
            for field in ("provider", "model", "openai_api_key", "ollama_url", "ollama_mode", "request_timeout", "context_window"):
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

    async def _run_with_progress(self, task_prompt: str, operation_name: str, request_id: str = None) -> Dict[str, Any]:
        """Run agent task with progress updates published as status events."""
        results = {"task": task_prompt, "calls": []}
        step_count = 0
        total_messages = 0

        try:
            async for event in self.run_events(task_prompt):
                event_type = event.get("type")

                if event_type == "start":
                    step_count += 1
                    try:
                        await publish_status(
                            self.name,
                            f"{operation_name}: Starting analysis...",
                            request_id=request_id,
                            phase=PHASE_PROGRESS
                        )
                    except Exception as e:
                        logger.warning(f"Failed to publish start status: {e}")

                elif event_type == "thinking":
                    # Track LLM conversation activity
                    total_messages += 1
                    try:
                        # Get conversation context for tracking
                        if hasattr(self, '_current_messages'):
                            message_count = len(self._current_messages)
                            # Estimate tokens from current conversation
                            estimated_tokens = self._estimate_token_count(self._current_messages) if hasattr(self, '_estimate_token_count') else 0

                            # Update agent context tracker
                            update_agent_context_usage(
                                self.name,
                                current_tokens=estimated_tokens,
                                predicted_tokens=estimated_tokens,
                                message_count=message_count
                            )
                    except Exception as e:
                        logger.debug("Failed to update agent context stats: %s", e)

                elif event_type == "mcp_call":
                    step_count += 1
                    tool_name = event.get("server", "unknown")
                    action = event.get("action", "unknown")
                    try:
                        await publish_status(
                            self.name,
                            f"{operation_name}: Step {step_count} - Using {tool_name} ({action})",
                            request_id=request_id,
                            phase=PHASE_PROGRESS
                        )
                    except Exception as e:
                        logger.warning(f"Failed to publish mcp_call status: {e}")

                    # Store tool calls in results - convert to expected format
                    if "calls" not in results:
                        results["calls"] = []
                    results["calls"].append({
                        "function": {"name": tool_name},
                        "server": tool_name,
                        "action": action,
                        "params": event.get("params", {})
                    })

                elif event_type == "mcp_result":
                    tool_name = event.get("server", "unknown")
                    try:
                        await publish_status(
                            self.name,
                            f"{operation_name}: Processing results from {tool_name}...",
                            request_id=request_id,
                            phase=PHASE_PROGRESS
                        )
                    except Exception as e:
                        logger.warning(f"Failed to publish mcp_result status: {e}")

                elif event_type == "final":
                    results["summary"] = event.get("summary", "")
                    try:
                        await publish_status(
                            self.name,
                            f"{operation_name}: Finalizing results...",
                            request_id=request_id,
                            phase=PHASE_PROGRESS
                        )
                    except Exception as e:
                        logger.warning(f"Failed to publish final status: {e}")

                    # Final agent tracking update
                    try:
                        if hasattr(self, '_current_messages'):
                            message_count = len(self._current_messages)
                            estimated_tokens = self._estimate_token_count(self._current_messages) if hasattr(self, '_estimate_token_count') else 0

                            # Check if LLM usage data is available in the event
                            actual_tokens = event.get("usage", {}).get("total_tokens", 0) if event.get("usage") else 0

                            update_agent_context_usage(
                                self.name,
                                current_tokens=estimated_tokens,
                                predicted_tokens=estimated_tokens,
                                message_count=message_count,
                                actual_tokens=actual_tokens if actual_tokens > 0 else None
                            )
                    except Exception as e:
                        logger.debug("Failed to update final agent context stats: %s", e)

                    break

                elif event_type == "error":
                    error_msg = event.get("message", "Unknown error")
                    results.setdefault("errors", []).append(error_msg)
                    try:
                        await publish_status(
                            self.name,
                            f"{operation_name}: Error - {error_msg}",
                            request_id=request_id,
                            level="error",
                            phase=PHASE_ERROR
                        )
                    except Exception:
                        pass
                    raise Exception(error_msg)

                elif event_type == "end":
                    break

            return results

        except Exception as e:
            results.setdefault("errors", []).append(str(e))
            try:
                await publish_status(
                    self.name,
                    f"{operation_name}: Failed - {str(e)}",
                    request_id=request_id,
                    level="error",
                    phase=PHASE_ERROR
                )
            except Exception:
                pass
            raise

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
            logger.info(f"Published research start status for request_id={request_id}")
        except Exception as e:
            logger.error(f"Failed to publish research start status: {e}")

        try:
            res = await self._run_with_progress(research_prompt, f"Researching '{topic}'", request_id)
            # Add status and agent info to match expected format
            res["status"] = "success"
            res["agent"] = self.name
            try:
                await publish_status(self.name, f"Research completed: {topic}", request_id=request_id, phase=PHASE_END)
                logger.info(f"Published research end status for request_id={request_id}")
            except Exception as e:
                logger.error(f"Failed to publish research end status: {e}")
            return res
        except Exception as e:
            try:
                await publish_status(self.name, f"Research failed: {str(e)}", request_id=request_id, level="error", phase=PHASE_ERROR)
            except Exception:
                pass
            return {"status": "error", "error": str(e), "agent": self.name}

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
            res = await self._run_with_progress(fact_check_prompt, "Fact-checking claim", request_id)
            # Add status and agent info to match expected format
            res["status"] = "success"
            res["agent"] = self.name
            try:
                await publish_status(self.name, "Fact-check completed", request_id=request_id, phase=PHASE_END)
            except Exception:
                pass
            return res
        except Exception as e:
            try:
                await publish_status(self.name, f"Fact-check failed: {str(e)}", request_id=request_id, level="error", phase=PHASE_ERROR)
            except Exception:
                pass
            return {"status": "error", "error": str(e), "agent": self.name}

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
            res = await self._run_with_progress(compare_prompt, f"Comparing sources for '{topic}'", request_id)
            # Add status and agent info to match expected format
            res["status"] = "success"
            res["agent"] = self.name
            try:
                await publish_status(self.name, "Compare sources completed", request_id=request_id, phase=PHASE_END)
            except Exception:
                pass
            return res
        except Exception as e:
            try:
                await publish_status(self.name, f"Compare sources failed: {str(e)}", request_id=request_id, level="error", phase=PHASE_ERROR)
            except Exception:
                pass
            return {"status": "error", "error": str(e), "agent": self.name}

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
        elif action == "fact_check":
            claim = params.get("claim")
            if not claim:
                return {"status": "error", "error": "Missing required parameter 'claim' for fact_check action"}
            return await self.fact_check(claim, request_id)
        elif action == "compare_sources":
            source_urls = params.get("source_urls")
            topic = params.get("topic")
            if not topic or not source_urls:
                return {"status": "error", "error": "Missing required parameters 'topic' and 'source_urls' for compare_sources action"}
            return await self.compare_sources(topic, source_urls, request_id)
        elif action in ("run", "execute", "ask"):
            # Handle general task requests by routing to research with progress tracking
            task = params.get("task") or params.get("query") or params.get("prompt")
            if not task:
                return {"status": "error", "error": f"Missing required parameter 'task' for {action} action"}
            # Route general tasks to research method with progress tracking
            return await self.research(task, params.get("max_results", 5), request_id)

        return await super().call(action, params)

    def get_default_action(self) -> str:
        return "research"
