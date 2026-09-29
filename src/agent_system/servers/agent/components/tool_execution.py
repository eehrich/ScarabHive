"""
Tool Execution Manager for Agent Server
Handles execution of both internal plugin tools and external tools.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, TYPE_CHECKING, AsyncGenerator, Tuple

if TYPE_CHECKING:
    from ..server import Agent
    from ....tools.base import ToolServerRegistry
    from .hook_integration import HookIntegrationManager
    from .status_forwarding import StatusEventForwarder

from ....core.cancellation import get_cancellation_manager, cancellable_operation, CancellationError
from ....core.request_context import register_request_user
from ....hooks.plugin_hook import HookType
from .server_resolution import resolve_longest_prefix
from ....llm.caller_llm import context_for_tool
from ....llm.models import ChatMessage
from ....llm.text_sanitizer import sanitize_for_llm, sanitize_json_content
from ....tools.integration import get_tool_integration
from ....utils.json_utils import parse_tool_arguments

logger = logging.getLogger(__name__)

# Per-tool request ids the framework sets on every call (see execute_tools_streaming).
FRAMEWORK_REQUEST_ID_KEYS = frozenset({"request_id", "requestId"})

#: "type" of the result the model reads for a call a pre_tool_call hook blocked.
BLOCKED_CALL_TYPE = "ToolCallBlocked"


class ToolDispatchError(Exception):
    """Programmatic tool dispatch failed (unknown tool, not allowed, unsupported
    tool type, blocked by a pre_tool_call hook). The message is agent-actionable
    — callers (e.g. the tool_script plugin) surface it verbatim to the LLM."""


def drop_runtime_params(params: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """``params`` without the keys the framework owns, and the keys it dropped.

    Runtime params (``_session_id``, ``_agent``, ... -- see inject_runtime_params)
    identify the CALLER, and request_id/requestId route status and cancellation.
    Whatever hands in arguments -- the model, a script, a pre_tool_call hook --
    must not be able to supply them: a forged ``_session_id`` survives injection
    whenever the run has none (injection only overwrites truthy values) and lets
    a tool impersonate another agent.
    """
    dropped = [k for k in params if str(k).startswith("_") or k in FRAMEWORK_REQUEST_ID_KEYS]
    if not dropped:
        return params, []
    return {k: v for k, v in params.items() if k not in dropped}, dropped


def tool_result_is_error(result: Any) -> bool:
    """Whether a tool result reports a failure, in either shape tools use:
    ``{"status": "error", ...}`` or a bare ``{"error": ...}`` without a status."""
    if not isinstance(result, dict):
        return False
    if result.get("status") == "error":
        return True
    return "status" not in result and bool(result.get("error"))


def tool_message_was_blocked(message: Any) -> bool:
    """Whether a tool-result message stands for a call a pre_tool_call hook
    blocked -- the call never ran."""
    content = getattr(message, "content", None)
    if not isinstance(content, str) or BLOCKED_CALL_TYPE not in content:
        return False
    try:
        data = json.loads(content)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("type") == BLOCKED_CALL_TYPE


def inject_runtime_params(params: Dict[str, Any], *,
                          session_id: Optional[str] = None,
                          user_id: Optional[str] = None,
                          request_id: Optional[str] = None,
                          agent: Optional["Agent"] = None) -> Dict[str, Any]:
    """Return a copy of ``params`` with the runtime context params injected.

    THE single place that defines which runtime params a tool call receives
    (_session_id, _user_id, _request_id, _agent_name, _agent). Used by the LLM
    tool path (_execute_plugin_tool) and by programmatic dispatch
    (Agent.dispatch_tool_call, e.g. tool-scripting) so the two can never drift.

    Injection happens unconditionally for present values and OVERWRITES any
    caller-supplied keys of the same name — callers outside the trusted path
    (e.g. script-provided params) must not be able to forge runtime context.
    """
    params = params.copy()

    if session_id:
        params["_session_id"] = session_id

    if user_id:
        params["_user_id"] = user_id
        # Register user_id for this request_id so sub-agents can find it:
        # when a tool spawns a sub-agent, the sub-agent generates a new
        # session and needs to know the user_id.
        if request_id:
            register_request_user(request_id, user_id)

    if request_id:
        params["_request_id"] = request_id

    if agent is not None and hasattr(agent, 'name'):
        params["_agent_name"] = agent.name

    # Inject the agent instance itself for tools that need it
    # (session service, registry access, programmatic dispatch, ...)
    if agent is not None:
        params["_agent"] = agent

    return params


class ToolExecutionManager:
    """Manages execution of tools and handles results."""

    def __init__(self, registry: ToolServerRegistry, agent: Optional[Agent] = None,
                 hook_manager: Optional[HookIntegrationManager] = None):
        self.registry = registry  # Legacy fallback; the shared one since bootstrap passes it in
        # Optional Agent instance for centralized counters and tool integration access
        self._agent = agent
        # The agent's hooks: pre_tool_call / post_tool_call fire around every
        # call of the model when one is given (the Agent passes its own).
        self._hook_manager = hook_manager
        # NOTE: session_id/user_id are deliberately NOT instance state — they are
        # passed through the call chain per request (see execute_tools_streaming)
        # to avoid races when concurrent requests share this manager.

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
        if self._agent and hasattr(self._agent, '_tool_integration_manager'):
            tool_integration = self._agent._tool_integration_manager.tool_integration
            if tool_integration is not None:  # type: ignore[unreachable]
                if tool_integration.initialized:  # type: ignore[unreachable]
                    # First try exact match (legacy behavior)
                    plugin_adapter = tool_integration.plugin_registry.get_server(tool_name)

                    # If not found, resolve the server name embedded in the flat
                    # tool name (servername_toolname) via the shared prefix walk.
                    if not plugin_adapter:
                        plugin_adapter, adapter_server_name = resolve_longest_prefix(
                            tool_integration.plugin_registry.get_server, tool_name)
                        if plugin_adapter:
                            logger.debug(f"Found plugin adapter for {tool_name} via server name {adapter_server_name}")

        if plugin_adapter:
            # Use the PluginToolAdapter which handles tool routing and status forwarding correctly
            try:
                # Call through the PluginToolAdapter with the tool name directly
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
            # Check if server has call_with_status (tool server interface)
            if hasattr(server, 'call_with_status'):
                result = await server.call_with_status(tool_name, params)
            else:
                # Fallback to regular call method
                result = await server.call(tool_name, params)
            return result
        except Exception as e:
            logger.exception("Tool %s invocation failed: %s", tool_name, e)
            raise

    # NOTE: the former execute_tools() convenience wrapper (collect-to-tuple)
    # was test-only production code and was removed (Review G5). Production
    # consumes the streaming generator below directly; tests use a shared
    # collector helper.

    async def execute_tools_streaming(
        self,
        tool_calls: List[Dict],
        tool_name_mapping: Dict[str, str],
        available_tools: List[str],
        step: int,
        request_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        status_forwarder: Optional[StatusEventForwarder] = None,
        assistant_message: Optional[ChatMessage] = None,
        llm_profile: Optional[str] = None,
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
            assistant_message: The message whose tool calls these are: it is stamped with the
                             id each tool runs under (ChatMessage.tool_request_ids)
            llm_profile: The profile the run was switched to, None when it runs its own
                             configuration. Each tool call runs in a context holding it, so a
                             sub-agent the tool starts can follow it (llm/caller_llm.py).

        Yields:
            Dict with either:
            - {"type": "status", "event": {...}} - Status event to forward
            - {"type": "tool_events", "events": [...]} - Tool execution events
            - {"type": "complete", "messages": [...], "results": [...]} - Final results
        """
        # NOTE: session_id and user_id are passed as parameters through the call chain
        # to avoid race conditions when multiple requests share the same ToolExecutionManager.
        # DO NOT store them as instance variables (self._current_session_id/user_id)!

        tool_messages = []
        events_to_yield = []
        results_to_add: List[Dict] = []
        # (position among the model's calls, message, events, results). Every
        # answer is sorted into the order of the calls before it joins the
        # history -- the rejected ones below as well.
        # IMPORTANT: Gemini API requires function_response parts to be in the same
        # order as the original function_call parts to avoid MALFORMED_FUNCTION_CALL errors
        indexed_results: List[tuple[int, ChatMessage, List[Dict], List[Dict]]] = []

        # Prepare tool executions (same as execute_tools())
        valid_tool_executions = []
        positions: List[int] = []  # valid call index -> position among tool_calls

        for pos, tc in enumerate(tool_calls):
            func = tc.get("function", {})
            openai_tool_name = func.get("name")
            raw_args = func.get("arguments")

            tool_name = tool_name_mapping.get(openai_tool_name, openai_tool_name)

            # Parse arguments -- the reading history stores as well
            # (utils.json_utils.parse_tool_arguments). Malformed arguments are
            # rejected, not repaired, and the model sends the call again.
            params: Dict[str, Any] = {}
            parse_problem = ""
            if isinstance(raw_args, str) and raw_args:
                parsed, parse_problem = parse_tool_arguments(raw_args)
                if parsed is None:
                    logger.warning(
                        "Rejected tool call %s: %s (length %d)",
                        tool_name, parse_problem, len(raw_args),
                    )
                else:
                    params = parsed
            elif isinstance(raw_args, dict):
                params = raw_args
            json_parse_failed = bool(parse_problem)

            # SECURITY: runtime params (_session_id, _agent, _request_id, ...) are
            # injected by the framework and identify the CALLER. An LLM must never
            # be able to supply them — a forged _session_id would let a tool
            # impersonate another agent (e.g. defeat json_store's owner-based write
            # protection whenever no session id is set, since injection only
            # overwrites truthy values). Strip them from model-supplied arguments.
            # request_id/requestId are framework-owned too: a model-supplied one
            # would become the base of the tool's request id, so cancelling the
            # real request would miss the tool and its status would be routed
            # under the model's value.
            if not json_parse_failed and isinstance(params, dict):
                params, forged = drop_runtime_params(params)
                if forged:
                    logger.warning(
                        "Dropping model-supplied runtime param(s) %s from tool call %s",
                        forged, tool_name,
                    )

            # If JSON parsing failed, return an error to the LLM so it can retry
            if json_parse_failed:
                tool_call_id = tc.get("id") or f"parse-error-{int(time.time()*1000)}"
                error_content = json.dumps({
                    "error": (
                        f"Invalid tool arguments for '{tool_name}': {parse_problem}. "
                        "The call was NOT executed. Send it again with the arguments "
                        "as one valid JSON object. Common causes: an unescaped double "
                        "quote inside a string value, or output cut off before the "
                        "JSON ended."
                    ),
                    "type": "JSONParseError",
                })
                events_to_yield.append({
                    "type": "tool_error",
                    "tool": tool_name,
                    "error": f"Invalid JSON arguments for {tool_name}",
                })
                indexed_results.append((pos, ChatMessage(
                    role="tool",
                    tool_call_id=tool_call_id,
                    name=openai_tool_name or "unknown",
                    content=error_content,
                    timestamp=datetime.now(timezone.utc),
                ), [], []))
                continue

            if not tool_name or tool_name not in available_tools:
                logger.warning("Unknown tool requested: %s (OpenAI name: %s)", tool_name, openai_tool_name)
                # Use tool_error type instead of error - error type causes frontend to abort
                events_to_yield.append({"type": "tool_error", "tool": tool_name, "error": f"Unknown tool: {tool_name}"})
                tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                # Return detailed error message so LLM can recover
                error_content = json.dumps({
                    "error": f"Unknown tool: '{tool_name}'. The tool does not exist. Please check available tools and try again.",
                    "type": "ToolNotFoundError"
                })
                indexed_results.append((pos, ChatMessage(
                    role="tool",
                    tool_call_id=tool_call_id,
                    name=openai_tool_name or "unknown",
                    content=error_content,
                    timestamp=datetime.now(timezone.utc)
                ), [], []))
                continue

            valid_tool_executions.append((tc, tool_name, openai_tool_name, params))
            positions.append(pos)

        # pre_tool_call / post_tool_call. Only for an agent some hook of the type
        # would run for: without one no context is built and nothing is copied.
        # Calls the framework already rejected above (malformed arguments,
        # unknown tool) never ran and reach no hook.
        hooks = self._hook_manager
        run_pre_hooks = (bool(valid_tool_executions) and hooks is not None
                         and hooks.wants_hooks(HookType.PRE_TOOL_CALL))
        run_post_hooks = (bool(valid_tool_executions) and hooks is not None
                          and hooks.wants_hooks(HookType.POST_TOOL_CALL))
        hook_token = (get_cancellation_manager().get_token(request_id)
                      if request_id and (run_pre_hooks or run_post_hooks) else None)
        hooked_calls: Dict[int, Dict[str, Any]] = {}  # position -> the call as the hooks see it
        # position -> when the call itself started / ended (time.time()). The
        # post hooks run once every call of the step is done; their own clock
        # says when the step ended, so each call's span is handed to them.
        call_started: Dict[int, float] = {}
        call_finished: Dict[int, float] = {}
        blocked: set[int] = set()  # valid call indices
        if run_pre_hooks or run_post_hooks:
            for i, (tc, tool_name, openai_tool_name, params) in enumerate(valid_tool_executions):
                # tool_name is what the mapping resolved: the server (for an
                # external MCP tool "<server>.<tool>"); the model called the tool.
                call = {"id": tc.get("id"), "name": openai_tool_name, "server": tool_name,
                        "arguments": params, "source": "model"}
                # A cancelled run asks nobody: the call reports itself cancelled
                # below without having started, and no hook -- post included --
                # takes it for one that ran.
                if hook_token is not None and hook_token.is_cancelled:
                    continue
                if run_pre_hooks:
                    # One call after the other, in the order the model sent them,
                    # before any of them starts: a hook that asks a person asks
                    # one question at a time, and blocking one call leaves the
                    # calls around it as they are.
                    outcome: List[Any] = []
                    async with aclosing(self._forwarding_status(
                            self._run_pre_tool_hooks(call, step, request_id, session_id, hook_token),
                            status_forwarder, outcome)) as forwarded:
                        async for item in forwarded:
                            yield item
                    arguments, block = outcome[0]
                    # A run cancelled while its hooks were asked reports the call
                    # cancelled below, not blocked: a hook that stopped waiting on
                    # the cancel (and failed, under on_error: block) checked nothing.
                    if hook_token is not None and hook_token.is_cancelled:
                        continue
                    if block is not None:
                        blocked.add(i)
                        indexed_results.append(
                            (positions[i], self._create_blocked_response(tc, openai_tool_name, block), [], []))
                        events_to_yield.append({"type": "tool_error", "tool": tool_name,
                                                "error": block, "blocked": True})
                        continue
                    call = {**call, "arguments": arguments}
                    valid_tool_executions[i] = (tc, tool_name, openai_tool_name, arguments)
                hooked_calls[positions[i]] = call

        # Execute all valid tools in parallel with real-time status streaming
        if valid_tool_executions:
            # Create tasks for parallel execution with unique request_id suffixes
            # Store task -> tool_info mapping for error handling
            tasks = []
            task_tool_info: Dict[asyncio.Task, tuple] = {}  # task -> (tc, tool_name, openai_tool_name)
            task_indices: Dict[asyncio.Task, int] = {}  # task -> original index (for ordering responses)
            request_ids: Dict[str, str] = {}  # call id -> the id its tool runs under
            for i, (tc, tool_name, openai_tool_name, params) in enumerate(valid_tool_executions):
                if i in blocked:
                    continue
                # Create tool-specific request_id (same logic as execute_tools)
                original_request_id = request_id
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

                call_started[positions[i]] = time.time()
                task = asyncio.create_task(
                    self._execute_single_tool(tc, tool_name, openai_tool_name, params_with_suffix, step, tool_specific_request_id, session_id, user_id,
                                              main_request_id=original_request_id),
                    # Its own context copy, holding the run's profile: set here, it
                    # cannot leak into the run or a sibling call.
                    context=context_for_tool(llm_profile),
                )
                # Stamped by the loop when the task ends, not when this poll
                # gets to it: the poll hands status events on in between, and
                # a slow consumer of the stream would lengthen the call.
                task.add_done_callback(
                    lambda _task, pos=positions[i]: call_finished.setdefault(pos, time.time()))
                tasks.append(task)
                task_tool_info[task] = (tc, tool_name, openai_tool_name)
                task_indices[task] = positions[i]  # Store original position for ordering
                if tc.get("id") and tool_specific_request_id:
                    request_ids[tc["id"]] = tool_specific_request_id
            if assistant_message is not None and request_ids:
                # Before any tool answers: the live list holds this same message, so a
                # viewer who joins while a call still waits finds its runs already.
                assistant_message.tool_request_ids = request_ids

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
                    original_index = task_indices.get(task, 999)  # Default high index if not found
                    try:
                        # task.result() RAISES on failure (asyncio.wait, not
                        # gather(return_exceptions=True)) -- errors land in the
                        # except blocks below, which build the error ChatMessage.
                        tool_message, events, tool_results = task.result()
                        # Store with original index for later sorting
                        indexed_results.append((original_index, tool_message, events, tool_results))
                        events_to_yield.extend(events)
                        results_to_add.extend(tool_results)
                    except asyncio.CancelledError:
                        logger.debug("Tool task was cancelled")
                        # Task was cancelled, this is expected during request cancellation
                        # Create a cancelled response to avoid orphaned tool_calls
                        if task in task_tool_info:
                            tc, tool_name, openai_tool_name = task_tool_info[task]
                            tool_call_id = tc.get("id") or f"cancelled-call-{int(time.time()*1000)}"
                            cancelled_msg = ChatMessage(
                                role="tool",
                                tool_call_id=tool_call_id,
                                name=sanitize_for_llm(openai_tool_name),
                                content=json.dumps({"error": f"Tool '{tool_name}' was cancelled."}),
                                timestamp=datetime.now(timezone.utc)
                            )
                            indexed_results.append((original_index, cancelled_msg, [], []))
                            events_to_yield.append({"type": "tool_cancelled", "tool": tool_name})
                    except Exception as e:
                        # CRITICAL: Create error response to avoid orphaned tool_calls
                        # Without this, the LLM will crash because it expects a tool response for every tool_call
                        logger.exception("Error processing tool result: %s", e)
                        if task in task_tool_info:
                            tc, tool_name, openai_tool_name = task_tool_info[task]
                            tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
                            error_content = json.dumps({
                                "error": f"Tool '{tool_name}' execution failed: {str(e)}",
                                "type": type(e).__name__
                            })
                            error_msg = ChatMessage(
                                role="tool",
                                tool_call_id=tool_call_id,
                                name=sanitize_for_llm(openai_tool_name),
                                content=sanitize_json_content(error_content),
                                timestamp=datetime.now(timezone.utc)
                            )
                            indexed_results.append((original_index, error_msg, [], []))
                            events_to_yield.append({"type": "tool_error", "tool": tool_name, "error": str(e)})

        # Sort results by original position and extract messages
        # CRITICAL: Gemini API requires function_response parts to match the order
        # of the original function_call parts. Without this sorting, parallel tool
        # execution can produce responses in completion order (not call order),
        # causing MALFORMED_FUNCTION_CALL errors.
        indexed_results.sort(key=lambda x: x[0])
        for pos, msg, _, _ in indexed_results:
            if run_post_hooks and pos in hooked_calls:
                # In call order, after all of them are done, and before the
                # result joins the history: nothing already sent changes.
                async with aclosing(self._forwarding_status(
                        self._run_post_tool_hooks(hooked_calls[pos], msg, step, request_id,
                                                  session_id, hook_token,
                                                  started_at=call_started.get(pos),
                                                  finished_at=call_finished.get(pos)),
                        status_forwarder, [])) as forwarded:
                    async for item in forwarded:
                        yield item
            tool_messages.append(msg)

        if valid_tool_executions:
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
                                 params: Dict[str, Any], step: int, request_id: str | None = None,
                                 session_id: str | None = None, user_id: str | None = None,
                                 main_request_id: str | None = None) -> tuple[ChatMessage, List[Dict], List[Dict]]:
        """Execute a single tool and return the result message, events, and results."""
        # Use cancellation system if request_id is available
        if request_id:
            return await self._execute_with_cancellation(tc, tool_name, openai_tool_name, params, step, request_id, session_id, user_id,
                                                         main_request_id=main_request_id)
        else:
            # Legacy execution without cancellation - wrap in try/except for robustness
            try:
                if "." in tool_name:
                    return await self._execute_external_tool(tc, tool_name, openai_tool_name, params, step, request_id, session_id, user_id)
                else:
                    return await self._execute_plugin_tool(tc, tool_name, openai_tool_name, params, step, request_id, session_id, user_id)
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
                                       params: Dict[str, Any], step: int, request_id: str,
                                       session_id: str | None = None, user_id: str | None = None,
                                       main_request_id: str | None = None) -> tuple[ChatMessage, List[Dict], List[Dict]]:
        """Execute tool with cancellation support."""
        cancellation_manager = get_cancellation_manager()

        # Check if already cancelled. The MAIN request token lives under the
        # ROOT request id -- `request_id` here is the per-tool suffix id
        # ('abc123_007'), under which no token exists. Looking it up there
        # made this guard (and the propagation below) dead code: a request
        # cancelled during the LLM call still started every tool of the step.
        main_token = cancellation_manager.get_token(main_request_id or request_id)
        if main_token and main_token.is_cancelled:
            logger.info("Tool %s cancelled before execution (main request %s already cancelled)", tool_name, main_request_id or request_id)
            return self._create_cancelled_response(tc, tool_name, openai_tool_name, request_id, forced=main_token.is_forced)
        elif not main_token:
            logger.debug("No main token found for request %s when starting tool %s", main_request_id or request_id, tool_name)

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
                        self._execute_external_tool(tc, tool_name, openai_tool_name, params_with_token, step, request_id, session_id, user_id)
                    )
                else:
                    task = asyncio.create_task(
                        self._execute_plugin_tool(tc, tool_name, openai_tool_name, params_with_token, step, request_id, session_id, user_id)
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

    @staticmethod
    async def _forwarding_status(awaitable: Any, status_forwarder: Optional[StatusEventForwarder],
                                 outcome: List[Any]) -> AsyncGenerator[Dict[str, Any], None]:
        """Await ``awaitable`` while passing the run's status events on, the way
        the tool poll loop does: a hook that asks a person over the run's
        stream reaches the viewer while it waits, not once it has its answer.
        The result is appended to ``outcome``."""
        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.05)
                if status_forwarder:
                    for status_event in status_forwarder.get_pending_events():
                        yield {"type": "status", "event": status_event}
            outcome.append(task.result())
        finally:
            if not task.done():
                task.cancel()

    async def _run_pre_tool_hooks(self, call: Dict[str, Any], step: int, request_id: str | None,
                                  session_id: str | None, token: Any
                                  ) -> Tuple[Dict[str, Any], Optional[str]]:
        """(the arguments to run the call with, why it was blocked or None).

        A failure of the hook machinery itself blocks the call: it could not be
        asked, and a call a policy hook never saw must not run on that account.
        A single hook that fails is the registry's business -- it is skipped.
        """
        assert self._hook_manager is not None
        try:
            return await self._hook_manager.execute_pre_tool_hooks(
                call, step=step, request_id=request_id or "", session_id=session_id or "",
                cancellation_token=token)
        except Exception as exc:
            logger.exception("pre_tool_call hooks failed for %s; the call does not run", call.get("name"))
            return call["arguments"], (
                f"The call to '{call.get('name')}' did not run: the checks that run before "
                f"every tool call failed ({type(exc).__name__}). Tell the user; do not retry it.")

    async def _run_post_tool_hooks(self, call: Dict[str, Any], message: ChatMessage, step: int,
                                   request_id: str | None, session_id: str | None, token: Any,
                                   started_at: Optional[float] = None,
                                   finished_at: Optional[float] = None) -> None:
        """Hand the result the model is about to read to the post_tool_call
        hooks and put what they return in its place.

        The hooks get the content decoded (every path here writes JSON) and
        what they return is encoded the way the result was.
        """
        assert self._hook_manager is not None
        content = message.content
        value: Any = content
        was_json = False
        if isinstance(content, str):
            try:
                value, was_json = json.loads(content), True
            except ValueError:
                pass
        try:
            new_value = await self._hook_manager.execute_post_tool_hooks(
                call, value, step=step, request_id=request_id or "", session_id=session_id or "",
                cancellation_token=token, started_at=started_at, finished_at=finished_at)
            if new_value == value:
                return
            if isinstance(new_value, str) and not was_json:
                content = new_value
            else:
                # Inside the try: what a hook returns may not encode (a tuple
                # key, a cycle), and the call it rewrites has already run.
                content = json.dumps(new_value, ensure_ascii=False, default=str)
        except Exception:
            logger.exception("post_tool_call hooks failed for %s; the result stays as the tool returned it",
                             call.get("name"))
            return
        message.content = sanitize_json_content(content)

    def _create_blocked_response(self, tc: Dict, openai_tool_name: str, reason: str) -> ChatMessage:
        """The result of a call a pre_tool_call hook blocked: an error the model
        reads in place of the result, in the shape tools report one."""
        return ChatMessage(
            role="tool",
            tool_call_id=tc.get("id") or f"blocked-call-{int(time.time()*1000)}",
            name=sanitize_for_llm(openai_tool_name),
            content=sanitize_json_content(json.dumps(
                {"status": "error", "error": reason, "type": BLOCKED_CALL_TYPE}, ensure_ascii=False)),
            timestamp=datetime.now(timezone.utc),
        )

    async def _execute_external_tool(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                   params: Dict[str, Any], step: int, request_id: str | None = None,
                                   session_id: str | None = None, user_id: str | None = None) -> tuple[ChatMessage, List[Dict], List[Dict]]:
        """Execute an external tool."""
        server_name, actual_tool_name = tool_name.split(".", 1)

        # NOTE: no session-context injection here. External servers are
        # foreign processes -- internal runtime keys (_session_id/_user_id)
        # must not leave the process. The old injection block was dead code
        # anyway: _make_params_serializable strips every "_"-prefixed key
        # before the call, so the values never reached the server.

        # Create serializable params for events (exclude non-JSON-serializable objects like StatusScope)
        serializable_params = self._make_params_serializable(params)

        # Emit tool call event (include request_id for correlation)
        # Prefer tool-specific request_id from params over the general request_id
        event_request_id = serializable_params.get('request_id') or request_id
        call_event = {"type": "tool_call", "step": step + 1, "server": tool_name, "action": actual_tool_name, "params": serializable_params, "request_id": event_request_id}
        events = [call_event]
        results = []

        try:
            logger.info("Invoking external tool %s on server %s with params %s", actual_tool_name, server_name, params)
            # Use the integration the agent already set up.
            #
            # This used to call get_tool_integration(config=agent_config) --
            # an AgentConfig where an AgentSystemConfig is expected. It only
            # ever worked because the lookup returns the existing global
            # instance before it looks at config at all; the moment it had to
            # build one, it died with AttributeError on external_servers.
            manager = getattr(self._agent, "_tool_integration_manager", None) if self._agent else None
            tool_integration = getattr(manager, "tool_integration", None) if manager else None
            if tool_integration is None:
                system_config = getattr(self._agent, "system_config", None) if self._agent else None
                if system_config is None:
                    raise RuntimeError("Cannot access tool integration without system config")
                tool_integration = get_tool_integration(config=system_config)
            # Use serializable_params to avoid passing non-JSON-serializable objects (like CancellationToken) to external servers
            tool_result = await tool_integration.call_tool(server_name, actual_tool_name, serializable_params, "external")
            logger.info("External tool %s returned: %s", tool_name, str(tool_result)[:500])

            results.append({
                "server": tool_name,
                "action": actual_tool_name,
                "params": serializable_params,
                "result": tool_result
            })

            # Emit MCP result event (include request_id for correlation)
            result_event = {"type": "tool_result", "step": step + 1, "server": tool_name, "action": actual_tool_name, "result": tool_result, "request_id": event_request_id}
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
                                 params: Dict[str, Any], step: int, request_id: str | None = None,
                                 session_id: str | None = None, user_id: str | None = None) -> tuple[ChatMessage, List[Dict], List[Dict]]:
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
            # Get plugin server from tool integration plugin registry
            if self._agent and hasattr(self._agent, '_tool_integration_manager'):
                tool_integration = self._agent._tool_integration_manager.tool_integration
                if tool_integration is not None:  # type: ignore[unreachable]
                    if tool_integration.initialized:  # type: ignore[unreachable]
                        plugin_adapter = tool_integration.plugin_registry.get_server(tool_name)
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
            # Return error response instead of raising - allows agent to recover from hallucinated tool names
            logger.warning("Server not found for tool: %s (hallucinated tool call?)", tool_name)
            tool_call_id = tc.get("id") or f"error-call-{int(time.time()*1000)}"
            error_content = json.dumps({
                "error": f"Unknown tool: '{tool_name}'. The tool does not exist. Please check available tools and try again.",
                "type": "ToolNotFoundError"
            })
            message = ChatMessage(
                role="tool",
                tool_call_id=tool_call_id,
                name=sanitize_for_llm(openai_tool_name),
                content=sanitize_json_content(error_content),
                timestamp=datetime.now(timezone.utc)
            )
            return message, [{"type": "tool_error", "tool": tool_name, "error": f"Unknown tool: {tool_name}"}], []

        serializable_params = self._make_params_serializable(params)
        event_request_id = serializable_params.get('request_id') or request_id
        call_event = {"type": "tool_call", "step": step + 1, "server": tool_name, "action": openai_tool_name, "params": serializable_params, "request_id": event_request_id}
        events = [call_event]
        results = []

        try:
            logger.info("Invoking tool %s with params %s", openai_tool_name, params)

            # Inject session context (shared with Agent.dispatch_tool_call — see
            # inject_runtime_params; passed through the call chain to avoid races)
            params = inject_runtime_params(
                params, session_id=session_id, user_id=user_id,
                request_id=request_id, agent=self._agent)

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
                    from pydantic import ValidationError
                    from ....llm.models import MultimodalToolContent
                    # Convert to Pydantic models. An invalid item is dropped on its
                    # own: raising here would replace the whole tool result -- a job
                    # that already ran -- with an error, and the model would run it again.
                    multimodal_content = []
                    items = raw_multimodal if isinstance(raw_multimodal, list) else [raw_multimodal]
                    for item in items:
                        if isinstance(item, dict):
                            try:
                                multimodal_content.append(MultimodalToolContent(**item))
                            except ValidationError as exc:
                                logger.warning(
                                    "Dropping invalid multimodal item from tool %s: %s",
                                    tool_name, exc.errors(include_url=False),
                                )
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

            result_event = {"type": "tool_result", "step": step + 1, "server": tool_name, "action": openai_tool_name, "result": tool_result, "request_id": event_request_id}
            events.append(result_event)

            # Create tool result message with optional multimodal content
            tool_call_id = tc.get("id") or f"{tool_name}-call-{int(time.time()*1000)}"
            # default=str: a plugin may hand back a set or a date; the model gets
            # it as text instead of "invocation failed: not JSON serializable".
            tool_msg_content = sanitize_json_content(json.dumps(tool_result, ensure_ascii=False, default=str))
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