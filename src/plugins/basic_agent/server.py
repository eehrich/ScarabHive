"""BasicAgent - the simplest agent that can execute tasks and list tools."""
from __future__ import annotations

import logging
from typing import Dict, Any

from agent_system.servers.agent.schema_based import SchemaBasedAgent
from agent_system.config.models import AgentSystemConfig, MCPConfig
from agent_system.mcp.base import MCPRegistry

logger = logging.getLogger(__name__)


class BasicAgent(SchemaBasedAgent):
    """Agent for basic requests.
    
    This agent uses SchemaBasedAgent's automatic method routing.
    Tools defined in schema.yaml are automatically routed to methods:
    - Tool: "basic_agent_execute_task" → Method: execute_task(params)
    - Tool: "basic_agent_list_available_tools" → Method: list_available_tools(params)
    
    Note: The MCP standard method list_tools() is inherited from Agent base class.
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig, registry: MCPRegistry):
        """Initialize BasicAgent with modern config system."""
        super().__init__(name, system_config, mcp_config, registry)

    async def execute_task(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a task using the basic agent.
        
        This method is automatically called for the "basic_agent_execute_task" tool.
        """
        # Extract parameters
        task = params.get("task")
        if not task:
            return {"status": "error", "error": "Missing required parameter 'task'"}
        
        request_id = params.get("request_id") or params.get("requestId")
        status = params.get("_status")
        
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
    
    async def list_available_tools(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List all available tools for this agent.
        
        This method is automatically called for the "basic_agent_list_available_tools" tool.
        """
        return await self._list_available_tools(params)
