"""
Utility functions for agent execution and result collection.
"""
import inspect
import uuid
from typing import Any, Callable, Dict, Optional, Union
from .server import Agent
from ...llm.models import ChatMessage


def extract_summary(result: Dict[str, Any]) -> str:
    """
    Extract a summary from the agent result for easier consumption.

    Args:
        result: The agent execution result dict

    Returns:
        A summary string describing the execution outcome
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


async def collect_final_result(
    agent: Agent, 
    task: Union[str, ChatMessage], 
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    llm_override: Optional[object] = None,
    llm_profile_info_override: Optional[str] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    Collect final result from agent.run_events() into a structured result dict.
    
    This utility function allows consumers to get a final result from the streaming
    run_events() method, maintaining the same result format as the old run() method.
    
    Args:
        agent: The agent instance to execute
        task: The task to execute (string or ChatMessage with multimodal content)
        request_id: Optional request ID for correlation
        session_id: Optional session ID for conversation history. Omit it for a
            STATELESS call -- an ephemeral id is generated so the run starts
            with an empty history and leaves nothing behind (sub-agents it
            starts excepted, see EPHEMERAL_SESSION_PREFIX).
        llm_override: Optional LLM client to use instead of agent's default
        llm_profile_info_override: Optional profile info string for status display
        on_event: Called with every event as it passes, before it is collected --
            for a caller that shows the run while it collects it (POST /run's job,
            agent-cli's stream). What it returns is awaited when it is awaitable:
            agent-cli prints the answer through async output hooks as it arrives.

    Returns:
        Dict containing task, calls, summary, and optionally errors
    """
    # An omitted session_id used to reach the session tracker as the literal
    # key None, so EVERY caller that did not pass one shared a single growing
    # history on that agent instance. Per-item agents (one instance reused for
    # hundreds of segments/scenes) therefore replayed every previous exchange
    # on every call until the request blew past the model's context limit
    # ("input token count exceeds the maximum number of tokens allowed
    # 1048576" on the audio_text_comparator). "No session id" must mean "no
    # shared history", so give each such call its own throwaway session.
    ephemeral_session = session_id is None
    if ephemeral_session:
        from agent_system.services.session_service import EPHEMERAL_SESSION_PREFIX

        session_id = f"{EPHEMERAL_SESSION_PREFIX}{uuid.uuid4()}"

    # Extract task text for result logging
    if isinstance(task, ChatMessage):
        if isinstance(task.content, str):
            task_text = task.content
        elif isinstance(task.content, list):
            # Handle Pydantic models (TextContent, ImageContent)
            text_parts = []
            for item in task.content:
                if hasattr(item, 'type') and item.type == "text":
                    text_parts.append(getattr(item, 'text', ''))
            task_text = " ".join(text_parts) if text_parts else "[multimodal input]"
        else:
            task_text = "[multimodal input]"
    else:
        task_text = task
    
    result = {"task": task_text, "calls": []}
    # Inside another run (an agent-as-tool, a plugin calling an agent from its tool
    # call), a Ctrl-C is not this call's: its user started the outer run, and only
    # that run's handler knows it (agent-cli, chat). Asked now -- the nested run
    # sets the variable itself.
    from ...tools.status import current_request_id
    inside_a_run = bool(current_request_id.get())
    
    try:
        async for event in agent.run_events(task, request_id=request_id, session_id=session_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info_override):
            if on_event is not None:
                shown = on_event(event)
                if inspect.isawaitable(shown):
                    await shown
            event_type = event.get("type")
            
            # Collect MCP calls for the result
            if event_type in ("tool_call", "mcp_call"):  # the old name until every deployed side is new (rename 17.09.2026)
                # Initialize the call entry
                call_entry = {
                    "server": event.get("server"),
                    "action": event.get("action"), 
                    "params": event.get("params", {})
                }
                result["calls"].append(call_entry)
            
            elif event_type in ("tool_result", "mcp_result"):  # the old name until every deployed side is new (rename 17.09.2026)
                # Find matching call and add result
                server = event.get("server")
                action = event.get("action")
                for call in reversed(result["calls"]):
                    if call.get("server") == server and call.get("action") == action and "result" not in call:
                        call["result"] = event.get("result")
                        break
            
            elif event_type == "final":
                result["summary"] = event.get("summary")
            
            elif event_type == "cancelled":
                result["cancelled"] = True
            
            elif event_type == "error":
                result.setdefault("errors", []).append(event.get("message"))
                from .server import refused_before_the_run

                if refused_before_the_run(event):  # it ran nothing: its caller saves nothing after it
                    result["refused"] = event["error_type"]
    
    except (KeyboardInterrupt, Exception) as e:
        # Handle cancellation gracefully
        import asyncio
        if isinstance(e, KeyboardInterrupt) and inside_a_run:
            raise
        if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
            result["cancelled"] = True
        else:
            result.setdefault("errors", []).append(str(e))
    finally:
        # A throwaway session must not outlive its single call, otherwise the
        # tracker accumulates one entry per invocation.
        if ephemeral_session:
            tracker = getattr(agent, "_session_tracker", None)
            discard = getattr(tracker, "discard_session", None)
            if discard is not None:
                try:
                    discard(session_id)
                except Exception:  # noqa: BLE001 - cleanup must never fail a run
                    pass

    return result