"""
Tool Execution Manager for Agent Server
Handles execution of both internal plugin tools and external tools.

ToolExecutionManager runs the tool calls of one step of the model
(execute_tools_streaming), phase after phase: check the calls, ask the
pre_tool_call hooks in call order, start the calls as parallel tasks under the
cancellation system, stream status events until all are done, ask the
post_tool_call hooks in call order, answer in call order. Running a single
call on its server is ToolInvoker's (tool_invocation.py). The rules every tool
call follows -- runtime params, error and never-ran results, multimodal items
-- are in tool_call_contract.py and re-exported here for the modules that
import them from this one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, TYPE_CHECKING, AsyncGenerator, Callable, Tuple

if TYPE_CHECKING:
    from ..server import Agent
    from ....tools.base import ToolServerRegistry
    from .hook_integration import HookIntegrationManager
    from .status_forwarding import StatusEventForwarder

from ....core.cancellation import get_cancellation_manager, cancellable_operation, CancellationError
from ....hooks.plugin_hook import HookType
from ....llm.caller_llm import context_for_tool
from ....llm.models import ChatMessage
from ....llm.text_sanitizer import sanitize_for_llm, sanitize_json_content
from ....utils.json_utils import parse_tool_arguments
from .tool_call_contract import (
    BLOCKED_CALL_TYPE,
    ToolDispatchError,
    call_id_or,
    drop_runtime_params,
    execution_failed_message,
    inject_runtime_params,
    pop_multimodal_content,
    tool_error_message,
    tool_message_never_ran,
    tool_result_is_error,
)
from .tool_invocation import CallOutcome, ToolInvoker

# The agent server, plugins and tests import these from here.
__all__ = [
    "ToolExecutionManager",
    "ToolDispatchError",
    "drop_runtime_params",
    "inject_runtime_params",
    "pop_multimodal_content",
    "tool_message_never_ran",
    "tool_result_is_error",
]

logger = logging.getLogger(__name__)


@dataclass
class _StepCalls:
    """The model's tool calls of one step and what has become of each so far:
    what execute_tools_streaming hands from phase to phase. One per call of
    it, never manager state -- concurrent requests share the manager."""

    step: int
    request_id: str | None
    session_id: str | None
    user_id: str | None
    # Per-request status forwarder (see execute_tools_streaming)
    status_forwarder: Optional[StatusEventForwarder]
    # (tc, tool_name, openai_tool_name, params) of the calls that passed the checks
    valid_tool_executions: List[Tuple[Dict, str, str, Dict[str, Any]]] = field(default_factory=list)
    positions: List[int] = field(default_factory=list)  # valid call index -> position among tool_calls
    # (position among the model's calls, message, events, results). Every
    # answer is sorted into the order of the calls before it joins the
    # history -- the rejected ones as well.
    # IMPORTANT: Gemini API requires function_response parts to be in the same
    # order as the original function_call parts to avoid MALFORMED_FUNCTION_CALL errors
    indexed_results: List[Tuple[int, ChatMessage, List[Dict], List[Dict]]] = field(default_factory=list)
    events_to_yield: List[Dict] = field(default_factory=list)
    results_to_add: List[Dict] = field(default_factory=list)
    tool_messages: List[ChatMessage] = field(default_factory=list)  # the answers, in call order
    # Whether pre_tool_call / post_tool_call hooks run for this step, and the
    # run's cancellation token they get.
    run_pre_hooks: bool = False
    run_post_hooks: bool = False
    hook_token: Any = None
    hooked_calls: Dict[int, Dict[str, Any]] = field(default_factory=dict)  # position -> the call as the hooks see it
    # position -> when the call itself started / ended (time.time()). The
    # post hooks run once every call of the step is done; their own clock
    # says when the step ended, so each call's span is handed to them.
    call_started: Dict[int, float] = field(default_factory=dict)
    call_finished: Dict[int, float] = field(default_factory=dict)
    blocked: set[int] = field(default_factory=set)  # valid call indices
    # Store task -> tool_info mapping for error handling
    task_tool_info: Dict[asyncio.Task, tuple] = field(default_factory=dict)  # task -> (tc, tool_name, openai_tool_name)
    task_indices: Dict[asyncio.Task, int] = field(default_factory=dict)  # task -> original index (for ordering responses)


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
        # Runs a single call on its server, with the same registry as its
        # legacy fallback (tool_invocation.py).
        self._invoker = ToolInvoker(registry, agent)
        # NOTE: session_id/user_id are deliberately NOT instance state — they are
        # passed through the call chain per request (see execute_tools_streaming)
        # to avoid races when concurrent requests share this manager.

    async def _invoke_tool(self, tool_name: str, params: Dict[str, Any]):
        """Execute a tool call against the registry and return results -- the
        bare call behind Agent.call_tool (ToolInvoker.invoke_tool)."""
        return await self._invoker.invoke_tool(tool_name, params)

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
        intercept: Optional[Callable[[Optional[str], Any, int], Optional[Dict[str, Any]]]] = None,
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
            intercept: Answers a call itself instead of a tool server (name, arguments, step) ->
                             result, or None to leave the call alone: the run's deferred tools
                             (deferred_tools.py). An answered call reaches no hook. A call
                             with broken JSON is shown with arguments None: it keeps its
                             parse error, and an unloaded deferred tool is loaded for it.

        Yields:
            Dict with either:
            - {"type": "status", "event": {...}} - Status event to forward
            - {"type": "tool_events", "events": [...]} - Tool execution events
            - {"type": "complete", "messages": [...], "results": [...]} - Final results

        The phases, in this order: _check_calls, _ask_pre_hooks_in_call_order
        (before any call starts), _start_calls, _stream_until_done,
        _answer_in_call_order (the post hooks, once all calls are done), then
        the last status events and the results.
        """
        # NOTE: session_id and user_id are passed as parameters through the call chain
        # to avoid race conditions when multiple requests share the same ToolExecutionManager.
        # DO NOT store them as instance variables (self._current_session_id/user_id)!
        calls = _StepCalls(step, request_id, session_id, user_id, status_forwarder)

        self._check_calls(calls, tool_calls, tool_name_mapping, available_tools, intercept)

        # pre_tool_call / post_tool_call. Only for an agent some hook of the type
        # would run for: without one no context is built and nothing is copied.
        # Calls the framework already rejected above (malformed arguments,
        # unknown tool) never ran and reach no hook.
        hooks = self._hook_manager
        calls.run_pre_hooks = (bool(calls.valid_tool_executions) and hooks is not None
                               and hooks.wants_hooks(HookType.PRE_TOOL_CALL))
        calls.run_post_hooks = (bool(calls.valid_tool_executions) and hooks is not None
                                and hooks.wants_hooks(HookType.POST_TOOL_CALL))
        calls.hook_token = (get_cancellation_manager().get_token(request_id)
                            if request_id and (calls.run_pre_hooks or calls.run_post_hooks) else None)
        if calls.run_pre_hooks or calls.run_post_hooks:
            async with aclosing(self._ask_pre_hooks_in_call_order(calls)) as phase:
                async for item in phase:
                    yield item

        # Execute all valid tools in parallel with real-time status streaming
        if calls.valid_tool_executions:
            tasks = await self._start_calls(calls, assistant_message, llm_profile)
            async with aclosing(self._stream_until_done(calls, tasks)) as phase:
                async for item in phase:
                    yield item

        async with aclosing(self._answer_in_call_order(calls)) as phase:
            async for item in phase:
                yield item

        if calls.valid_tool_executions:
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
        if calls.events_to_yield:
            yield {"type": "tool_events", "events": calls.events_to_yield}

        # Yield final completion with all results
        yield {
            "type": "complete",
            "messages": calls.tool_messages,
            "results": calls.results_to_add
        }

    def _check_calls(
        self,
        calls: _StepCalls,
        tool_calls: List[Dict],
        tool_name_mapping: Dict[str, str],
        available_tools: List[str],
        intercept: Optional[Callable[[Optional[str], Any, int], Optional[Dict[str, Any]]]],
    ) -> None:
        """Parse each call's arguments and answer at once, in call order, the
        calls that do not run: malformed arguments, a call ``intercept``
        answers, an unknown tool. The others go on to the hooks and the tool
        servers (calls.valid_tool_executions)."""
        step = calls.step
        request_id = calls.request_id

        # Prepare tool executions (same as execute_tools())
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
                if intercept is not None:
                    intercept(openai_tool_name, None, step)  # loads an unloaded deferred tool, in call order
                call_id = call_id_or(tc, "parse-error")
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
                calls.events_to_yield.append({
                    "type": "tool_error",
                    "tool": tool_name,
                    "error": f"Invalid JSON arguments for {tool_name}",
                })
                calls.indexed_results.append((pos, ChatMessage(
                    role="tool",
                    tool_call_id=call_id,
                    name=openai_tool_name or "unknown",
                    content=error_content,
                    timestamp=datetime.now(timezone.utc),
                ), [], []))
                continue

            answer = intercept(openai_tool_name, params, step) if intercept is not None else None
            if answer is not None:
                calls.indexed_results.append((pos, ChatMessage(
                    role="tool",
                    tool_call_id=call_id_or(tc, f"{openai_tool_name}-call"),
                    name=sanitize_for_llm(openai_tool_name),
                    content=json.dumps(answer, ensure_ascii=False),
                    timestamp=datetime.now(timezone.utc),
                ), [], []))
                calls.events_to_yield.extend([
                    {"type": "tool_call", "step": step + 1, "server": tool_name, "action": openai_tool_name,
                     "params": params, "request_id": request_id},
                    {"type": "tool_result", "step": step + 1, "server": tool_name, "action": openai_tool_name,
                     "result": answer, "request_id": request_id},
                ])
                continue

            if not tool_name or tool_name not in available_tools:
                logger.warning("Unknown tool requested: %s (OpenAI name: %s)", tool_name, openai_tool_name)
                # Use tool_error type instead of error - error type causes frontend to abort
                calls.events_to_yield.append({"type": "tool_error", "tool": tool_name, "error": f"Unknown tool: {tool_name}"})
                call_id = call_id_or(tc, "error-call")
                # Return detailed error message so LLM can recover
                error_content = json.dumps({
                    "error": f"Unknown tool: '{tool_name}'. The tool does not exist. Please check available tools and try again.",
                    "type": "ToolNotFoundError"
                })
                calls.indexed_results.append((pos, ChatMessage(
                    role="tool",
                    tool_call_id=call_id,
                    name=openai_tool_name or "unknown",
                    content=error_content,
                    timestamp=datetime.now(timezone.utc)
                ), [], []))
                continue

            calls.valid_tool_executions.append((tc, tool_name, openai_tool_name, params))
            calls.positions.append(pos)

    async def _ask_pre_hooks_in_call_order(self, calls: _StepCalls) -> AsyncGenerator[Dict[str, Any], None]:
        """Put each call the way the hooks see it into calls.hooked_calls, and
        ask the pre_tool_call hooks about it first when there are any: a call
        they block is answered here and does not start, one whose arguments
        they change runs with those."""
        for i, (tc, tool_name, openai_tool_name, params) in enumerate(calls.valid_tool_executions):
            # tool_name is what the mapping resolved: the server (for an
            # external MCP tool "<server>.<tool>"); the model called the tool.
            call = {"id": tc.get("id"), "name": openai_tool_name, "server": tool_name,
                    "arguments": params, "source": "model"}
            # A cancelled run asks nobody: the call reports itself cancelled
            # below without having started, and no hook -- post included --
            # takes it for one that ran.
            if calls.hook_token is not None and calls.hook_token.is_cancelled:
                continue
            if calls.run_pre_hooks:
                # One call after the other, in the order the model sent them,
                # before any of them starts: a hook that asks a person asks
                # one question at a time, and blocking one call leaves the
                # calls around it as they are.
                outcome: List[Any] = []
                async with aclosing(self._forwarding_status(
                        self._run_pre_tool_hooks(call, calls.step, calls.request_id, calls.session_id,
                                                 calls.hook_token),
                        calls.status_forwarder, outcome)) as forwarded:
                    async for item in forwarded:
                        yield item
                arguments, block = outcome[0]
                # A run cancelled while its hooks were asked reports the call
                # cancelled below, not blocked: a hook that stopped waiting on
                # the cancel (and failed, under on_error: block) checked nothing.
                if calls.hook_token is not None and calls.hook_token.is_cancelled:
                    continue
                if block is not None:
                    calls.blocked.add(i)
                    calls.indexed_results.append(
                        (calls.positions[i], self._create_blocked_response(tc, openai_tool_name, block), [], []))
                    calls.events_to_yield.append({"type": "tool_error", "tool": tool_name,
                                                  "error": block, "blocked": True})
                    continue
                call = {**call, "arguments": arguments}
                calls.valid_tool_executions[i] = (tc, tool_name, openai_tool_name, arguments)
            calls.hooked_calls[calls.positions[i]] = call

    async def _start_calls(self, calls: _StepCalls, assistant_message: Optional[ChatMessage],
                           llm_profile: Optional[str]) -> List[asyncio.Task]:
        """Start each call the hooks did not block as a task of its own, under a
        request id of its own; returns the tasks."""
        # Create tasks for parallel execution with unique request_id suffixes
        tasks = []
        request_ids: Dict[str, str] = {}  # call id -> the id its tool runs under
        for i, (tc, tool_name, openai_tool_name, params) in enumerate(calls.valid_tool_executions):
            if i in calls.blocked:
                continue
            # Create tool-specific request_id (same logic as execute_tools)
            original_request_id = calls.request_id
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

            calls.call_started[calls.positions[i]] = time.time()
            task = asyncio.create_task(
                self._execute_single_tool(tc, tool_name, openai_tool_name, params_with_suffix, calls.step,
                                          tool_specific_request_id, calls.session_id, calls.user_id,
                                          main_request_id=original_request_id),
                # Its own context copy, holding the run's profile: set here, it
                # cannot leak into the run or a sibling call.
                context=context_for_tool(llm_profile),
            )
            # Stamped by the loop when the task ends, not when this poll
            # gets to it: the poll hands status events on in between, and
            # a slow consumer of the stream would lengthen the call.
            task.add_done_callback(
                lambda _task, pos=calls.positions[i]: calls.call_finished.setdefault(pos, time.time()))
            tasks.append(task)
            calls.task_tool_info[task] = (tc, tool_name, openai_tool_name)
            calls.task_indices[task] = calls.positions[i]  # Store original position for ordering
            if tc.get("id") and tool_specific_request_id:
                request_ids[tc["id"]] = tool_specific_request_id
        if assistant_message is not None and request_ids:
            # Before any tool answers: the live list holds this same message, so a
            # viewer who joins while a call still waits finds its runs already.
            assistant_message.tool_request_ids = request_ids
        return tasks

    async def _stream_until_done(self, calls: _StepCalls,
                                 tasks: List[asyncio.Task]) -> AsyncGenerator[Dict[str, Any], None]:
        """Pass the run's status events on while the calls run, and collect each
        call's answer -- or an error or cancellation in its place -- as its task
        ends."""
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
            if calls.status_forwarder:
                status_events = calls.status_forwarder.get_pending_events()
                for status_event in status_events:
                    yield {"type": "status", "event": status_event}

            # Process completed tasks
            for task in done:
                original_index = calls.task_indices.get(task, 999)  # Default high index if not found
                try:
                    # task.result() RAISES on failure (asyncio.wait, not
                    # gather(return_exceptions=True)) -- errors land in the
                    # except blocks below, which build the error ChatMessage.
                    tool_message, events, tool_results = task.result()
                    # Store with original index for later sorting
                    calls.indexed_results.append((original_index, tool_message, events, tool_results))
                    calls.events_to_yield.extend(events)
                    calls.results_to_add.extend(tool_results)
                except asyncio.CancelledError:
                    logger.debug("Tool task was cancelled")
                    # Task was cancelled, this is expected during request cancellation
                    # Create a cancelled response to avoid orphaned tool_calls
                    if task in calls.task_tool_info:
                        tc, tool_name, openai_tool_name = calls.task_tool_info[task]
                        cancelled_msg = ChatMessage(
                            role="tool",
                            tool_call_id=call_id_or(tc, "cancelled-call"),
                            name=sanitize_for_llm(openai_tool_name),
                            content=json.dumps({"error": f"Tool '{tool_name}' was cancelled."}),
                            timestamp=datetime.now(timezone.utc)
                        )
                        calls.indexed_results.append((original_index, cancelled_msg, [], []))
                        calls.events_to_yield.append({"type": "tool_cancelled", "tool": tool_name})
                except Exception as e:
                    # CRITICAL: Create error response to avoid orphaned tool_calls
                    # Without this, the LLM will crash because it expects a tool response for every tool_call
                    logger.exception("Error processing tool result: %s", e)
                    if task in calls.task_tool_info:
                        tc, tool_name, openai_tool_name = calls.task_tool_info[task]
                        error_msg = execution_failed_message(tc, tool_name, openai_tool_name, e)
                        calls.indexed_results.append((original_index, error_msg, [], []))
                        calls.events_to_yield.append({"type": "tool_error", "tool": tool_name, "error": str(e)})

    async def _answer_in_call_order(self, calls: _StepCalls) -> AsyncGenerator[Dict[str, Any], None]:
        """The answers in the order of the calls (calls.tool_messages), each
        handed to the post_tool_call hooks before it joins them."""
        # Sort results by original position and extract messages
        # CRITICAL: Gemini API requires function_response parts to match the order
        # of the original function_call parts. Without this sorting, parallel tool
        # execution can produce responses in completion order (not call order),
        # causing MALFORMED_FUNCTION_CALL errors.
        calls.indexed_results.sort(key=lambda x: x[0])
        for pos, msg, _, _ in calls.indexed_results:
            if calls.run_post_hooks and pos in calls.hooked_calls:
                # In call order, after all of them are done, and before the
                # result joins the history: nothing already sent changes.
                async with aclosing(self._forwarding_status(
                        self._run_post_tool_hooks(calls.hooked_calls[pos], msg, calls.step, calls.request_id,
                                                  calls.session_id, calls.hook_token,
                                                  started_at=calls.call_started.get(pos),
                                                  finished_at=calls.call_finished.get(pos)),
                        calls.status_forwarder, [])) as forwarded:
                    async for item in forwarded:
                        yield item
            calls.tool_messages.append(msg)

    async def _execute_single_tool(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                 params: Dict[str, Any], step: int, request_id: str | None = None,
                                 session_id: str | None = None, user_id: str | None = None,
                                 main_request_id: str | None = None) -> CallOutcome:
        """Execute a single tool and return the result message, events, and results."""
        # Use cancellation system if request_id is available
        if request_id:
            return await self._execute_with_cancellation(tc, tool_name, openai_tool_name, params, step, request_id, session_id, user_id,
                                                         main_request_id=main_request_id)
        else:
            # Legacy execution without cancellation - wrap in try/except for robustness
            try:
                return await self._invoker.execute_tool(tc, tool_name, openai_tool_name, params, step, request_id, session_id, user_id)
            except Exception as e:
                # Handle unknown tool errors gracefully - return error message instead of crashing
                logger.exception("Tool %s execution failed (legacy path): %s", tool_name, e)
                message = execution_failed_message(tc, tool_name, openai_tool_name, e)
                return message, [{"type": "tool_error", "tool": tool_name, "error": str(e)}], []

    async def _execute_with_cancellation(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                       params: Dict[str, Any], step: int, request_id: str,
                                       session_id: str | None = None, user_id: str | None = None,
                                       main_request_id: str | None = None) -> CallOutcome:
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
                task = asyncio.create_task(
                    self._invoker.execute_tool(tc, tool_name, openai_tool_name, params_with_token, step, request_id, session_id, user_id)
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
                    message = execution_failed_message(tc, tool_name, openai_tool_name, e)
                    return message, [{"type": "tool_error", "tool": tool_name, "error": str(e), "request_id": request_id}], []

            except CancellationError as e:
                # Tool gracefully cancelled itself
                logger.info("Tool %s gracefully cancelled (request_id: %s)", tool_name, request_id)
                return self._create_cancelled_response(tc, tool_name, openai_tool_name, request_id, forced=e.forced)

    def _create_cancelled_response(self, tc: Dict, tool_name: str, openai_tool_name: str,
                                 request_id: str, forced: bool = False) -> CallOutcome:
        """Create a cancelled tool response."""
        call_id = call_id_or(tc, "cancelled-call")
        cancel_type = "force-cancelled" if forced else "cancelled"
        cancel_content = json.dumps({
            "error": f"Tool '{tool_name}' was {cancel_type}.",
            "cancelled": True,
            "forced": forced
        })

        message = tool_error_message(call_id, openai_tool_name, cancel_content)

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
        return tool_error_message(
            call_id_or(tc, "blocked-call"), openai_tool_name,
            json.dumps({"status": "error", "error": reason, "type": BLOCKED_CALL_TYPE}, ensure_ascii=False))
