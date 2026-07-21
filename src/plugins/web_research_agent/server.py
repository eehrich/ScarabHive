"""WebResearchAgent - simplified: relies on global AgentConfig inheritance.

All legacy reconstruction (parent_llm, bespoke bootstrap, context tracking 
noise) removed. The generic plugin factory now provides a full system_config 
and mcp_config; enabled_servers / filtering handled centrally.
"""
from __future__ import annotations

from typing import Dict, Any
import logging

from agent_system.servers.agent.schema_based import SchemaBasedAgent

logger = logging.getLogger(__name__)


class WebResearchAgent(SchemaBasedAgent):
    """Lean web research agent (search + scraping via configured MCP servers).
    
    Uses SchemaBasedAgent's automatic method routing.
    Tools defined in schema.yaml are automatically routed to methods:
    - Tool: "web_research_agent_web_research" → Method: web_research(params)
    - Tool: "web_research_agent_fact_check" → Method: fact_check(params)
    - Tool: "web_research_agent_source_analysis" → Method: source_analysis(params)
    - Tool: "web_research_agent_research_assistant" → Method: research_assistant(params)
    """

    # No custom __init__: base Agent handles config/LLM initialization.
    # Tools come from enabled MCP servers (duckduckgo_search, web_scraper) in config.

    async def web_research(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Perform comprehensive web research.
        
        This method is automatically called for the "web_research_agent_web_research" tool.
        """
        topic = params.get("topic")
        if not topic:
            return {"status": "error", "error": "Missing required parameter 'topic'"}
        
        max_results = params.get("max_results", 5)
        request_id = params.get("request_id") or params.get("requestId")
        
        research_prompt = f"""
Perform comprehensive research.

Topic: {topic}

Instructions:
1. choose the right tools to use for this research.
2.1. if no web research is needed and a better tool is available:
    a. use that tool first.
    b. in case the tool does not provide enough information, repeat the research using web_search 2.2.
2.2. if web search is needed:
    a. search for recent information about the topic using web search tool.
    b. From the search results, identify the {max_results} most relevant sources.
    c. Scrape content from those sources to get detailed information.
3. Ensure all information is up-to-date and from credible sources.
4. Synthesize the findings into a comprehensive research summary.        

Please provide:
- Key findings and insights
- Important facts and data points
- Different perspectives or viewpoints
- Source URLs for verification
"""
        return await self._run_task(research_prompt, request_id, params.get("_session_id"), params.get("_status"))
    
    async def fact_check(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fact-check a specific claim.
        
        This method is automatically called for the "web_research_agent_fact_check" tool.
        """
        claim = params.get("claim")
        if not claim:
            return {"status": "error", "error": "Missing required parameter 'claim'"}
        
        request_id = params.get("request_id") or params.get("requestId")
        
        fact_check_prompt = f"""
Fact-check this claim: "{claim}"

Instructions:
1. Search for information about this specific claim
2. Look for authoritative sources (news, academic, official sites)
3. Scrape content from credible sources
4. Analyze the evidence for and against the claim
5. Provide a verdict with supporting evidence

Please provide:
- Verdict (True/False/Partially True/Unverified)
- Evidence supporting or refuting the claim
- Quality and credibility of sources
- Important context or nuances
- Source URLs for verification
"""
        return await self._run_task(fact_check_prompt, request_id, params.get("_session_id"), params.get("_status"))
    
    async def source_analysis(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze and compare sources for a topic.
        
        This method is automatically called for the "web_research_agent_source_analysis" tool.
        """
        topic = params.get("topic")
        if not topic:
            return {"status": "error", "error": "Missing required parameter 'topic'"}
        
        request_id = params.get("request_id") or params.get("requestId")
        
        compare_prompt = f"""
Compare and analyze multiple sources about: {topic}

Instructions:
1. Search for information from multiple sources
2. Scrape content from diverse sources (news, academic, blogs, official)
3. Compare how different sources present the information
4. Identify common facts vs. differing opinions
5. Assess source credibility and potential biases

Please provide:
- Common facts agreed upon by multiple sources
- Points of disagreement or different perspectives
- Source credibility assessment
- Identified biases or agendas
- Synthesis of the most reliable information
- Source URLs for each perspective
"""
        return await self._run_task(compare_prompt, request_id, params.get("_session_id"), params.get("_status"))
    
    async def research_assistant(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """General research assistant for any task.
        
        This method is automatically called for the "web_research_agent_research_assistant" tool.
        """
        task = params.get("task") or params.get("query") or params.get("prompt")
        if not task:
            return {"status": "error", "error": "Missing required parameter 'task'"}
        
        request_id = params.get("request_id") or params.get("requestId")
        
        # For general tasks, just pass through to the agent
        return await self._run_task(task, request_id, params.get("_session_id"), params.get("_status"))

    async def _run_task(self, prompt: str, request_id: str, session_id: str | None, status) -> Dict[str, Any]:
        """Run a task by streaming agent events and collecting results."""
        result: Dict[str, Any] = {"task": prompt, "calls": []}
        try:
            async for event in self.run_events(prompt, request_id=request_id, session_id=session_id):
                event_type = event.get("type")
                if event_type == "mcp_call":
                    filtered_params = {k: v for k, v in event.get("params", {}).items() if not k.startswith('_')}
                    result["calls"].append({
                        "function": {"name": event.get("server", "unknown")},
                        "server": event.get("server", "unknown"),
                        "action": event.get("action", "unknown"),
                        "params": filtered_params
                    })
                elif event_type == "final":
                    result["summary"] = event.get("summary", "")
                elif event_type == "error":
                    error_msg = event.get("message", "error")
                    return {"status": "error", "error": error_msg, "agent": self.name}
            
            result["status"] = "success"
            result["agent"] = self.name
            return result
        except Exception as e:  # pragma: no cover - defensive
            return {"status": "error", "error": str(e), "agent": self.name}


    # ------------------------------------------------------------------
    # Custom system prompt hook override
    # ------------------------------------------------------------------
    def get_custom_system_prompt(self, context: Dict[str, Any]):  # type: ignore[override]
        """Load system_prompt from plugin-local YAML and render with Jinja2.
        
        Priority order:
        1. If agent_config.system_prompt is set (inline override), return None to use that
        2. If agent_config.system_template is set (custom template), return None to use that
        3. Otherwise load plugin's default template from prompts/system_prompt.md

        Returning None delegates to Agent._render_prompts() which handles config-based prompts.
        """
        # Check if config-based prompt is defined (takes precedence over plugin default)
        if hasattr(self.agent_config, 'system_prompt') and self.agent_config.system_prompt:
            logger.debug("Agent %s: system_prompt defined in config, skipping plugin template", self.name)
            return None
        
        if hasattr(self.agent_config, 'system_template') and self.agent_config.system_template:
            logger.debug("Agent %s: system_template defined in config, skipping plugin template", self.name)
            return None
        
        # No config-based prompt, load plugin's default markdown template
        try:
            from pathlib import Path
            from jinja2 import Template

            prompt_file = Path(__file__).parent / "prompts" / "system_prompt.md"
            if prompt_file.exists():
                raw_prompt = prompt_file.read_text(encoding="utf-8")
                if raw_prompt.strip():
                    # Render Jinja2 with context (tools, max_steps, datetime, etc.)
                    rendered_prompt = Template(raw_prompt).render(**context)
                    logger.debug("Agent %s: loaded and rendered plugin template (%d chars)", self.name, len(rendered_prompt))
                    return rendered_prompt
        except Exception as e:  # pragma: no cover - defensive
            # Warn (not debug): the agent silently degrades to the generic
            # default prompt if this fails, losing its tool guidance.
            logger.warning("Failed loading custom system_prompt markdown: %s", e)
        return None
