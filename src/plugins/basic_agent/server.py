"""
BasicAgent - Simple agent for basic task execution.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from pathlib import Path

from agent_system.servers.agent.server import Agent

logger = logging.getLogger(__name__)


class BasicAgent(Agent):
    """ Agent for basic requests."""

    def __init__(self, name: str, config: Any, registry: Any, ssl_verify: bool = True):
        """Initialize BasicAgent."""
        super().__init__(name, config, registry, None, ssl_verify)

    def get_tools(self) -> list[Dict[str, Any]]:
        """Return the tool schema for basic agent."""
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema_data = load_schema_from_dir(Path(__file__).parent, template_vars={"name": self.name})
        if not schema_data:
            raise RuntimeError("Missing required schema.yaml for basic_agent plugin")
        
        # Extract tools array from schema
        if 'tools' in schema_data:
            return schema_data['tools']
        else:
            # Fallback for single-tool schemas
            return [schema_data]

    async def call(self, tool: str, params: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
        """Handle tool calls for basic agent."""
        # Extract request_id and status for tracking
        request_id = params.get("request_id") or params.get("requestId")
        status = params.get("_status")
        
        if tool == f"{self.name}_execute_task":
            # Main agent execution tool
            task = params.get("task")
            if not task:
                return {"status": "error", "error": f"Missing required parameter 'task' for {tool}"}
            return await self._execute_task(task, request_id, status)
        elif tool == f"{self.name}_list_tools":
            # List available tools
            return await self._list_available_tools(params)
        else:
            raise ValueError(f"Unknown tool: {tool}")

    async def _execute_task(self, task: str, request_id: Optional[str], status) -> Dict[str, Any]:
        """Execute a task using the basic agent."""
        try:
            if status:
                await status.progress(f"Starting basic agent task: {task[:100]}...")
            
            # Execute the task and collect results
            result_text = ""
            step_count = 0
            tool_calls = []
            
            async for event in self.run_events(task, request_id=request_id):
                event_type = event.get("type")
                
                if event_type == "start":
                    step_count += 1
                    if status:
                        await status.progress("Starting analysis...")
                        
                elif event_type == "mcp_call":
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
                        
                elif event_type == "mcp_result":
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
                await status.progress("Task completed successfully")
            
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

    async def _list_available_tools(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List all available tools that the agent can access (simplified: only names and descriptions)."""
        try:
            # Extract status for progress updates
            status = params.get("_status")
                        
            # Get tools from all registered servers in the registry
            all_tools = []
            
            if self.registry:
                server_names = self.registry.list()
                                
                for server_name in server_names:
                    try:
                        server = self.registry.get(server_name)
                        if server and hasattr(server, 'get_tools'):
                            tools = server.get_tools()
                            for tool in tools:
                                tool_info = {
                                    "name": tool.get("function", {}).get("name", "unknown"),
                                    "description": tool.get("function", {}).get("description", "")
                                }
                                all_tools.append(tool_info)
                    except Exception as e:
                        logger.debug(f"Could not get tools from server '{server_name}': {e}")
            
            if status:
                await status.end(f"Listed available tools ({len(all_tools)} tools)")
            
            return all_tools
            
        except Exception as e:
            logger.error(f"Failed to list tools: {e}")
            return []

    def get_default_action(self) -> str:
        """Return the default action for this agent."""
        return self.name