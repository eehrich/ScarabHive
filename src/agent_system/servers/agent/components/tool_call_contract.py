"""The framework's side of a tool call: what it owns in the arguments and how an answer reads.

Which keys of a call's arguments belong to the framework (the runtime params
it injects, the request ids it sets) and how a tool result or tool message
reports an error, a call that never ran or multimodal items. The model's own
loop (tool_execution.py, tool_invocation.py), programmatic dispatch
(Agent.dispatch_tool_call, e.g. tool_script) and the hook integration all
follow these rules, so they live here once and none of those callers has to
import the execution manager to reach them.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ....core.request_context import register_request_user
from ....llm.models import ChatMessage
from ....llm.text_sanitizer import sanitize_for_llm, sanitize_json_content
from ..deferred_tools import NOT_LOADED_TYPE

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)

# Per-tool request ids the framework sets on every call (see execute_tools_streaming).
FRAMEWORK_REQUEST_ID_KEYS = frozenset({"request_id", "requestId"})

#: "type" of the result the model reads for a call a pre_tool_call hook blocked.
BLOCKED_CALL_TYPE = "ToolCallBlocked"
#: Result types of calls that never ran (tool_message_never_ran).
NEVER_RAN_TYPES = (BLOCKED_CALL_TYPE, NOT_LOADED_TYPE)


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


def tool_message_never_ran(message: Any) -> bool:
    """Whether a tool-result message stands for a call that never ran: one a
    pre_tool_call hook blocked, or a deferred tool called before it was loaded."""
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not any(kind in content for kind in NEVER_RAN_TYPES):
        return False
    try:
        data = json.loads(content)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("type") in NEVER_RAN_TYPES


def inject_runtime_params(params: Dict[str, Any], *,
                          session_id: Optional[str] = None,
                          user_id: Optional[str] = None,
                          request_id: Optional[str] = None,
                          agent: Optional["Agent"] = None) -> Dict[str, Any]:
    """Return a copy of ``params`` with the runtime context params injected.

    THE single place that defines which runtime params a tool call receives
    (_session_id, _user_id, _request_id, _agent_name, _agent). Used by the LLM
    tool path (ToolInvoker.execute_plugin_tool) and by programmatic dispatch
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


def make_params_serializable(params: Dict[str, Any]) -> Dict[str, Any]:
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


def pop_multimodal_content(tool_result: Any, tool_name: str) -> Optional[List[Any]]:
    """Take ``_multimodal_content`` ([{type, path, mime_type, description}]) out of a result.

    The items ride on the tool message as real image/audio parts; left in the
    result they reach the model as a JSON list of paths. External MCP results
    carry them too (mcp_client persists image blocks) -- that path used to
    skip this, and a screenshot never reached a model as an image.
    """
    if not isinstance(tool_result, dict):
        return None
    raw_multimodal = tool_result.pop("_multimodal_content", None)
    if not raw_multimodal:
        return None
    from pydantic import ValidationError
    from ....llm.models import MultimodalToolContent
    # An invalid item is dropped on its own: raising here would replace the
    # whole tool result -- a job that already ran -- with an error, and the
    # model would run it again.
    multimodal_content: List[Any] = []
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
        logger.debug("Extracted %d multimodal items from tool %s", len(multimodal_content), tool_name)
    return multimodal_content or None


def call_id_or(tc: Dict, fallback: str) -> str:
    """The provider's id of the call ``tc``, or ``<fallback>-<milliseconds>`` for
    a call that came without one: the id its tool message answers under."""
    return tc.get("id") or f"{fallback}-{int(time.time()*1000)}"


def tool_error_message(call_id: str, openai_tool_name: str, error_content: str) -> ChatMessage:
    """The tool message the model reads in place of a result -- the call failed,
    was cancelled or blocked: ``error_content`` (JSON) under the call's id, name
    and content sanitized."""
    return ChatMessage(
        role="tool",
        tool_call_id=call_id,
        name=sanitize_for_llm(openai_tool_name),
        content=sanitize_json_content(error_content),
        timestamp=datetime.now(timezone.utc)
    )


def execution_failed_message(tc: Dict, tool_name: str, openai_tool_name: str,
                             exc: BaseException) -> ChatMessage:
    """The answer to a call that raised: ``{"error": "Tool '<tool>' execution
    failed: ...", "type": <exception class>}``. A call with no answer leaves an
    orphaned tool_call, which the LLM API rejects."""
    call_id = call_id_or(tc, "error-call")
    error_content = json.dumps({
        "error": f"Tool '{tool_name}' execution failed: {str(exc)}",
        "type": type(exc).__name__
    })
    return tool_error_message(call_id, openai_tool_name, error_content)
