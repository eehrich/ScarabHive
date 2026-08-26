"""Message format adapter for OpenAI Realtime API.

This module provides conversion between the standard ChatMessage format
used throughout AgentSystem and the Realtime API's conversation item format.
"""
from __future__ import annotations

import logging
from typing import Any
from agent_system.llm.models import ChatMessage

logger = logging.getLogger(__name__)


class RealtimeMessageAdapter:
    """Converts between ChatMessage format and Realtime API conversation items."""
    
    @staticmethod
    def messages_to_conversation_items(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """Convert ChatMessage list to Realtime API conversation items.
        
        Args:
            messages: List of ChatMessage objects
            
        Returns:
            List of Realtime API conversation item dictionaries
            
        Example:
            ChatMessage(role='user', content='Hello')
            ->
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Hello"}]
            }
        """
        items = []
        
        for msg in messages:
            # Skip system messages - they go into session configuration instead
            if msg.role == "system":
                continue
                
            # Handle tool/function call messages
            if msg.role == "tool":
                # Tool result from previous function call
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.tool_call_id,
                    "output": msg.content or ""
                })
                continue
            
            # Handle assistant messages with tool calls
            if msg.role == "assistant" and msg.tool_calls:
                # Assistant requested function calls
                for tool_call in msg.tool_calls:
                    func = tool_call.get("function", {})
                    items.append({
                        "type": "function_call",
                        "call_id": tool_call.get("id"),
                        "name": func.get("name"),
                        "arguments": func.get("arguments", "{}")
                    })
                # Also add text content if present
                if msg.content:
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": msg.content}]
                    })
                continue
            
            # Regular user or assistant message
            content_type = "input_text" if msg.role == "user" else "text"
            items.append({
                "type": "message",
                "role": msg.role,
                "content": [{
                    "type": content_type,
                    "text": msg.content or ""
                }]
            })
        
        return items
    
    @staticmethod
    def extract_system_instructions(messages: list[ChatMessage]) -> str | None:
        """Extract system message content to use as session instructions.
        
        Args:
            messages: List of ChatMessage objects
            
        Returns:
            System message content, or None if no system message found
        """
        for msg in messages:
            if msg.role == "system":
                # Extract text content from content field (str or list)
                if isinstance(msg.content, str):
                    return msg.content
                elif isinstance(msg.content, list):
                    # Extract text from content array
                    text_parts = []
                    for item in msg.content:
                        if isinstance(item, str):
                            text_parts.append(item)
                        elif isinstance(item, dict) and item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                    return " ".join(text_parts) if text_parts else None
        return None
    
    @staticmethod
    def tools_to_realtime_tools(tools: list[dict]) -> list[dict]:
        """Convert tool schemas to Realtime API tool format.
        
        The Realtime API uses a FLATTENED tool format, different from Chat Completions!
        Chat Completions: {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
        Realtime API: {"type": "function", "name": "...", "description": "...", "parameters": {...}}
        
        Args:
            tools: List of tool definition dictionaries in Chat Completions format
            
        Returns:
            List of Realtime API tool dictionaries with flattened structure
        """
        import copy
        realtime_tools = []
        
        for tool in tools:
            if tool.get("type") == "function" and "function" in tool:
                # Extract function details from nested structure
                func_def = tool["function"]
                
                # Create flattened Realtime API tool
                realtime_tool = {
                    "type": "function",
                    "name": func_def.get("name"),
                    "description": func_def.get("description", ""),
                }
                
                # Add parameters if present
                if "parameters" in func_def:
                    params = copy.deepcopy(func_def["parameters"])
                    # Remove unsupported fields
                    if "additionalProperties" in params:
                        del params["additionalProperties"]
                    realtime_tool["parameters"] = params
                
                realtime_tools.append(realtime_tool)
            else:
                logger.warning(f"Skipping invalid tool schema: {tool}")
        
        return realtime_tools
    
    @staticmethod
    def parse_response_content(events: list[dict]) -> tuple[str | None, list[dict] | None]:
        """Extract assistant content and tool calls from response events.
        
        Args:
            events: List of response events from Realtime API
            
        Returns:
            Tuple of (content_text, tool_calls)
            - content_text: Accumulated text content or None
            - tool_calls: List of tool call dicts or None
            
        Example Response Events:
            [
                {"type": "response.content_part.added", "part": {"type": "text"}},
                {"type": "response.text.delta", "delta": "Hello"},
                {"type": "response.text.delta", "delta": " there!"},
                {"type": "response.text.done", "text": "Hello there!"},
                {"type": "response.done"}
            ]
        """
        text_parts = []
        tool_calls = []
        current_tool_call = None
        
        for event in events:
            event_type = event.get("type")
            
            # Text content deltas
            if event_type == "response.text.delta":
                delta = event.get("delta", "")
                text_parts.append(delta)
            
            # Text content done
            elif event_type == "response.text.done":
                # Use final text if available, otherwise use accumulated deltas
                final_text = event.get("text")
                if final_text is not None:
                    text_parts = [final_text]
            
            # Function call start
            elif event_type == "response.function_call_arguments.delta":
                if current_tool_call is None:
                    # Start accumulating new tool call
                    current_tool_call = {
                        "id": event.get("call_id"),
                        "type": "function",
                        "function": {
                            "name": event.get("name"),
                            "arguments": event.get("delta", "")
                        }
                    }
                else:
                    # Accumulate arguments
                    current_tool_call["function"]["arguments"] += event.get("delta", "")
            
            # Function call done
            elif event_type == "response.function_call_arguments.done":
                if current_tool_call:
                    # Use final arguments if provided
                    final_args = event.get("arguments")
                    if final_args is not None:
                        current_tool_call["function"]["arguments"] = final_args
                    
                    tool_calls.append(current_tool_call)
                    current_tool_call = None
        
        # Return accumulated content
        content = "".join(text_parts) if text_parts else None
        tool_calls_result = tool_calls if tool_calls else None
        
        return content, tool_calls_result
    
    @staticmethod
    def build_assistant_message(
        content: str | None,
        tool_calls: list[dict] | None
    ) -> dict[str, Any]:
        """Build assistant message dict from content and tool calls.
        
        Args:
            content: Text content from assistant
            tool_calls: List of tool call dicts
            
        Returns:
            Assistant message dictionary in ChatMessage-compatible format
        """
        message: dict[str, Any] = {
            "role": "assistant",
            "content": content or ""
        }
        
        if tool_calls:
            message["tool_calls"] = tool_calls
        
        return message
