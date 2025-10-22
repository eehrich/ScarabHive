"""
Tool Execution Manager for Agent Server
Handles execution of both internal plugin tools and external MCP tools.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, List, Any, Optional, TYPE_CHECKING, AsyncGenerator

if TYPE_CHECKING:
    from ..server import Agent
    from ....mcp.base import MCPRegistry
    from .status_forwarding import StatusEventForwarder

from ....core.cancellation import get_cancellation_manager, cancellable_operation, CancellationError
from ....llm.models import ChatMessage
from ....llm.text_sanitizer import sanitize_for_llm, sanitize_json_content
from ....mcp.integration import get_mcp_integration

logger = logging.getLogger(__name__)


class ToolExecutionManager:
    """Manages execution of tools and handles results."""

    def __init__(self, registry: MCPRegistry, agent: Optional[Agent] = None, 
                 status_forwarder: Optional[StatusEventForwarder] = None):
        self.registry = registry  # Legacy registry (empty for now)
        # Optional Agent instance for centralized counters and MCP integration access
        self._agent = agent
        # Optional StatusEventForwarder for real-time status streaming during tool execution
        self._status_forwarder = status_forwarder

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

    async def _invoke_tool(self, tool_name: str, params: Dict[str, Any], action_name: Optional[str] = None):
        """Execute a tool call against the registry and return results."""

        # First, try to get plugin adapter (important for status forwarding)
        plugin_adapter = None
        if self._agent and hasattr(self._agent, '_mcp_integration_manager'):
            mcp_integration = self._agent._mcp_integration_manager.mcp_integration
            if mcp_integration is not None:  # type: ignore[unreachable]
                if mcp_integration.initialized:  # type: ignore[unreachable]
                    plugin_adapter = mcp_integration.plugin_registry.get_server(tool_name)

        if plugin_adapter:
            # Use the PluginMCPAdapter which handles tool routing and status forwarding correctly
            if not action_name:  # type: ignore[unreachable]
                action_name = params.get("action") or plugin_adapter.plugin_server.get_default_action()
            try:
                # Call through the PluginMCPAdapter which will route to the correct tool
                result = await plugin_adapter.call_tool(action_name, params)
                return result
            except Exception as e:
                logger.exception("Plugin tool %s invocation failed: %s", tool_name, e)
                raise
        
        # If no plugin adapter, try to get server directly (for config agents)
        server = None
        if self._agent and hasattr(self._agent, '_get_server_from_any_registry'):
            server = self._agent._get_server_from_any_registry(tool_name)
        
        # Final fallback to legacy registry (though it's usually empty)
        if not server:
            server = self.registry.get(tool_name) if tool_name in self.registry.list() else None

        if not server:
            raise RuntimeError(f"Unknown tool: {tool_name}")

        if not action_name:
            action_name = params.get("action") or server.get_default_action()
        
        try:
            # Check if server has call_with_status (MCP server interface)
            if hasattr(server, 'call_with_status'):
                result = await server.call_with_status(action_name, params)
            else:
                # Fallback to regular call method
                result = await server.call(action_name, params)
            return result
        except Exception as e:
            logger.exception("Tool %s invocation failed: %s", tool_name, e)
            raise

    async def execute_tools(self, tool_calls: List[Dict], tool_name_mapping: Dict[str, str],
                          available_tools: List[str], step: int, request_id: str | None = None) -> tuple[List[ChatMessage], List[Dict], List[Dict]]:
        """Execute all tool calls and return tool result messages, events, and results.
        
        This is a convenience wrapper around execute_tools_streaming() for backward compatibility
        and testing. It collects all streaming results and returns them as a tuple.
        
        For production use with real-time status streaming, use execute_tools_streaming() directly.

        Returns:
            Tuple of (tool_messages, events_to_yield, results_to_add)
        """
        tool_messages = []
        events_to_yield = []
        results_to_add: List[Dict] = []
        
        # Collect all results from the streaming version
        async for item in self.execute_tools_streaming(tool_calls, tool_name_mapping, available_tools, step, request_id):
            if item["type"] == "status":
                # Status events are handled by streaming version, ignored here
                pass
            elif item["type"] == "tool_events":
                events_to_yield.extend(item["events"])
            elif item["type"] == "complete":
                tool_messages = item["messages"]
                results_to_add = item["results"]

        return tool_messages, events_to_yield, results_to_add

    async def execute_tools_streaming(
        self,
        tool_calls: List[Dict],
        tool_name_mapping: Dict[str, str],
        available_tools: List[str],
        step: int,
        request_id: str | None = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Execute tools in parallel while streaming status events in real-time.
        
        This async generator allows status events from sub-agents to be streamed
        to the client while tools are still executing, instead of buffering them
        until all tools complete.
        
        Yields:
            Dict with either:
            - {"type": "status", "event": {...}} - Status event to forward
            - {"type": "tool_events", "events": [...]} - Tool execution events  
            - {"type": "complete", "messages": [...], "results": [...]} - Final results
        """
        tool_messages = []
        events_to_yield = []
        results_to_add: List[Dict] = []
        
        # Prepare tool executions (same as execute_tools())
        valid_tool_executions = []

        for i, tc in enumerate(tool_calls):
            func = tc.get("function", {})
            openai_tool_name = func.get("name")
            raw_args = func.get("arguments")

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
                tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                tool_messages.append(ChatMessage(
                    role="tool",
                    tool_call_id=tool_call_id,
                    name=openai_tool_name or "unknown",
                    content=json.dumps({"error": f"Tool '{tool_name}' is not available."})
                ))
                continue

            valid_tool_executions.append((tc, tool_name, openai_tool_name, params))

        # Execute all valid tools in parallel with real-time status streaming
        if valid_tool_executions:
            # Create tasks for parallel execution with unique request_id suffixes
            tasks = []
            for i, (tc, tool_name, openai_tool_name, params) in enumerate(valid_tool_executions):
                # Create tool-specific request_id (same logic as execute_tools)
                original_request_id = params.get("request_id") or params.get("requestId") or request_id
                if original_request_id:
                    if self._agent is not None:
                        try:
                            tool_specific_request_id = await self._agent.next_internal_tool_request_id(original_request_id)
                        except Exception:
                            tool_specific_request_id = f"{original_request_id}_{i+1:03d}"
                    else:
                        tool_specific_request_id = f"{original_request_id}_{i+1:03d}"

                    params_with_suffix = params.copy()
                    params_with_suffix["request_id"] = tool_specific_request_id
                    params_with_suffix["requestId"] = tool_specific_request_id
                else:
                    params_with_suffix = params
                    tool_specific_request_id = None

                task = asyncio.create_task(
                    self._execute_single_tool(tc, tool_name, openai_tool_name, params_with_suffix, step, tool_specific_request_id)
                )
                tasks.append(task)

            # Poll for completion while streaming status events
            pending = set(tasks)
            while pending:
                # Wait for any task completion or timeout (50ms polling interval)
                done, pending = await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                
                # Yield any pending status events from sub-agents
                if self._status_forwarder:
                    status_events = self._status_forwarder.get_pending_events()
                    for status_event in status_events:
                        yield {"type": "status", "event": status_event}
                
                # Process completed tasks
                for task in done:
                    try:
                        result = task.result()
                        if isinstance(result, BaseException):
                            # Handle error (same as execute_tools)
                            logger.exception("Tool execution failed: %s", result)
                        else:
                            tool_message, events, tool_results = result
                            tool_messages.append(tool_message)
                            events_to_yield.extend(events)
                            results_to_add.extend(tool_results)
                    except Exception as e:
                        logger.exception("Error processing tool result: %s", e)

            # Yield final status events after all tools complete
            if self._status_forwarder:
                status_events = self._status_forwarder.get_pending_events()
                for status_event in status_events:
                    yield {"type": "status", "event": status_event}
        
        # Yield tool execution events
        if events_to_yield:
            yield {"type": "tool_events", "events": events_to_yield}
        
        # Yield final completion with all results
        yield {
            "type": "complete",
            "messages": tool_messages,
            "results": results_to_add
        }

    async def _execute_single_tool(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                 params: Dict[str, Any], step: int, request_id: str | None = None) -> tuple[ChatMessage, List[Dict], List[Dict]]:
        """Execute a single tool and return the result message, events, and results."""
        # Use cancellation system if request_id is available
        if request_id:
            return await self._execute_with_cancellation(tc, tool_name, openai_tool_name, params, step, request_id)
        else:
            # Legacy execution without cancellation
            if "." in tool_name:
                return await self._execute_external_tool(tc, tool_name, openai_tool_name, params, step, request_id)
            else:
                return await self._execute_plugin_tool(tc, tool_name, openai_tool_name, params, step, request_id)

    async def _execute_with_cancellation(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                       params: Dict[str, Any], step: int, request_id: str) -> tuple[ChatMessage, List[Dict], List[Dict]]:
        """Execute tool with cancellation support."""
        cancellation_manager = get_cancellation_manager()

        # Check if already cancelled (main request token)
        main_token = cancellation_manager.get_token(request_id)
        if main_token and main_token.is_cancelled:
            logger.info("Tool %s cancelled before execution (main request %s already cancelled)", tool_name, request_id)
            return self._create_cancelled_response(tc, tool_name, openai_tool_name, request_id, forced=main_token.is_forced)
        elif not main_token:
            logger.debug("No main token found for request %s when starting tool %s", request_id, tool_name)

        # Create tool-specific request ID for tool-level cancellation
        tool_request_id = f"{request_id}_{step:03d}"

        # Create cancellation context with tool-specific ID
        async with cancellable_operation(tool_request_id, cleanup_timeout=30.0) as tool_token:
            # If main request is cancelled during tool execution, cancel tool token too
            if main_token and main_token.is_cancelled and not tool_token.is_cancelled:
                tool_token.cancel()

            # Add cancellation token to params for tools that support it
            params_with_token = params.copy()
            params_with_token["_cancellation_token"] = tool_token

            # Execute the tool
            try:
                if "." in tool_name:
                    task = asyncio.create_task(
                        self._execute_external_tool(tc, tool_name, openai_tool_name, params_with_token, step, request_id)
                    )
                else:
                    task = asyncio.create_task(
                        self._execute_plugin_tool(tc, tool_name, openai_tool_name, params_with_token, step, request_id)
                    )

                # Register task for forced cancellation with tool-specific ID
                cancellation_manager.register_task(tool_request_id, task)

                # Wait for completion or cancellation
                try:
                    return await task
                except asyncio.CancelledError:
                    # Task was force-cancelled
                    logger.warning("Tool %s force-cancelled (request_id: %s)", tool_name, request_id)
                    return self._create_cancelled_response(tc, tool_name, openai_tool_name, request_id, forced=True)

            except CancellationError as e:
                # Tool gracefully cancelled itself
                logger.info("Tool %s gracefully cancelled (request_id: %s)", tool_name, request_id)
                return self._create_cancelled_response(tc, tool_name, openai_tool_name, request_id, forced=e.forced)

    def _create_cancelled_response(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                 request_id: str, forced: bool = False) -> tuple[ChatMessage, List[Dict], List[Dict]]:
        """Create a cancelled tool response."""
        tool_call_id = tc.get("id") or f"cancelled-call-{int(time.time()*1000)}"
        cancel_type = "force-cancelled" if forced else "cancelled"
        error_content = json.dumps({
            "error": f"Tool '{tool_name}' was {cancel_type}.",
            "cancelled": True,
            "forced": forced
        })

        message = ChatMessage(
            role="tool",
            tool_call_id=tool_call_id,
            name=sanitize_for_llm(openai_tool_name),
            content=sanitize_json_content(error_content)
        )

        event_type = "tool_force_cancelled" if forced else "tool_cancelled"
        return message, [{"type": event_type, "tool": tool_name, "request_id": request_id}], []

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
            # Get MCP integration with agent config if available
            if self._agent and hasattr(self._agent, 'agent_config'):
                mcp_integration = get_mcp_integration(config=self._agent.agent_config)
            else:
                # Fallback - this should not happen in normal operation
                raise RuntimeError("Cannot access MCP integration without agent config")
            # Use serializable_params to avoid passing non-JSON-serializable objects (like CancellationToken) to external servers
            tool_result = await mcp_integration.call_tool(server_name, actual_tool_name, serializable_params, "external")
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
        except (Exception, GeneratorExit) as e:
            # Handle both normal exceptions and GeneratorExit (when async generator tools are closed)
            if isinstance(e, GeneratorExit):
                logger.warning("Tool %s closed with GeneratorExit (request_id: %s)", tool_name, request_id)
                # Treat GeneratorExit as cancellation
                tool_call_id = tc.get("id") or f"{tool_name}-cancelled-{int(time.time()*1000)}"
                error_content = json.dumps({"error": "Tool execution was cancelled (GeneratorExit)"})
            else:
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
        """Execute a plugin tool (or config agent tool)."""
        # CRITICAL FIX: Check if this is the agent's OWN tool (recursive call)
        # Own tools are prefixed with agent name: e.g. "meta_web_research_agent_web_research"
        if self._agent and tool_name.startswith(f"{self._agent.name}_"):
            # This is an own tool - use the agent itself as the server
            server = self._agent
            logger.debug(f"Tool '{tool_name}' is agent's own tool, using self as server")
        else:
            # Use agent's central method to get server from any registry
            server = None
            if self._agent and hasattr(self._agent, '_get_server_from_any_registry'):
                server = self._agent._get_server_from_any_registry(tool_name)
            
            # Fallback to legacy lookup if central method not available
            if not server:
                # Get plugin server from MCP integration plugin registry
                if self._agent and hasattr(self._agent, '_mcp_integration_manager'):
                    mcp_integration = self._agent._mcp_integration_manager.mcp_integration
                    if mcp_integration is not None:  # type: ignore[unreachable]
                        if mcp_integration.initialized:  # type: ignore[unreachable]
                            plugin_adapter = mcp_integration.plugin_registry.get_server(tool_name)
                            if plugin_adapter and hasattr(plugin_adapter, 'plugin_server'):
                                server = plugin_adapter.plugin_server

                if not server:
                    # Fallback to agent's registry (for config agents and other servers)
                    if self._agent and hasattr(self._agent, 'registry'):
                        agent_registry = self._agent.registry
                        if agent_registry and tool_name in agent_registry.list():
                            server = agent_registry.get(tool_name)
                    
                    # Final fallback to legacy registry (though it may be empty)
                    if not server:
                        server = self.registry.get(tool_name) if tool_name in self.registry.list() else None

        if not server:
            raise RuntimeError(f"Server not found for tool: {tool_name}")

        # For multi-tool plugins, the openai_tool_name contains the actual tool name to call
        # The tool_name is the plugin registry name that was mapped back
        # So we should call the server with the original tool name, not an action
        if hasattr(server, 'get_tools'):
            # Multi-tool plugin: call with the specific tool name (regardless of name equality)
            action_name = openai_tool_name
        else:
            # Legacy single-tool plugin: use action parameter
            action_name = params.get("action") or params.get("tool") or server.get_default_action()

            # Validate action against server schema for legacy plugins
            if hasattr(server, 'get_schema'):
                try:
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
                except Exception as e:
                    logger.debug("Could not validate action for tool %s: %s", tool_name, e)

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
            
            # CRITICAL FIX: If we already have the server (e.g., for own tools), call it directly
            # instead of going through _invoke_tool() which tries to look up the server again
            if hasattr(server, 'call_with_status'):
                tool_result = await server.call_with_status(action_name, params)
            else:
                # Fallback to regular call method
                tool_result = await server.call(action_name, params)
                
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

        except (Exception, GeneratorExit) as e:
            # Handle both normal exceptions and GeneratorExit (when async generator tools are closed)
            if isinstance(e, GeneratorExit):
                logger.warning("Tool %s closed with GeneratorExit (request_id: %s)", tool_name, request_id)
                # Treat GeneratorExit as cancellation
                tool_call_id = tc.get("id") or f"{tool_name}-cancelled-{int(time.time()*1000)}"
                error_content = json.dumps({"error": "Tool execution was cancelled (GeneratorExit)"})
            else:
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