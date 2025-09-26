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
    StatusScope
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
    
    # Try new profile-based LLM resolution first
    research_llm = None
    if isinstance(parent_llm, dict) and parent_llm.get('llm_system'):
        from agent_system.llm.factory import resolve_llm_config_for_agent
        
        try:
            # Convert dictionary to AgentConfig-like object for resolve_llm_config_for_agent
            from agent_system.config.models import LLMSystemConfig
            from agent_system.config.models import LLMConfig as BaseLLMConfig
            
            # Create AgentConfig from parent_llm dictionary
            temp_config = AgentConfig(
                llm=BaseLLMConfig(**(parent_llm.get('llm', {}))),
                llm_system=LLMSystemConfig(**(parent_llm.get('llm_system', {}))),
                agent_llm_profiles=parent_llm.get('agent_llm_profiles', {}),
            )
            
            # Use agent name to resolve LLM profile
            resolved_kwargs = resolve_llm_config_for_agent(temp_config, name)
            logger.info(f"WebResearchAgent '{name}' using LLM profile resolution: provider={resolved_kwargs['provider']}, model={resolved_kwargs['model']}")
            research_llm = LLMConfig(**resolved_kwargs)
        except Exception as e:
            logger.error(f"Failed to resolve LLM profile for {name}: {e}")
            raise ValueError(f"WebResearchAgent requires proper LLM profile configuration: {e}")

    # Profile resolution should have succeeded
    if research_llm is None:
        raise ValueError(f"WebResearchAgent LLM configuration failed for {name}")

    # Allow server config to override max_steps (fall back to default 50)
    resolved_max_steps = int(server_cfg.get("max_steps", 50)) if isinstance(server_cfg, dict) else 50

    research_config = AgentConfig(
        llm=research_llm,
        llm_system=LLMSystemConfig(**(parent_llm.get("llm_system", {}))),
        agent_llm_profiles=parent_llm.get("agent_llm_profiles", {}),
        mcp=MCPConfig(enabled_servers=["duckduckgo_search", "web_scraper"]),
        servers={
            "duckduckgo_search": {
                "type": "duckduckgo_search"
            },
            "web_scraper": {
                "type": "web_scraper"
            }
        },
        max_steps=resolved_max_steps,  # More steps for complex research tasks
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
        # Import needed models at the top
        from agent_system.config.models import AgentConfig, MCPConfig, LLMConfig, LLMSystemConfig
        from agent_system.llm.factory import resolve_llm_config_for_agent
        
        server_cfg = config or {}
        parent_llm = server_cfg.get("parent_llm") if isinstance(server_cfg, dict) else None
        
        # If parent config has the new LLM system, use it
        if isinstance(parent_llm, dict) and parent_llm.get("llm_system"):
            # Build a full config from parent_llm for LLM resolution
            temp_config = AgentConfig(
                llm=LLMConfig(**(parent_llm.get("llm", {}))),
                llm_system=LLMSystemConfig(**(parent_llm.get("llm_system", {}))),
                agent_llm_profiles=parent_llm.get("agent_llm_profiles", {}),
            )
            
            # Resolve LLM config for this specific agent name
            llm_kwargs = resolve_llm_config_for_agent(temp_config, name)
            
            # Create LLMConfig from resolved kwargs
            research_llm = LLMConfig(
                provider=llm_kwargs["provider"],
                model=llm_kwargs["model"],
                openai_api_key=llm_kwargs["openai_api_key"],
                ollama_url=llm_kwargs["ollama_url"],
                context_window=llm_kwargs["context_window"],
                ollama_mode=llm_kwargs["ollama_mode"],
                request_timeout=llm_kwargs["request_timeout"],
            )
            
            logger.info("WebResearchAgent '%s' using LLM profile resolution: provider=%s, model=%s", 
                       name, llm_kwargs["provider"], llm_kwargs["model"])
        
        else:
            # Profile-based configuration is required
            raise ValueError(f"WebResearchAgent '{name}' requires LLM system configuration with profiles")

        # Allow server config to override max_steps (fall back to default 50)
        resolved_max_steps = int(server_cfg.get("max_steps", 50)) if isinstance(server_cfg, dict) else 50

        research_config = AgentConfig(
            llm=research_llm,
            llm_system=LLMSystemConfig(**(parent_llm.get("llm_system", {}))),
            agent_llm_profiles=parent_llm.get("agent_llm_profiles", {}),
            mcp=MCPConfig(enabled_servers=["duckduckgo_search", "web_scraper"]),
            servers={
                "duckduckgo_search": {"type": "duckduckgo_search"},
                "web_scraper": {"type": "web_scraper"},
            },
            max_steps=resolved_max_steps,
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

    async def _run_with_progress(self, task_prompt: str, request_id: str, status: StatusScope) -> Dict[str, Any]:
        """Run agent task with progress updates published as status events."""
        results = {"task": task_prompt, "calls": []}
        step_count = 0
        total_messages = 0

        try:
            # Pass the request_id to run_events so coordinator/worker messages have correct correlation
            # Consume all events but don't break early to let base Agent.run_events complete
            events_generator = self.run_events(task_prompt, request_id=request_id)
            async for event in events_generator:
                event_type = event.get("type")

                if event_type == "start":
                    step_count += 1
                    await status.progress("Starting analysis...")

                elif event_type == "thinking":
                    # Track LLM conversation activity
                    total_messages += 1
                    await status.progress(f"Processing {total_messages} ...")
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
                    await status.progress(f"Step {step_count} - Using {tool_name} ({action})")
                    
                    # Store tool calls in results - convert to expected format
                    if "calls" not in results:
                        results["calls"] = []
                    # Filter out internal parameters like _status before storing
                    filtered_params = {k: v for k, v in event.get("params", {}).items() if not k.startswith('_')}
                    results["calls"].append({
                        "function": {"name": tool_name},
                        "server": tool_name,
                        "action": action,
                        "params": filtered_params
                    })

                elif event_type == "mcp_result":
                    tool_name = event.get("server", "unknown")
                    await status.progress(f"Processing results from {tool_name}...")

                elif event_type == "final":
                    results["summary"] = event.get("summary", "")
                    await status.progress("Finalizing results...")

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

                    # Continue consuming events to let base Agent.run_events complete 
                    # and publish final coordinator/worker status messages

                elif event_type == "error":
                    error_msg = event.get("message", "Unknown error")
                    if not error_msg or error_msg.strip() == "":
                        error_msg = "Agent error occurred without details"
                    results.setdefault("errors", []).append(error_msg)
                    await status.error(f"Error - {error_msg}")
                    raise Exception(error_msg)

                elif event_type == "end":
                    # Mark that we've seen the end event but continue consuming
                    # to let base Agent.run_events complete and publish final status
                    # Continue loop to let generator finish naturally
                    pass

            # Let the generator complete naturally to ensure final status publishing
            # The async for loop will exit when the generator is exhausted
            # status_scope will automatically publish coordinator/worker END messages
            return results

        except Exception as e:
            # Error handling - status_scope will still publish proper END status
            logger.error(f"WebResearchAgent task failed: {e}")
            results.setdefault("errors", []).append(str(e))
            raise

    async def _execute_task(self, prompt: str, request_id: str, status: StatusScope) -> Dict[str, Any]:
        """Execute a research task with common error handling and result formatting."""
        try:
            res = await self._run_with_progress(prompt, request_id, status)
            # Add status and agent info to match expected format
            res["status"] = "success"
            res["agent"] = self.name
            return res
        except Exception as e:
            return {"status": "error", "error": str(e), "agent": self.name}

    async def research(self, topic: str, max_results: int, request_id: str, status: StatusScope) -> Dict[str, Any]:
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
        return await self._execute_task(research_prompt, request_id, status)

    async def fact_check(self, claim: str, request_id: str, status: StatusScope) -> Dict[str, Any]:
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
        return await self._execute_task(fact_check_prompt, request_id, status)

    async def compare_sources(self, topic: str, request_id: str, status: StatusScope) -> Dict[str, Any]:
        compare_prompt = f"""
        Compare information about this topic across multiple sources: "{topic}"

        Instructions:
        1. Search for information about this topic from multiple perspectives
        2. Include diverse sources (news, academic, blogs, official sites)
        3. Scrape content from various credible sources
        4. Compare and contrast the information provided
        5. Identify consensus, disagreements, and biases

        Please provide:
        - Summary of common information
        - Points of agreement and disagreement
        - Source credibility assessment
        - Potential biases or limitations
        """
        return await self._execute_task(compare_prompt, request_id, status)

    def get_schema(self) -> Dict[str, Any]:
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema = load_schema_from_dir(Path(__file__).parent, template_vars={"name": self.name})
        if not schema:
            raise RuntimeError("Missing required schema.yaml for web_research_agent plugin")
        return schema

    async def call(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
        # Extract request_id for status correlation
        request_id = params.get("request_id") or params.get("requestId")
        status = params.get("_status")   

        if action == "research":
            topic = params.get("topic")
            if not topic:
                return {"status": "error", "error": "Missing required parameter 'topic' for research action"}
            max_results = params.get("max_results", 5)
            return await self.research(topic, max_results, request_id, status)
        elif action == "fact_check":
            claim = params.get("claim")
            if not claim:
                return {"status": "error", "error": "Missing required parameter 'claim' for fact_check action"}
            return await self.fact_check(claim, request_id, status)
        elif action == "compare_sources":
            topic = params.get("topic")
            if not topic:
                return {"status": "error", "error": "Missing required parameter 'topic' for compare_sources action"}
            return await self.compare_sources(topic, request_id, status)
        elif action in ("run", "execute", "ask"):
            # Handle general task requests by routing to research with progress tracking
            task = params.get("task") or params.get("query") or params.get("prompt")
            if not task:
                return {"status": "error", "error": f"Missing required parameter 'task' for {action} action"}
            # Route general tasks to research method with progress tracking
            return await self.research(task, params.get("max_results", 5), request_id, status)

        return await super().call(action, params)

    def get_default_action(self) -> str:
        return "research"
