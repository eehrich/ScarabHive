"""
Tool Execution Manager for Agent Server
Handles execution of both internal plugin tools and external MCP tools.
"""
import json
import logging
import time
from typing import Dict, List, Any

from ....llm.clients import ChatMessage
from ....utils.text_sanitizer import sanitize_for_llm, sanitize_json_content
from ....mcp.integration import get_mcp_integration

logger = logging.getLogger(__name__)


class ToolExecutionManager:
    """Manages execution of tools and handles results."""

    def __init__(self, registry: Any, agent=None):
        self.registry = registry
        # Optional Agent instance for centralized counters
        self._agent = agent

    def _make_params_serializable(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create a JSON-serializable copy of params by excluding non-serializable objects.
        
        This is needed because params may contain StatusScope objects or other non-JSON-serializable
        objects that are added for internal use but shouldn't be included in event data.
        """
        serializable_params = {}
        for key, value in params.items():
            # Skip parameters starting with "_" (internal objects)
            if key.startswith('_'):
                continue
            # Try to serialize the value to check if it's JSON-serializable
            try:
                json.dumps(value)
                serializable_params[key] = value
            except (TypeError, ValueError):
                # Skip non-serializable values
                continue
        return serializable_params

    async def _invoke_tool(self, tool_name: str, params: Dict[str, Any]):
        """Execute a tool call against the registry and return results."""
        if tool_name not in self.registry.list():
            raise RuntimeError(f"Unknown tool: {tool_name}")
        server = self.registry.get(tool_name)
        action_name = params.get("action") or server.get_default_action()
        try:
            result = await server.call_with_status(action_name, params)
            return result
        except Exception as e:
            logger.exception("Tool %s invocation failed: %s", tool_name, e)
            raise

    async def execute_tools(self, tool_calls: List[Dict], tool_name_mapping: Dict[str, str],
                          available_tools: List[str], step: int, request_id: str | None = None) -> tuple[List[ChatMessage], List[Dict], List[Dict]]:
        """Execute all tool calls and return tool result messages, events, and results.
        
        Returns:
            Tuple of (tool_messages, events_to_yield, results_to_add)
        """
        tool_messages = []
        events_to_yield = []
        results_to_add = []
        
        # Prepare tool executions (parse arguments and validate tools)
        valid_tool_executions = []
        
        for i, tc in enumerate(tool_calls):
            func = tc.get("function", {})
            openai_tool_name = func.get("name")  # This is the OpenAI-compatible name
            raw_args = func.get("arguments")

            # Map back to original tool name if it was converted
            tool_name = tool_name_mapping.get(openai_tool_name, openai_tool_name)

            # Parse arguments
            params: Dict[str, Any] = {}
            if isinstance(raw_args, str) and raw_args:
                try:
                    params = json.loads(raw_args)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse tool arguments: %s", raw_args)
                    params = {}
            elif isinstance(raw_args, dict):
                params = raw_args

            if not tool_name or tool_name not in available_tools:
                logger.warning("Unknown tool requested: %s (OpenAI name: %s)", tool_name, openai_tool_name)
                events_to_yield.append({"type": "error", "message": f"Unknown tool: {tool_name}"})
                # Add error result for this specific tool call
                tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                tool_messages.append(ChatMessage(
                    role="tool",
                    tool_call_id=tool_call_id,
                    name=openai_tool_name or "unknown",
                    content=json.dumps({"error": f"Tool '{tool_name}' is not available."})
                ))
                continue

            # Store valid tool execution for parallel processing
            valid_tool_executions.append((tc, tool_name, openai_tool_name, params))

        # Execute all valid tools in parallel
        if valid_tool_executions:
            import asyncio
            
            # Create tasks for parallel execution with unique request_id suffixes
            tasks = []
            for i, (tc, tool_name, openai_tool_name, params) in enumerate(valid_tool_executions):
                # Create tool-specific request_id suffix for all tool calls to ensure unique IDs
                # If original request_id is "abc123", tool calls become "abc123_001", "abc123_002", etc.
                original_request_id = params.get("request_id") or params.get("requestId") or request_id
                if original_request_id:
                    # Prefer the Agent counter when available so suffixes are globally unique
                    if self._agent is not None:
                        try:
                            tool_specific_request_id = await self._agent.next_internal_tool_request_id(original_request_id)
                        except Exception:
                            tool_specific_request_id = f"{original_request_id}_{i+1:03d}"
                    else:
                        # Local deterministic suffix if no Agent provided
                        tool_specific_request_id = f"{original_request_id}_{i+1:03d}"

                    # Update params with tool-specific request_id for status tracking
                    params_with_suffix = params.copy()
                    params_with_suffix["request_id"] = tool_specific_request_id
                    # Also set camelCase version for JS compatibility
                    params_with_suffix["requestId"] = tool_specific_request_id
                else:
                    # No request_id available - use original params
                    params_with_suffix = params
                    tool_specific_request_id = None
                
                # Pass the tool-specific request_id to ensure events use the correct ID
                tasks.append(
                    self._execute_single_tool(tc, tool_name, openai_tool_name, params_with_suffix, step, tool_specific_request_id)
                )
            
            # Execute all tools concurrently
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results (maintain order based on original tool_calls order)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    # Handle exceptions from parallel execution
                    tc, tool_name, openai_tool_name, params = valid_tool_executions[i]
                    logger.exception("Tool execution failed for %s: %s", tool_name, result)
                    tool_call_id = tc.get("id") or f"{tool_name}-error-{int(time.time()*1000)}"
                    tool_messages.append(ChatMessage(
                        role="tool",
                        tool_call_id=tool_call_id,
                        name=openai_tool_name,
                        content=json.dumps({"error": f"Tool execution failed: {str(result)}"})
                    ))
                else:
                    # Normal result
                    tool_message, events, tool_results = result
                    tool_messages.append(tool_message)
                    events_to_yield.extend(events)
                    results_to_add.extend(tool_results)

        return tool_messages, events_to_yield, results_to_add

    async def _execute_single_tool(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                 params: Dict[str, Any], step: int, request_id: str | None = None) -> tuple[ChatMessage, List[Dict], List[Dict]]:
        """Execute a single tool and return the result message, events, and results."""
        if "." in tool_name:
            # External tool - call via MCP integration
            return await self._execute_external_tool(tc, tool_name, openai_tool_name, params, step, request_id)
        else:
            # Plugin tool - use existing logic
            return await self._execute_plugin_tool(tc, tool_name, openai_tool_name, params, step, request_id)

    async def _execute_external_tool(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                   params: Dict[str, Any], step: int, request_id: str | None = None) -> tuple[ChatMessage, List[Dict], List[Dict]]:
        """Execute an external MCP tool."""
        server_name, actual_tool_name = tool_name.split(".", 1)

        # Create serializable params for events (exclude non-JSON-serializable objects like StatusScope)
        serializable_params = self._make_params_serializable(params)

        # Emit MCP call event (include request_id for correlation)
        # Prefer tool-specific request_id from params over the general request_id
        event_request_id = serializable_params.get('request_id') or request_id
        call_event = {"type": "mcp_call", "step": step + 1, "server": tool_name, "action": actual_tool_name, "params": serializable_params, "request_id": event_request_id}
        events = [call_event]
        results = []

        try:
            logger.info("Invoking external tool %s on server %s with params %s", actual_tool_name, server_name, params)
            mcp_integration = get_mcp_integration()
            tool_result = await mcp_integration.call_tool(server_name, actual_tool_name, params, "external")
            logger.info("External tool %s returned: %s", tool_name, str(tool_result)[:500])

            results.append({
                "server": tool_name,
                "action": actual_tool_name,
                "params": serializable_params,
                "result": tool_result
            })

            # Emit MCP result event (include request_id for correlation)
            result_event = {"type": "mcp_result", "step": step + 1, "server": tool_name, "action": actual_tool_name, "result": tool_result, "request_id": event_request_id}
            events.append(result_event)

            # Create tool result message
            tool_call_id = tc.get("id") or f"{tool_name}-call-{int(time.time()*1000)}"
            tool_msg_content = json.dumps(tool_result, ensure_ascii=False)
            # Sanitize tool result content before adding to messages
            tool_msg_content = sanitize_json_content(tool_msg_content)
            message = ChatMessage(
                role="tool",
                tool_call_id=tool_call_id,
                name=sanitize_for_llm(openai_tool_name),
                content=tool_msg_content
            )
            return message, events, results
        except Exception as e:
            logger.exception("External tool %s invocation failed: %s", tool_name, e)
            tool_call_id = tc.get("id") or f"{tool_name}-error-{int(time.time()*1000)}"
            error_content = json.dumps({"error": f"Tool invocation failed: {str(e)}"})
            message = ChatMessage(
                role="tool",
                tool_call_id=tool_call_id,
                name=sanitize_for_llm(openai_tool_name),
                content=sanitize_json_content(error_content)
            )
            return message, events, results

    async def _execute_plugin_tool(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                 params: Dict[str, Any], step: int, request_id: str | None = None) -> tuple[ChatMessage, List[Dict], List[Dict]]:
        """Execute a plugin tool."""
        server = self.registry.get(tool_name)
        action_name = params.get("action") or params.get("tool") or server.get_default_action()

        # Validate action against server schema
        schema = server.get_schema()
        valid_actions = []
        if "function" in schema and "parameters" in schema["function"]:
            action_prop = schema["function"]["parameters"].get("properties", {}).get("action", {})
            valid_actions = action_prop.get("enum", [])

        if valid_actions and action_name not in valid_actions:
            logger.warning("Invalid action '%s' for tool %s, valid actions: %s. Using default action.",
                         action_name, tool_name, valid_actions)
            action_name = server.get_default_action()
            params["action"] = action_name

        # Create serializable params for events (exclude non-JSON-serializable objects like StatusScope)
        serializable_params = self._make_params_serializable(params)

        # Emit MCP call event (include request_id for correlation)
        # Prefer tool-specific request_id from params over the general request_id
        event_request_id = serializable_params.get('request_id') or request_id
        call_event = {"type": "mcp_call", "step": step + 1, "server": tool_name, "action": action_name, "params": serializable_params, "request_id": event_request_id}
        events = [call_event]
        results = []

        try:
            logger.info("Invoking tool %s action %s with params %s", tool_name, action_name, params)
            tool_result = await self._invoke_tool(tool_name, params)
            logger.info("Tool %s returned: %s", tool_name, str(tool_result)[:500])

            results.append({
                "server": tool_name,
                "action": action_name,
                "params": serializable_params,
                "result": tool_result
            })

            # Emit MCP result event (include request_id for correlation)
            result_event = {"type": "mcp_result", "step": step + 1, "server": tool_name, "action": action_name, "result": tool_result, "request_id": event_request_id}
            events.append(result_event)

            # Create tool result message
            tool_call_id = tc.get("id") or f"{tool_name}-call-{int(time.time()*1000)}"
            tool_msg_content = json.dumps(tool_result, ensure_ascii=False)
            # Sanitize tool result content before adding to messages
            tool_msg_content = sanitize_json_content(tool_msg_content)
            message = ChatMessage(
                role="tool",
                tool_call_id=tool_call_id,
                name=openai_tool_name,
                content=tool_msg_content
            )
            return message, events, results

        except Exception as e:
            logger.exception("Tool %s invocation failed: %s", tool_name, e)
            # Add error result for this specific tool call
            tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
            error_content = json.dumps({"error": sanitize_for_llm(str(e))})
            message = ChatMessage(
                role="tool",
                tool_call_id=tool_call_id,
                name=sanitize_for_llm(openai_tool_name),
                content=sanitize_json_content(error_content)
            )
            return message, events, results