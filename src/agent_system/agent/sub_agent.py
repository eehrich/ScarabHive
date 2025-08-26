"""
SubAgent - Agent that can be used as an MCP Server by other agents.
Enables agent-to-agent communication and hierarchical agent architectures.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from ..mcp.base import MCPServer
from ..config.models import AgentConfig
from .core import Agent

logger = logging.getLogger(__name__)


class SubAgent(MCPServer):
    """
    A SubAgent that wraps an Agent instance and exposes it as an MCP Server.
    This allows agents to use other agents as tools, enabling hierarchical architectures.
    """

    def __init__(self, name: str, agent: Agent, config: dict | None = None, ssl_verify: bool = True) -> None:
        """
        Initialize a SubAgent.
        
        Args:
            name: The name of this sub-agent (used as tool name)
            agent: The underlying Agent instance to wrap
            config: Optional configuration dict
            ssl_verify: SSL verification setting (inherited from parent)
        """
        super().__init__(name, config, ssl_verify)
        self.agent = agent
        config = config or {}  # Ensure config is not None
        self.description = config.get("description", f"Sub-agent: {name}")
        
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """
        Execute a tool call by delegating to the wrapped agent.
        
        Args:
            tool: The tool/action to execute (should be "run" or "execute") 
            params: Parameters including the task to execute
            
        Returns:
            The agent's execution result
        """
        # Validate action
        if tool not in ["run", "execute", "ask"]:
            return {
                "status": "error", 
                "error": f"Unknown action '{tool}'. Available actions: run, execute, ask"
            }
            
        # Extract task from parameters
        task = params.get("task") or params.get("query") or params.get("prompt")
        if not task:
            return {
                "status": "error",
                "error": "Missing required parameter: 'task', 'query', or 'prompt'"
            }
            
        try:
            # Execute the task using the wrapped agent
            logger.info("SubAgent %s executing task: %s", self.name, task[:100])
            result = await self.agent.run(str(task))
            
            # Wrap result with sub-agent metadata
            return {
                "status": "success",
                "sub_agent": self.name,
                "task": task,
                "result": result,
                "summary": self._extract_summary(result)
            }
            
        except Exception as e:
            logger.error("SubAgent %s failed to execute task: %s", self.name, e)
            return {
                "status": "error",
                "sub_agent": self.name, 
                "task": task,
                "error": str(e)
            }
    
    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI function schema for this sub-agent.
        
        Returns:
            OpenAI function schema dict
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
                            "enum": ["run", "execute", "ask"],
                            "description": "Action to perform (run/execute/ask the sub-agent)"
                        },
                        "task": {
                            "type": "string", 
                            "description": "The task/query/prompt to execute"
                        }
                    },
                    "required": ["task"],
                },
            },
        }
        
    def get_default_action(self) -> str:
        """Return the default action for this sub-agent."""
        return "run"
        
    def _extract_summary(self, result: Dict[str, Any]) -> str:
        """
        Extract a summary from the agent result for easier consumption.
        
        Args:
            result: The agent execution result
            
        Returns:
            A summary string
        """
        if isinstance(result, dict):
            # Look for summary in result
            if "summary" in result:
                return str(result["summary"])
            
            # If there are successful tool calls, summarize them
            calls = result.get("calls", [])
            if calls:
                successful_calls = [c for c in calls if "error" not in str(c.get("result", ""))]
                if successful_calls:
                    return f"Executed {len(successful_calls)} tool(s) successfully"
                    
            # Check for errors
            errors = result.get("errors", [])
            if errors:
                return f"Failed with {len(errors)} error(s): {errors[0]}"
                
            return "Task completed"
        
        return str(result)[:200]  # Fallback to string representation
