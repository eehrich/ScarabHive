"""
Tool Execution Manager for Agent Server
Handles execution of both internal plugin tools and external MCP tools.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
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

    def __init__(self, registry: MCPRegistry, agent: Optional[Agent] = None):
        self.registry = registry  # Legacy registry (empty for now)
        # Optional Agent instance for centralized counters and MCP integration access
        self._agent = agent
        # Current session ID and user ID for tool execution context
        self._current_session_id: Optional[str] = None
        self._current_user_id: Optional[str] = None

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
        """Execute a tool call against the registry and return results.

        Modern interface: tool_name IS the function/method to call.
        No separate action_name needed - the tool name identifies the exact operation.
        """

        # First, try to get plugin adapter (important for status forwarding)
        plugin_adapter = None
        if self._agent and hasattr(self._agent, '_mcp_integration_manager'):
            mcp_integration = self._agent._mcp_integration_manager.mcp_integration
            if mcp_integration is not None:  # type: ignore[unreachable]
                if mcp_integration.initialized:  # type: ignore[unreachable]
                    plugin_adapter = mcp_integration.plugin_registry.get_server(tool_name)

        if plugin_adapter:
            # Use the PluginMCPAdapter which handles tool routing and status forwarding correctly
            try:
                # Call through the PluginMCPAdapter with the tool name directly
                result = await plugin_adapter.call_tool(tool_name, params)
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

        try:
            # Check if server has call_with_status (MCP server interface)
            if hasattr(server, 'call_with_status'):
                result = await server.call_with_status(tool_name, params)
            else:
                # Fallback to regular call method
                result = await server.call(tool_name, params)
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
        request_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        status_forwarder: Optional[StatusEventForwarder] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Execute tools in parallel while streaming status events in real-time.

        This async generator allows status events from sub-agents to be streamed
        to the client while tools are still executing, instead of buffering them
        until all tools complete.

        Args:
            session_id: Agent session ID to inject into tool params for session-aware tools
            user_id: User ID to inject into tool params for multi-user isolation
            status_forwarder: Per-request status forwarder for streaming events (MUST be passed per-request
                             to avoid race conditions with concurrent requests)

        Yields:
            Dict with either:
            - {"type": "status", "event": {...}} - Status event to forward
            - {"type": "tool_events", "events": [...]} - Tool execution events
            - {"type": "complete", "messages": [...], "results": [...]} - Final results
        """
        # Store session_id and user_id for use in tool execution
        self._current_session_id = session_id
        self._current_user_id = user_id

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
                    content=json.dumps({"error": f"Tool '{tool_name}' is not available."}),
                    timestamp=datetime.now(timezone.utc)
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
            # NOTE: No hard iteration limit - tools can run as long as needed
            # (e.g., sub_agent_manager may run for hours)
            # Tools are cancelled via cancellation_token if request is cancelled by user
            pending = set(tasks)
            
            while pending:
                # Wait for any task completion or timeout (50ms polling interval)
                done, pending = await asyncio.wait(pending, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)

                # Yield any pending status events from sub-agents
                # CRITICAL: Use the passed status_forwarder parameter, NOT self._status_forwarder
                # to avoid race conditions when multiple requests share the same agent instance
                if status_forwarder:
                    status_events = status_forwarder.get_pending_events()
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
                    except asyncio.CancelledError:
                        logger.debug("Tool task was cancelled")
                        # Task was cancelled, this is expected during request cancellation
                    except Exception as e:
                        logger.exception("Error processing tool result: %s", e)

            # Drain any remaining status events after all tools complete
            # This ensures .end() events are not lost due to timing issues
            if status_forwarder:
                # Use drain to ensure all events are consumed
                # Reduced timeout for faster response
                drained_events = await status_forwarder.drain_pending_events(max_wait_ms=30)
                for status_event in drained_events:
                    yield {"type": "status", "event": status_event}

            # Final check for any remaining status events
            if status_forwarder:
                status_events = status_forwarder.get_pending_events()
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
            # Legacy execution without cancellation - wrap in try/except for robustness
            try:
                if "." in tool_name:
                    return await self._execute_external_tool(tc, tool_name, openai_tool_name, params, step, request_id)
                else:
                    return await self._execute_plugin_tool(tc, tool_name, openai_tool_name, params, step, request_id)
            except Exception as e:
                # Handle unknown tool errors gracefully - return error message instead of crashing
                logger.exception("Tool %s execution failed (legacy path): %s", tool_name, e)
                tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                error_content = json.dumps({
                    "error": f"Tool '{tool_name}' execution failed: {str(e)}",
                    "type": type(e).__name__
                })
                message = ChatMessage(
                    role="tool",
                    tool_call_id=tool_call_id,
                    name=sanitize_for_llm(openai_tool_name),
                    content=sanitize_json_content(error_content),
                    timestamp=datetime.now(timezone.utc)
                )
                return message, [{"type": "tool_error", "tool": tool_name, "error": str(e)}], []

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

        # Get tool cleanup timeout from agent config (fallback to 30.0 for backward compatibility)
        cleanup_timeout = 30.0
        if self._agent and self._agent.agent_config and self._agent.agent_config.timeouts:
            cleanup_timeout = self._agent.agent_config.timeouts.tool_cleanup_timeout

        # Create cancellation context with tool-specific ID
        async with cancellable_operation(tool_request_id, cleanup_timeout=cleanup_timeout) as tool_token:
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
                except Exception as e:
                    # Tool execution failed with exception - CRITICAL: Must return error response to avoid orphaned tool_calls
                    logger.exception("Tool %s execution failed (request_id: %s): %s", tool_name, request_id, e)
                    tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                    error_content = json.dumps({
                        "error": f"Tool '{tool_name}' execution failed: {str(e)}",
                        "type": type(e).__name__
                    })
                    message = ChatMessage(
                        role="tool",
                        tool_call_id=tool_call_id,
                        name=sanitize_for_llm(openai_tool_name),
                        content=sanitize_json_content(error_content),
                        timestamp=datetime.now(timezone.utc)
                    )
                    return message, [{"type": "tool_error", "tool": tool_name, "error": str(e), "request_id": request_id}], []

            except CancellationError as e:
                # Tool gracefully cancelled itself
                logger.info("Tool %s gracefully cancelled (request_id: %s)", tool_name, request_id)
                return self._create_cancelled_response(tc, tool_name, openai_tool_name, request_id, forced=e.forced)

    def _create_cancelled_response(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                 request_id: str, forced: bool = False) -> tuple[ChatMessage, List[Dict], List[Dict]]:
        """Create a cancelled tool response."""
        tool_call_id = tc.get("id") or f"cancelled-call-{int(time.time()*1000)}"
        cancel_type = "force-cancelled" if forced else "cancelled"
        cancel_content = json.dumps({
            "error": f"Tool '{tool_name}' was {cancel_type}.",
            "cancelled": True,
            "forced": forced
        })

        message = ChatMessage(
            role="tool",
            tool_call_id=tool_call_id,
            name=sanitize_for_llm(openai_tool_name),
            content=sanitize_json_content(cancel_content),
            timestamp=datetime.now(timezone.utc)
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
                content=tool_msg_content,
                timestamp=datetime.now(timezone.utc)
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
                content=sanitize_json_content(error_content),
                timestamp=datetime.now(timezone.utc)
            )
            return message, events, results

    async def _execute_plugin_tool(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                 params: Dict[str, Any], step: int, request_id: str | None = None) -> tuple[ChatMessage, List[Dict], List[Dict]]:
        """Execute a plugin tool (or config agent tool)."""
        # CRITICAL FIX: Check if tool_name exists as a registered server FIRST
        # This prevents prefix-based false positives where "sysadmin_agent_manager"
        # incorrectly matches "sysadmin_agent_" prefix check
        server = None

        # Try to get server from registries first
        if self._agent and hasattr(self._agent, '_get_server_from_any_registry'):
            server = self._agent._get_server_from_any_registry(tool_name)

        # If not found in registry, check if it's an own tool using prefix check
        if not server and self._agent and tool_name.startswith(f"{self._agent.name}_"):
            # This is likely an own tool - use the agent itself as the server
            server = self._agent
            logger.debug(f"Tool '{tool_name}' is agent's own tool (prefix match), using self as server")

        # Legacy fallback paths (for systems not using _get_server_from_any_registry)
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

        serializable_params = self._make_params_serializable(params)
        event_request_id = serializable_params.get('request_id') or request_id
        call_event = {"type": "mcp_call", "step": step + 1, "server": tool_name, "action": openai_tool_name, "params": serializable_params, "request_id": event_request_id}
        events = [call_event]
        results = []

        try:
            logger.info("Invoking tool %s with params %s", openai_tool_name, params)

            # Inject session context from current execution context if available
            if self._current_session_id or self._current_user_id or request_id or (self._agent and hasattr(self._agent, 'name')) or (self._agent and hasattr(self._agent, 'registry')):
                params = params.copy()  # Don't mutate original

                if self._current_session_id:
                    params["_session_id"] = self._current_session_id
                    logger.debug(f"✓ Injected session_id '{self._current_session_id}' into tool params")

                if self._current_user_id:
                    params["_user_id"] = self._current_user_id
                    logger.debug(f"✓ Injected user_id '{self._current_user_id}' into tool params")

                if request_id:
                    params["_request_id"] = request_id
                    logger.debug(f"✓ Injected request_id '{request_id}' into tool params")

                if self._agent and hasattr(self._agent, 'name'):
                    params["_agent_name"] = self._agent.name
                    logger.debug(f"✓ Injected agent_name '{self._agent.name}' into tool params")

                # Inject the agent instance itself for tools that need it
                # Tools can access agent._session_service, agent.registry, etc.
                if self._agent:
                    params["_agent"] = self._agent
                    logger.debug("✓ Injected agent instance into tool params")

            if hasattr(server, 'call_with_status'):
                tool_result = await server.call_with_status(openai_tool_name, params)
            else:
                tool_result = await server.call(openai_tool_name, params)

            logger.info("Tool %s returned: %s", tool_name, str(tool_result)[:500])

            # Extract multimodal content from tool result (if present)
            # Tools can return _multimodal_content: [{type, path, mime_type, description}]
            multimodal_content = None
            if isinstance(tool_result, dict):
                raw_multimodal = tool_result.pop("_multimodal_content", None)
                if raw_multimodal:
                    from ....llm.models import MultimodalToolContent
                    # Convert to Pydantic models
                    multimodal_content = []
                    items = raw_multimodal if isinstance(raw_multimodal, list) else [raw_multimodal]
                    for item in items:
                        if isinstance(item, dict):
                            multimodal_content.append(MultimodalToolContent(**item))
                        elif isinstance(item, MultimodalToolContent):
                            multimodal_content.append(item)
                    if multimodal_content:
                        logger.debug(
                            "Extracted %d multimodal items from tool %s",
                            len(multimodal_content), tool_name
                        )

            results.append({
                "server": tool_name,
                "action": openai_tool_name,
                "params": serializable_params,
                "result": tool_result
            })

            result_event = {"type": "mcp_result", "step": step + 1, "server": tool_name, "action": openai_tool_name, "result": tool_result, "request_id": event_request_id}
            events.append(result_event)

            # Create tool result message with optional multimodal content
            tool_call_id = tc.get("id") or f"{tool_name}-call-{int(time.time()*1000)}"
            tool_msg_content = sanitize_json_content(json.dumps(tool_result, ensure_ascii=False))
            message = ChatMessage(
                role="tool",
                tool_call_id=tool_call_id,
                name=openai_tool_name,
                content=tool_msg_content,
                timestamp=datetime.now(timezone.utc),
                multimodal_content=multimodal_content  # Attach multimodal content
            )
            return message, events, results

        except (Exception, GeneratorExit) as e:
            if isinstance(e, GeneratorExit):
                logger.warning("Tool %s closed with GeneratorExit (request_id: %s)", tool_name, request_id)
                tool_call_id = tc.get("id") or f"{tool_name}-cancelled-{int(time.time()*1000)}"
                error_content = json.dumps({"error": "Tool execution was cancelled (GeneratorExit)"})
            else:
                logger.exception("Tool %s invocation failed: %s", tool_name, e)
                tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                error_content = json.dumps({"error": sanitize_for_llm(str(e))})

            message = ChatMessage(
                role="tool",
                tool_call_id=tool_call_id,
                name=sanitize_for_llm(openai_tool_name),
                content=sanitize_json_content(error_content),
                timestamp=datetime.now(timezone.utc)
            )
            return message, events, results