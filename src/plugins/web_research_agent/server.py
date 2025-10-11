"""WebResearchAgent - simplified: relies on global AgentConfig inheritance.

All legacy reconstruction (parent_llm, bespoke bootstrap, context tracking 
noise) removed. The generic plugin factory now provides a full system_config 
and mcp_config; enabled_servers / filtering handled centrally.
"""
from __future__ import annotations

from typing import Dict, Any
from pathlib import Path
import logging

from agent_system.servers.agent.server import Agent

logger = logging.getLogger(__name__)


class WebResearchAgent(Agent):
    """Lean web research agent (search + scraping via configured MCP servers)."""

    # No custom __init__: base Agent handles config/LLM initialization.
    # Tools come from enabled MCP servers (duckduckgo_search, web_scraper) in config.

    async def _execute_task(self, prompt: str, request_id: str, status) -> Dict[str, Any]:  # type: ignore[override]
        """Run a task by streaming base Agent events; simplified result extraction."""
        result: Dict[str, Any] = {"task": prompt, "calls": []}
        try:
            async for event in self.run_events(prompt, request_id=request_id):
                et = event.get("type")
                if et == "mcp_call":
                    filtered_params = {k: v for k, v in event.get("params", {}).items() if not k.startswith('_')}
                    result["calls"].append({
                        "function": {"name": event.get("server", "unknown")},
                        "server": event.get("server", "unknown"),
                        "action": event.get("action", "unknown"),
                        "params": filtered_params
                    })
                elif et == "final":
                    result["summary"] = event.get("summary", "")
                elif et == "error":
                    msg = event.get("message", "error")
                    return {"status": "error", "error": msg, "agent": self.name}
            result["status"] = "success"
            result["agent"] = self.name
            return result
        except Exception as e:  # pragma: no cover - defensive
            return {"status": "error", "error": str(e), "agent": self.name}

    async def research(self, topic: str, max_results: int, request_id: str, status) -> Dict[str, Any]:
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

    async def fact_check(self, claim: str, request_id: str, status) -> Dict[str, Any]:
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

    async def compare_sources(self, topic: str, request_id: str, status) -> Dict[str, Any]:
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

    def get_tools(self) -> list[Dict[str, Any]]:
        """Return the multi-tool schema for web research agent."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema_data = load_schema_from_dir(Path(__file__).parent, template_vars={"name": self.name})
        if not schema_data:
            raise RuntimeError("Missing required schema.yaml for web_research_agent plugin")
        
        # Extract tools array from schema
        if 'tools' in schema_data:
            return schema_data['tools']
        else:
            # Fallback for single-tool schemas
            return [schema_data]



    async def call(self, tool: str, params: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
        # Extract request_id for status correlation
        request_id = params.get("request_id") or params.get("requestId")
        status = params.get("_status")   

        # Handle dynamic tool names with {{ name }} prefix
        # Tool names are now: {name}_web_research, {name}_fact_check, etc.
        if tool == f"{self.name}_web_research":
            topic = params.get("topic")
            if not topic:
                return {"status": "error", "error": "Missing required parameter 'topic' for research action"}
            max_results = params.get("max_results", 5)
            return await self.research(topic, max_results, request_id, status)
        elif tool == f"{self.name}_fact_check":
            claim = params.get("claim")
            if not claim:
                return {"status": "error", "error": "Missing required parameter 'claim' for fact_check action"}
            return await self.fact_check(claim, request_id, status)
        elif tool == f"{self.name}_source_analysis":
            topic = params.get("topic")
            if not topic:
                return {"status": "error", "error": "Missing required parameter 'topic' for compare_sources action"}
            return await self.compare_sources(topic, request_id, status)
        elif tool == f"{self.name}_research_assistant":
            # Handle general task requests by routing to research with progress tracking
            task = params.get("task") or params.get("query") or params.get("prompt")
            if not task:
                return {"status": "error", "error": f"Missing required parameter 'task' for {tool} action"}
            # Route general tasks to research method with progress tracking
            return await self.research(task, params.get("max_results", 5), request_id, status)

        raise ValueError(f"Unknown tool: {tool}")


    # ------------------------------------------------------------------
    # Custom system prompt hook override
    # ------------------------------------------------------------------
    def get_custom_system_prompt(self, context: Dict[str, Any]):  # type: ignore[override]
        """Load system_prompt from plugin-local YAML and render with Jinja2.
        
        Priority order:
        1. If agent_config.system_prompt is set (inline override), return None to use that
        2. If agent_config.system_template is set (custom template), return None to use that
        3. Otherwise load plugin's default template from prompts/system_prompt.yaml

        Returning None delegates to Agent._render_prompts() which handles config-based prompts.
        """
        # Check if config-based prompt is defined (takes precedence over plugin default)
        if hasattr(self.agent_config, 'system_prompt') and self.agent_config.system_prompt:
            logger.debug("Agent %s: system_prompt defined in config, skipping plugin template", self.name)
            return None
        
        if hasattr(self.agent_config, 'system_template') and self.agent_config.system_template:
            logger.debug("Agent %s: system_template defined in config, skipping plugin template", self.name)
            return None
        
        # No config-based prompt, load plugin's default template
        try:
            prompt_file = Path(__file__).parent / "prompts" / "system_prompt.yaml"
            if prompt_file.exists():
                # We only need the raw system_prompt block; reuse simple YAML parse
                import yaml  # Local import to avoid global dependency at import time
                from jinja2 import Template
                
                data = yaml.safe_load(prompt_file.read_text(encoding="utf-8")) or {}
                raw_prompt = data.get("system_prompt")
                if isinstance(raw_prompt, str) and raw_prompt.strip():
                    # Render template with context (tools, max_steps, datetime, etc.)
                    rendered_prompt = Template(raw_prompt).render(**context)
                    logger.debug("Agent %s: loaded and rendered plugin template (%d chars)", self.name, len(rendered_prompt))
                    return rendered_prompt
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("Failed loading custom system_prompt YAML: %s", e)
        return None
