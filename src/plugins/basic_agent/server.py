"""BasicAgent - the simplest agent that can execute tasks and list tools."""
from __future__ import annotations

import logging
from typing import Dict, Any, Optional

from agent_system.servers.agent.schema_based import SchemaBasedAgent
from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from agent_system.tools.base import ToolServerRegistry
from agent_system.llm.factory import agent_params_for_profile, create_llm_from_profile

logger = logging.getLogger(__name__)


class BasicAgent(SchemaBasedAgent):
    """Agent for basic requests.

    This agent uses SchemaBasedAgent's automatic method routing.
    Tools defined in schema.yaml are automatically routed to methods:
    - Tool: "basic_agent_execute_task" → Method: execute_task(params)
    - Tool: "basic_agent_list_available_tools" → Method: list_available_tools(params)

    Note: The MCP standard method list_tools() is inherited from Agent base class.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig, registry: ToolServerRegistry,
                 session_service: object | None = None):
        """Initialize BasicAgent with modern config system."""
        super().__init__(name, system_config, server_config, registry, session_service=session_service)

    async def execute_task(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a task using the basic agent.

        This method is automatically called for the "basic_agent_execute_task" tool.
        Supports optional llm_profile parameter to override the agent's default LLM.
        """
        # Extract parameters
        task = params.get("task")
        if not task:
            return {"status": "error", "error": "Missing required parameter 'task'"}

        request_id = params.get("request_id") or params.get("requestId") or params.get("_request_id")
        session_id = params.get("session_id") or params.get("_session_id")
        status = params.get("_status")
        llm_profile_name = params.get("llm_profile")
        use_advanced_model = params.get("use_advanced_model", False)

        # Prepare LLM override if profile specified
        llm_override: Optional[object] = None
        llm_profile_info: Optional[str] = None

        # Priority: llm_profile > use_advanced_model > default.
        # use_advanced_model wird NICHT hier gemappt, sondern als Flag an
        # run_events durchgereicht — dort passiert das Advanced-Mapping
        # zentral (inkl. llm_params) und _run_events berechnet die zur
        # Advanced-Kette passende Fallback-Reihenfolge.
        if llm_profile_name:
            # Validate profile exists in agent's available profiles
            available_profiles = self.agent_config.available_llm_profiles if self.agent_config else []
            if available_profiles and llm_profile_name not in available_profiles:
                return {
                    "status": "error",
                    "error": f"LLM profile '{llm_profile_name}' not available for this agent. Available profiles: {available_profiles}"
                }

            # Validate profile exists in system config
            if llm_profile_name not in self.system_config.llm_system.profiles:
                return {
                    "status": "error",
                    "error": f"LLM profile '{llm_profile_name}' not found in system configuration. Available system profiles: {list(self.system_config.llm_system.profiles.keys())}"
                }

            try:
                # create_llm_from_profile, not the raw registry: it forwards EVERY
                # resolved field. Hand-listing the arguments dropped
                # thinking_level, max_tokens, safety_settings, service_tier and
                # provider_routing — invisible for the profiles configured
                # today, and silently wrong the moment this agent is pointed at
                # an OpenRouter profile. The profile here comes from a runtime
                # tool argument, so that moment is one config line away.
                #
                # And with the agent's own llm_params: the argument picks another
                # MODEL, not another agent, so what the agent says about every
                # model it runs on ("*") holds here as it does for a fallback.
                llm_override = create_llm_from_profile(
                    self.system_config, llm_profile_name,
                    llm_params=agent_params_for_profile(self.agent_config, llm_profile_name))

                # Create profile info for logging
                profile = self.system_config.llm_system.profiles[llm_profile_name]
                model_ref = profile.model_ref
                model_config = self.system_config.llm_system.models[model_ref]
                llm_profile_info = f"{llm_profile_name}:{model_config.provider}/{model_config.model}"

                logger.info(f"Using LLM profile override: {llm_profile_info}")

                if status:
                    await status.progress(f"Using LLM profile: {llm_profile_info}")

            except Exception as e:
                logger.error(f"Failed to create LLM override for profile '{llm_profile_name}': {e}")
                return {
                    "status": "error",
                    "error": f"Failed to initialize LLM profile '{llm_profile_name}': {str(e)}"
                }

        try:
            if status:
                await status.progress(f"Starting basic agent task: {task[:100]}...")

            # Execute the task and collect results
            result_text = ""
            step_count = 0
            tool_calls = []

            async for event in self.run_events(
                task,
                request_id=request_id,
                session_id=session_id,
                llm_override=llm_override,
                llm_profile_info_override=llm_profile_info,
                # Explizites llm_profile gewinnt: dann kein Advanced-Mapping.
                use_advanced_model=bool(use_advanced_model) and not llm_profile_name
            ):
                event_type = event.get("type")

                if event_type == "start":
                    step_count += 1
                    if status:
                        await status.progress("Starting analysis...")

                elif event_type in ("tool_call", "mcp_call"):  # the old name until every deployed side is new (rename 17.09.2026)
                    step_count += 1
                    tool_name = event.get("server", "unknown")
                    action = event.get("action", "unknown")
                    if status:
                        await status.progress(f"Step {step_count} - Using {tool_name} ({action})")

                    # Store tool calls for result summary
                    filtered_params = {k: v for k, v in event.get("params", {}).items() if not k.startswith('_')}
                    tool_calls.append({
                        "tool": tool_name,
                        "action": action,
                        "params": filtered_params
                    })

                elif event_type in ("tool_result", "mcp_result"):  # the old name until every deployed side is new (rename 17.09.2026)
                    tool_name = event.get("server", "unknown")
                    if status:
                        await status.progress(f"Processing results from {tool_name}...")

                elif event_type == "final":
                    # This is where the actual result is!
                    result_text = event.get("summary", "") or event.get("message", "")
                    if status:
                        await status.progress("Finalizing results...")

                elif event_type == "error":
                    error_msg = event.get("message", "Unknown error")
                    if status:
                        await status.error(f"Task failed: {error_msg}")
                    return {
                        "status": "error",
                        "error": error_msg,
                        "request_id": request_id
                    }

            if status:
                # end, not progress: this was the last thing said, so the
                # scope's default overwrote it with a bare "completed" and
                # threw away steps, tool calls and result size.
                task_text = params.get("task") or ""
                subject = task_text if len(task_text) <= 60 else task_text[:57] + "..."
                await status.end(
                    f"{step_count} step(s), {len(tool_calls)} tool call(s), "
                    f"{len(result_text or '')} chars -- {subject}")

            # Return the actual result from the final event
            return {
                "status": "success",
                "result": result_text or "Task completed successfully",
                "tool_calls": tool_calls,
                "steps": step_count,
                "request_id": request_id
            }

        except Exception as e:
            if status:
                await status.error(f"Task execution failed: {str(e)}")
            logger.error(f"Task execution failed: {e}")
            return {
                "status": "error",
                "error": str(e),
                "request_id": request_id
            }

    async def list_available_tools(self, params: Dict[str, Any]) -> list[Dict[str, Any]]:
        """List all available tools for this agent.

        This method is automatically called for the "basic_agent_list_available_tools" tool.
        """
        return await self._list_usable_tools_with_details(params)
