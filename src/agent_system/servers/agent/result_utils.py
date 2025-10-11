"""
Utility functions for agent execution and result collection.
"""
from typing import Dict, Any, Optional, Union
from .server import Agent
from ...llm.models import ChatMessage


async def collect_final_result(
    agent: Agent, 
    task: Union[str, ChatMessage], 
    request_id: Optional[str] = None,
    llm_override: Optional[object] = None,
    llm_profile_info_override: Optional[str] = None
) -> Dict[str, Any]:
    """
    Collect final result from agent.run_events() into a structured result dict.
    
    This utility function allows consumers to get a final result from the streaming
    run_events() method, maintaining the same result format as the old run() method.
    
    Args:
        agent: The agent instance to execute
        task: The task to execute (string or ChatMessage with multimodal content)
        request_id: Optional request ID for correlation
        llm_override: Optional LLM client to use instead of agent's default
        llm_profile_info_override: Optional profile info string for status display
        
    Returns:
        Dict containing task, calls, summary, and optionally errors
    """
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
    
    async for event in agent.run_events(task, request_id=request_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info_override):
        event_type = event.get("type")
        
        # Collect MCP calls for the result
        if event_type == "mcp_call":
            # Initialize the call entry
            call_entry = {
                "server": event.get("server"),
                "action": event.get("action"), 
                "params": event.get("params", {})
            }
            result["calls"].append(call_entry)
        
        elif event_type == "mcp_result":
            # Find matching call and add result
            server = event.get("server")
            action = event.get("action")
            for call in reversed(result["calls"]):
                if call.get("server") == server and call.get("action") == action and "result" not in call:
                    call["result"] = event.get("result")
                    break
        
        elif event_type == "final":
            result["summary"] = event.get("summary")
        
        elif event_type == "error":
            result.setdefault("errors", []).append(event.get("message"))
    
    return result