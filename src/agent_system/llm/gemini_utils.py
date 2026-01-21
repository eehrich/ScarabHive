"""Shared utilities for Gemini API conversion.

Provides common conversion logic used by both GeminiClient (HTTP-based),
GeminiSDKClient (SDK-based), and GeminiBatchClient to convert OpenAI format 
to Gemini format.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Gemini API has a 20MB request size limit
GEMINI_MAX_REQUEST_BYTES = 20 * 1024 * 1024  # 20 MB
GEMINI_TARGET_REQUEST_BYTES = 15 * 1024 * 1024  # 15 MB target after compaction


def extract_available_tool_names(tools: List[Dict[str, Any]]) -> Set[str]:
    """Extract tool names from OpenAI-format tools list.
    
    This is used to determine which tools are currently available,
    so we can filter out tool calls from history that reference
    tools that are no longer available (e.g., after agent switch).
    
    Args:
        tools: List of OpenAI-format tool dicts with type="function"
    
    Returns:
        Set of tool function names
    """
    available = set()
    for tool in tools:
        if tool.get("type") == "function":
            func = tool.get("function", {})
            if func.get("name"):
                available.add(func["name"])
    return available


def estimate_contents_bytes(contents: List[Dict[str, Any]]) -> int:
    """Estimate the total size of Gemini contents in bytes.
    
    This is used to check against Gemini's 20MB request size limit.
    
    Args:
        contents: List of Gemini content dicts with role and parts
        
    Returns:
        Estimated size in bytes
    """
    total_bytes = 0
    
    for content in contents:
        # Role overhead
        role = content.get("role", "")
        total_bytes += len(role) + 20  # JSON structure overhead
        
        parts = content.get("parts", [])
        for part in parts:
            if not isinstance(part, dict):
                continue
                
            # Text parts
            if "text" in part:
                text = part["text"]
                total_bytes += len(text.encode('utf-8')) if isinstance(text, str) else 0
            
            # Inline data (images, audio)
            if "inlineData" in part:
                inline_data = part["inlineData"]
                if isinstance(inline_data, dict):
                    data = inline_data.get("data", "")
                    total_bytes += len(data) if isinstance(data, str) else 0
            
            # Function calls/responses
            if "functionCall" in part:
                total_bytes += len(json.dumps(part["functionCall"]))
            if "functionResponse" in part:
                total_bytes += len(json.dumps(part["functionResponse"]))
    
    return total_bytes


def compact_contents_for_byte_limit(
    contents: List[Dict[str, Any]],
    max_bytes: int = GEMINI_MAX_REQUEST_BYTES,
    target_bytes: int = GEMINI_TARGET_REQUEST_BYTES
) -> Tuple[List[Dict[str, Any]], int]:
    """Compact Gemini contents by replacing old inline data with text placeholders.
    
    This is a fallback mechanism when Context Engineer's compaction wasn't enough
    or isn't enabled. It removes inline data from oldest messages first until
    the request size is under the target.
    
    Args:
        contents: List of Gemini content dicts
        max_bytes: Maximum bytes before compaction triggers
        target_bytes: Target bytes after compaction
        
    Returns:
        (compacted_contents, bytes_removed) tuple
    """
    current_bytes = estimate_contents_bytes(contents)
    
    if current_bytes <= max_bytes:
        return contents, 0
    
    logger.warning(
        f"[Gemini] Request size {current_bytes / (1024*1024):.1f}MB exceeds "
        f"{max_bytes / (1024*1024):.1f}MB limit. Compacting inline data..."
    )
    
    # Deep copy to avoid mutating original
    import copy
    compacted = copy.deepcopy(contents)
    
    bytes_removed = 0
    
    # Collect all inline data with their locations and sizes
    # Format: (content_idx, part_idx, size, mime_type)
    inline_data_items: List[Tuple[int, int, int, str]] = []
    
    for content_idx, content in enumerate(compacted):
        parts = content.get("parts", [])
        for part_idx, part in enumerate(parts):
            if isinstance(part, dict) and "inlineData" in part:
                inline_data = part["inlineData"]
                if isinstance(inline_data, dict):
                    data = inline_data.get("data", "")
                    size = len(data) if isinstance(data, str) else 0
                    mime_type = inline_data.get("mimeType", "unknown")
                    if size > 0:
                        inline_data_items.append((content_idx, part_idx, size, mime_type))
    
    if not inline_data_items:
        logger.warning("[Gemini] No inline data found to compact")
        return compacted, 0
    
    # Sort by content index (oldest first) - we want to remove oldest data first
    # Keep last 2 content blocks protected
    protected_indices = set(range(max(0, len(compacted) - 2), len(compacted)))
    
    # Filter to only include items from non-protected content blocks
    compactable_items = [
        item for item in inline_data_items 
        if item[0] not in protected_indices
    ]
    
    # Sort by content index (oldest first)
    compactable_items.sort(key=lambda x: x[0])
    
    # Remove inline data until we're under target
    for content_idx, part_idx, size, mime_type in compactable_items:
        if current_bytes <= target_bytes:
            break
        
        # Replace inline data with text placeholder
        part = compacted[content_idx]["parts"][part_idx]
        media_type = mime_type.split("/")[0] if "/" in mime_type else "media"
        
        placeholder = {
            "text": f"[{media_type.title()} removed: {mime_type}, {size / 1024:.0f}KB - "
                    f"compacted to reduce request size]"
        }
        compacted[content_idx]["parts"][part_idx] = placeholder
        
        bytes_removed += size
        current_bytes -= size
        
        logger.debug(
            f"[Gemini] Compacted {mime_type} ({size / 1024:.0f}KB) at content[{content_idx}]"
        )
    
    if bytes_removed > 0:
        logger.info(
            f"[Gemini] Compacted {bytes_removed / (1024*1024):.1f}MB of inline data. "
            f"New size: {current_bytes / (1024*1024):.1f}MB"
        )
    
    return compacted, bytes_removed


def prepare_messages_for_gemini(
    messages: List[Any],
    tools: List[Dict[str, Any]],
    include_critical_instruction: bool = True,
    enforce_byte_limit: bool = True
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Prepare messages for Gemini API: filter unavailable tool calls and convert.
    
    This is the main entry point for both streaming and non-streaming requests.
    It combines filter_unavailable_tool_calls and convert_openai_messages_to_gemini.
    
    Also enforces Gemini's 20MB request size limit by compacting old inline data
    if necessary (fallback when Context Engineer isn't enabled or wasn't enough).
    
    Args:
        messages: List of ChatMessage objects
        tools: List of OpenAI-format tool dicts
        include_critical_instruction: Whether to prepend critical function calling instruction
        enforce_byte_limit: Whether to compact inline data if request exceeds 20MB
    
    Returns:
        (system_instruction, contents) tuple for Gemini API
    """
    # Extract available tool names
    available_tool_names = extract_available_tool_names(tools)
    
    # Filter messages to remove/convert tool calls for unavailable tools
    filtered_messages = filter_unavailable_tool_calls(messages, available_tool_names)
    
    # Convert to Gemini format
    system_instruction, contents = convert_openai_messages_to_gemini(
        filtered_messages, include_critical_instruction
    )
    
    # Enforce byte limit if enabled (fallback compaction)
    if enforce_byte_limit and contents:
        contents, bytes_removed = compact_contents_for_byte_limit(contents)
        if bytes_removed > 0:
            logger.warning(
                f"[Gemini] Fallback compaction removed {bytes_removed / (1024*1024):.1f}MB. "
                "Consider enabling Context Engineer plugin for smarter compaction."
            )
    
    return system_instruction, contents


def filter_unavailable_tool_calls(
    messages: List[Any],
    available_tool_names: Set[str]
) -> List[Any]:
    """Filter out tool calls and responses for tools not in available_tool_names.
    
    When switching agents, the conversation history may contain tool calls from
    tools that are no longer available. Gemini will return UNEXPECTED_TOOL_CALL
    if it sees these in the history. This function converts them to text summaries.
    
    Args:
        messages: List of ChatMessage objects
        available_tool_names: Set of tool names currently available (can be empty)
    
    Returns:
        Filtered list of ChatMessage objects with unavailable tools converted to text
    """
    # Check if any messages have tool calls - if not, no filtering needed
    has_tool_calls = any(
        getattr(msg, 'tool_calls', None) or getattr(msg, 'role', None) == 'tool'
        for msg in messages
    )
    if not has_tool_calls:
        return messages
    
    filtered = []
    # Track tool_call_ids from unavailable tools to also filter their responses
    unavailable_call_ids: Set[str] = set()
    
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            # Check if any tool calls are for unavailable tools
            available_calls = []
            unavailable_calls = []
            
            for tc in msg.tool_calls:
                func_name = tc.get("function", {}).get("name", "") if isinstance(tc, dict) else getattr(tc.function, "name", "")
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                
                if func_name in available_tool_names:
                    available_calls.append(tc)
                else:
                    unavailable_calls.append(tc)
                    unavailable_call_ids.add(tc_id)
            
            if unavailable_calls:
                # Convert unavailable tool calls to text summary
                summaries = []
                for tc in unavailable_calls:
                    func_name = tc.get("function", {}).get("name", "") if isinstance(tc, dict) else getattr(tc.function, "name", "")
                    func_args = tc.get("function", {}).get("arguments", "{}") if isinstance(tc, dict) else getattr(tc.function, "arguments", "{}")
                    summaries.append(f"[Previously called {func_name} with args: {func_args}]")
                
                logger.debug(
                    f"[Gemini] Converted {len(unavailable_calls)} unavailable tool calls to text: "
                    f"{[tc.get('function', {}).get('name') if isinstance(tc, dict) else getattr(tc.function, 'name', '') for tc in unavailable_calls]}"
                )
                
                # Create modified message
                new_content = msg.content or ""
                if summaries:
                    summary_text = "\n".join(summaries)
                    new_content = f"{new_content}\n{summary_text}" if new_content else summary_text
                
                # Create new message with only available tool calls using Pydantic's model_copy
                new_msg = msg.model_copy(update={
                    "content": new_content if new_content else None,
                    "tool_calls": available_calls if available_calls else None
                })
                filtered.append(new_msg)
            else:
                filtered.append(msg)
        
        elif msg.role == "tool":
            # Check if this is a response to an unavailable tool call
            tc_id = msg.tool_call_id
            if tc_id in unavailable_call_ids:
                # Skip this response - it's orphaned now
                logger.debug(f"[Gemini] Skipping tool response for unavailable tool (call_id: {tc_id})")
                continue
            filtered.append(msg)
        
        else:
            filtered.append(msg)
    
    return filtered


def filter_unavailable_tool_calls_dict(
    messages: List[Dict[str, Any]],
    available_tool_names: Set[str]
) -> List[Dict[str, Any]]:
    """Filter out tool calls and responses for tools not in available_tool_names.
    
    Dict-based version for batch processing where messages are dicts, not ChatMessage objects.
    
    When switching agents, the conversation history may contain tool calls from
    tools that are no longer available. Gemini will return UNEXPECTED_TOOL_CALL
    if it sees these in the history. This function converts them to text summaries.
    
    Args:
        messages: List of message dicts (OpenAI format)
        available_tool_names: Set of tool names currently available (can be empty)
    
    Returns:
        Filtered list of message dicts with unavailable tools converted to text
    """
    # Check if any messages have tool calls - if not, no filtering needed
    has_tool_calls = any(
        msg.get("tool_calls") or msg.get("role") == "tool"
        for msg in messages
    )
    if not has_tool_calls:
        return messages
    
    filtered: List[Dict[str, Any]] = []
    # Track tool_call_ids from unavailable tools to also filter their responses
    unavailable_call_ids: Set[str] = set()
    
    for msg in messages:
        role = msg.get("role")
        tool_calls = msg.get("tool_calls")
        
        if role == "assistant" and tool_calls:
            # Check if any tool calls are for unavailable tools
            available_calls = []
            unavailable_calls = []
            
            for tc in tool_calls:
                func_name = tc.get("function", {}).get("name", "")
                tc_id = tc.get("id", "")
                
                if func_name in available_tool_names:
                    available_calls.append(tc)
                else:
                    unavailable_calls.append(tc)
                    unavailable_call_ids.add(tc_id)
            
            if unavailable_calls:
                # Convert unavailable tool calls to text summary
                summaries = []
                for tc in unavailable_calls:
                    func_name = tc.get("function", {}).get("name", "")
                    func_args = tc.get("function", {}).get("arguments", "{}")
                    summaries.append(f"[Previously called {func_name} with args: {func_args}]")
                
                logger.debug(
                    f"[Gemini] Batch: Converted {len(unavailable_calls)} unavailable tool calls to text: "
                    f"{[tc.get('function', {}).get('name') for tc in unavailable_calls]}"
                )
                
                # Create modified message dict
                new_content = msg.get("content", "") or ""
                if summaries:
                    summary_text = "\n".join(summaries)
                    new_content = f"{new_content}\n{summary_text}" if new_content else summary_text
                
                # Create new message with only available tool calls
                new_msg = dict(msg)  # Shallow copy
                new_msg["content"] = new_content if new_content else None
                new_msg["tool_calls"] = available_calls if available_calls else None
                filtered.append(new_msg)
            else:
                filtered.append(msg)
        
        elif role == "tool":
            # Check if this is a response to an unavailable tool call
            tc_id = msg.get("tool_call_id", "")
            if tc_id in unavailable_call_ids:
                # Skip this response - it's orphaned now
                logger.debug(f"[Gemini] Batch: Skipping tool response for unavailable tool (call_id: {tc_id})")
                continue
            filtered.append(msg)
        
        else:
            filtered.append(msg)
    
    return filtered


def convert_openai_messages_to_gemini(
    messages: List[Any],
    include_critical_instruction: bool = True
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Convert ChatMessage list to Gemini format.
    
    Args:
        messages: List of ChatMessage objects
        include_critical_instruction: Whether to prepend critical function calling instruction
    
    Returns:
        (system_instruction, contents) tuple where:
        - system_instruction: Merged system messages as single string
        - contents: List of Gemini content dicts with role and parts
    """
    system_instructions: List[str] = []
    contents: List[Dict[str, Any]] = []
    
    # Add critical instruction to prevent MALFORMED_FUNCTION_CALL
    # (Gemini sometimes generates Python code instead of JSON for function calls)
    if include_critical_instruction:
        system_instructions.append(
            "CRITICAL: When calling functions, output the function name exactly as defined. "
            "Do NOT prepend 'default_api.' or any other namespace. "
            "Always generate valid JSON for function arguments. "
            "Properly escape all special characters in JSON strings (quotes, backslashes, newlines)."
        )

    for msg in messages:
        if msg.role == "system":
            # Gemini uses systemInstruction separately - collect ALL system messages
            content = msg.content if isinstance(msg.content, str) else ""
            if content:
                system_instructions.append(content)
                logger.debug(f"Collected system instruction: {len(content)} chars")
            continue

        # Map roles
        role = "user" if msg.role == "user" else "model"

        # Handle tool responses
        if msg.role == "tool":
            # Tool responses use role="tool" (per Gemini SDK docs)
            tool_name = msg.name or "unknown"
            
            # Try to parse as JSON, fallback to string content
            if isinstance(msg.content, str):
                try:
                    result_data = json.loads(msg.content)
                except json.JSONDecodeError:
                    # Content is not valid JSON (e.g., compacted reference like "[Tool:xxx ref:yyy]")
                    result_data = {"result": msg.content}
            else:
                result_data = msg.content
            
            # Convert our error format to Gemini's expected format
            # Our format: {"error": True, "message": "..."}
            # Gemini format: {"error": "..."}
            if isinstance(result_data, dict) and result_data.get("error") is True:
                error_message = result_data.get("message", "Unknown error")
                result_data = {"error": error_message}
                logger.warning(f"[Gemini] DEPRECATED: Tool returned old error format. Converted for {tool_name}: {error_message[:100]}")
            
            # Build parts for the tool response
            # Gemini supports native multimodal in functionResponse
            parts: list[dict[str, Any]] = [{
                "functionResponse": {
                    "name": tool_name,
                    "response": result_data
                }
            }]
            
            # Add multimodal content as inlineData parts (Gemini native support!)
            if msg.multimodal_content:
                from ..utils.multimodal_tool_content import encode_multimodal_item
                for item in msg.multimodal_content:
                    encoded = encode_multimodal_item(item)
                    if encoded:
                        parts.append({
                            "inlineData": {
                                "mimeType": encoded.mime_type,
                                "data": encoded.data
                            }
                        })
                        logger.debug(
                            "[Gemini] Added native multimodal content: %s (%s)",
                            encoded.type, encoded.mime_type
                        )
            
            tool_content: dict[str, Any] = {
                "role": "tool",
                "parts": parts
            }
            contents.append(tool_content)  # type: ignore[arg-type]
            continue

        # Handle assistant with tool_calls
        if msg.role == "assistant" and msg.tool_calls:
            parts = []
            # Add text if present
            if msg.content:
                parts.append({"text": msg.content})
            
            # Add function calls with thought_signature preservation
            for idx, tc in enumerate(msg.tool_calls):
                func = tc.get("function", {})
                func_name = func.get("name", "")
                func_args = func.get("arguments", "{}")
                
                # Parse args if string
                if isinstance(func_args, str):
                    try:
                        func_args = json.loads(func_args)
                    except json.JSONDecodeError:
                        func_args = {}
                
                part = {
                    "functionCall": {
                        "name": func_name,
                        "args": func_args
                    }
                }
                
                # Preserve thoughtSignature if present (required for Gemini 3 Pro)
                # The signature is stored in extra_content.google.thought_signature
                extra_content = tc.get("extra_content", {})
                google_extra = extra_content.get("google", {})
                thought_sig = google_extra.get("thought_signature")
                
                # Also check direct thought_signature field
                if not thought_sig:
                    thought_sig = tc.get("thought_signature")
                
                # Only set thoughtSignature if we actually have one from the original response.
                # 
                # IMPORTANT: Do NOT set skip_thought_signature_validator for ALL function calls!
                # Google's documentation says validation is only for the CURRENT TURN.
                # Setting bypass tokens on historical function calls can cause MALFORMED_FUNCTION_CALL.
                #
                # The bypass token should ONLY be used when:
                # 1. Switching from another model (e.g., DeepSeek to Gemini) mid-conversation
                # 2. AND the function call is in the CURRENT turn (after last user text message)
                # 3. AND there's no original thought_signature
                #
                # For now, we only restore existing signatures. The model-switch case needs
                # more sophisticated turn detection to work properly.
                if thought_sig:
                    part["thoughtSignature"] = thought_sig
                    logger.debug(f"[Gemini] Restored thoughtSignature for {func_name}")
                
                parts.append(part)
            
            contents.append({"role": role, "parts": parts})
            continue

        # Regular message - handle both string and multimodal content
        if isinstance(msg.content, str):
            # Simple text message
            if msg.content:
                contents.append({
                    "role": role,
                    "parts": [{"text": msg.content}]
                })
        elif isinstance(msg.content, list):
            # Multimodal content (text + images/etc.)
            parts = _convert_multimodal_content(msg.content)
            
            if parts:
                contents.append({
                    "role": role,
                    "parts": parts
                })

    # Merge all system instructions (first one is the main prompt, others are context additions)
    system_instruction = None
    if system_instructions:
        system_instruction = "\n\n".join(system_instructions)
        logger.debug(f"Final merged system instruction: {len(system_instruction)} chars from {len(system_instructions)} parts")

    # Merge consecutive tool responses into single content blocks.
    # Gemini requires that when a model makes multiple parallel function calls,
    # all corresponding function_responses must be in a single role="tool" content block.
    # If they're separate, Gemini returns 400 INVALID_ARGUMENT with
    # "Mismatched function_call/function_response pairs".
    contents = _merge_consecutive_tool_responses(contents)

    # Merge consecutive user messages into single content blocks.
    # Gemini does NOT allow multiple consecutive user messages (unlike OpenAI).
    # See: https://github.com/google/generative-ai-docs/issues/209
    # This can happen when auto-injected "Continue" messages accumulate in session history.
    contents = _merge_consecutive_same_role_messages(contents, "user")

    return system_instruction, contents


def _merge_consecutive_tool_responses(
    contents: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Merge consecutive tool response content blocks into single blocks.
    
    Gemini's API requires that when a model makes multiple parallel function calls,
    all the corresponding function_responses must be in a single Content block
    with role="tool" and multiple functionResponse parts.
    
    Args:
        contents: List of Gemini content dicts
    
    Returns:
        List with consecutive tool responses merged
    """
    if not contents:
        return contents
    
    merged: List[Dict[str, Any]] = []
    i = 0
    
    while i < len(contents):
        current = contents[i]
        
        # Check if this is a tool response
        if current.get("role") == "tool":
            # Collect all consecutive tool responses
            tool_parts: List[Dict[str, Any]] = list(current.get("parts", []))
            j = i + 1
            
            while j < len(contents) and contents[j].get("role") == "tool":
                # Merge parts from consecutive tool responses
                tool_parts.extend(contents[j].get("parts", []))
                j += 1
            
            # If we merged multiple tool responses, log it
            if j > i + 1:
                logger.debug(
                    "[Gemini] Merged %d consecutive tool responses into single content block "
                    "with %d parts",
                    j - i, len(tool_parts)
                )
            
            # Add merged tool response
            merged.append({
                "role": "tool",
                "parts": tool_parts
            })
            i = j
        else:
            # Not a tool response, keep as-is
            merged.append(current)
            i += 1
    
    return merged


def _merge_consecutive_same_role_messages(
    contents: List[Dict[str, Any]],
    role: str
) -> List[Dict[str, Any]]:
    """Merge consecutive messages with the same role into single content blocks.
    
    Gemini does NOT allow multiple consecutive user messages (unlike OpenAI).
    This merges them by combining all parts from consecutive messages.
    
    Note: Parts are kept in their original order (text interleaved with images/audio).
    When merging multiple messages, their parts are concatenated in sequence.
    
    Args:
        contents: List of Gemini content dicts
        role: Role to merge (e.g., "user")
    
    Returns:
        List with consecutive same-role messages merged
    """
    if not contents:
        return contents
    
    merged: List[Dict[str, Any]] = []
    i = 0
    
    while i < len(contents):
        current = contents[i]
        
        # Check if this matches the target role
        if current.get("role") == role:
            # Collect all consecutive messages with same role
            j = i + 1
            while j < len(contents) and contents[j].get("role") == role:
                j += 1
            
            # If only one message, keep as-is (preserves interleaved text/images)
            if j == i + 1:
                merged.append(current)
                i = j
                continue
            
            # Multiple consecutive messages - merge their parts
            # Each message's parts stay in order, messages concatenated with separator
            combined_parts: List[Dict[str, Any]] = []
            
            for msg_idx in range(i, j):
                msg_parts = contents[msg_idx].get("parts", [])
                
                # Add separator between messages (as text part) if not the first
                if msg_idx > i and combined_parts:
                    # Check if last part and first new part are both text - merge them
                    last_is_text = combined_parts and "text" in combined_parts[-1]
                    first_is_text = msg_parts and "text" in msg_parts[0]
                    
                    if last_is_text and first_is_text:
                        # Merge text with separator
                        combined_parts[-1]["text"] += "\n\n" + msg_parts[0]["text"]
                        msg_parts = msg_parts[1:]  # Skip first part, already merged
                    elif last_is_text:
                        # Add separator to last text part
                        combined_parts[-1]["text"] += "\n\n"
                
                combined_parts.extend(msg_parts)
            
            logger.debug(
                "[Gemini] Merged %d consecutive '%s' messages into single content block with %d parts",
                j - i, role, len(combined_parts)
            )
            
            if combined_parts:
                merged.append({
                    "role": role,
                    "parts": combined_parts
                })
            i = j
        else:
            # Different role, keep as-is
            merged.append(current)
            i += 1
    
    return merged


def _convert_multimodal_content(content_list: List[Any]) -> List[Dict[str, Any]]:
    """Convert multimodal content (text + images) to Gemini parts format.
    
    Args:
        content_list: List of content items (dicts or Pydantic models)
    
    Returns:
        List of Gemini part dicts
    """
    parts: List[Dict[str, Any]] = []
    
    for item in content_list:
        # Handle dict format (direct JSON)
        if isinstance(item, dict):
            content_type = item.get("type")
            
            if content_type == "text":
                text_val = item.get("text", "")
                if text_val:
                    parts.append({"text": text_val})
            
            elif content_type in ("image", "image_url"):
                # Extract image data
                image_url = item.get("image_url")
                image_source = item.get("source")
                
                # Handle OpenAI format: image_url can be string or dict with "url" key
                data_url = None
                if isinstance(image_url, str):
                    data_url = image_url
                elif isinstance(image_url, dict):
                    data_url = image_url.get("url")
                
                # Handle Anthropic format: source with base64 data
                if not data_url and image_source:
                    if isinstance(image_source, dict):
                        source_type = image_source.get("type")
                        if source_type == "base64":
                            media_type = image_source.get("media_type", "image/jpeg")
                            data = image_source.get("data", "")
                            if data:
                                data_url = f"data:{media_type};base64,{data}"
                        elif source_type == "url":
                            data_url = image_source.get("url")
                
                # Convert data URL to Gemini inlineData format
                if data_url:
                    if data_url.startswith("data:"):
                        # Parse data URL: data:image/png;base64,iVBORw0KG...
                        try:
                            header, base64_data = data_url.split(",", 1)
                            mime_type = header.split(":")[1].split(";")[0]
                            parts.append({
                                "inlineData": {
                                    "mimeType": mime_type,
                                    "data": base64_data
                                }
                            })
                        except (ValueError, IndexError) as e:
                            logger.warning(f"[Gemini] Failed to parse data URL: {e}")
                    else:
                        # External URL - Gemini doesn't support external URLs directly
                        # Would need to download and convert to base64
                        logger.warning(f"[Gemini] External image URLs not yet supported: {data_url[:100]}")
            
            elif content_type == "audio":
                # Extract audio data - same format as image but with audio_url
                audio_url = item.get("audio_url")
                media_type = item.get("media_type", "audio/wav")
                
                if audio_url and audio_url.startswith("data:"):
                    try:
                        header, base64_data = audio_url.split(",", 1)
                        # Use media_type from header or fallback to provided
                        parsed_mime = header.split(":")[1].split(";")[0]
                        parts.append({
                            "inlineData": {
                                "mimeType": parsed_mime or media_type,
                                "data": base64_data
                            }
                        })
                    except (ValueError, IndexError) as e:
                        logger.warning(f"[Gemini] Failed to parse audio data URL: {e}")
                elif audio_url:
                    logger.warning(f"[Gemini] External audio URLs not supported: {audio_url[:100]}")
        
        # Handle Pydantic model objects (TextContent, ImageContent, etc.)
        elif hasattr(item, "type"):
            if item.type == "text" and hasattr(item, "text"):
                if item.text:
                    parts.append({"text": item.text})
            
            elif item.type in ("image", "image_url"):
                # Extract from Pydantic ImageContent model
                image_url = getattr(item, "image_url", None)
                image_source = getattr(item, "source", None)
                
                data_url = None
                if isinstance(image_url, str):
                    data_url = image_url
                elif isinstance(image_url, dict):
                    data_url = image_url.get("url")
                
                if not data_url and image_source:
                    if hasattr(image_source, "type"):
                        if image_source.type == "base64":
                            media_type = getattr(image_source, "media_type", "image/jpeg")
                            data = getattr(image_source, "data", "")
                            if data:
                                data_url = f"data:{media_type};base64,{data}"
                        elif image_source.type == "url":
                            data_url = getattr(image_source, "url", None)
                
                # Convert to Gemini format
                if data_url:
                    if data_url.startswith("data:"):
                        try:
                            header, base64_data = data_url.split(",", 1)
                            mime_type = header.split(":")[1].split(";")[0]
                            parts.append({
                                "inlineData": {
                                    "mimeType": mime_type,
                                    "data": base64_data
                                }
                            })
                        except (ValueError, IndexError) as e:
                            logger.warning(f"[Gemini] Failed to parse data URL from Pydantic model: {e}")
                    else:
                        logger.warning(f"[Gemini] External image URLs not yet supported: {data_url[:100]}")
            
            elif item.type == "audio":
                # Extract from Pydantic AudioContent model
                audio_url = getattr(item, "audio_url", None)
                media_type = getattr(item, "media_type", "audio/wav")
                
                if audio_url and audio_url.startswith("data:"):
                    try:
                        header, base64_data = audio_url.split(",", 1)
                        parsed_mime = header.split(":")[1].split(";")[0]
                        parts.append({
                            "inlineData": {
                                "mimeType": parsed_mime or media_type,
                                "data": base64_data
                            }
                        })
                    except (ValueError, IndexError) as e:
                        logger.warning(f"[Gemini] Failed to parse audio data URL from Pydantic model: {e}")
                elif audio_url:
                    logger.warning(f"[Gemini] External audio URLs not supported: {audio_url[:100]}")
            
            elif item.type == "text_file":
                # Extract from Pydantic TextFileContent model - convert to text
                content = getattr(item, "content", "")
                name = getattr(item, "name", None) or "file"
                if content:
                    parts.append({"text": f"[File: {name}]\n{content}"})
    
    return parts


def convert_openai_tools_to_gemini(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI tool schema to Gemini function declarations.
    
    Uses sanitize_schema_for_gemini() to clean schemas for Gemini API compatibility.
    
    Args:
        tools: List of OpenAI tool definitions with type=function
    
    Returns:
        List of Gemini function declaration dicts
    """
    function_declarations: List[Dict[str, Any]] = []

    for tool in tools:
        if tool.get("type") != "function":
            continue

        func = tool.get("function", {})
        declaration = {
            "name": func.get("name", ""),
            "description": func.get("description", ""),
        }

        # Add parameters if present, sanitize for Gemini compatibility
        params = func.get("parameters", {})
        if params:
            declaration["parameters"] = sanitize_schema_for_gemini(params)

        function_declarations.append(declaration)

    return function_declarations


def sanitize_schema_for_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Remove JSON Schema keywords not supported by Gemini API.
    
    This is the canonical schema sanitization function used by all Gemini clients
    (SDK, HTTP, and Batch). It recursively processes schemas to remove unsupported
    constructs while preserving essential type information.
    
    Gemini's Function Declaration schema doesn't support:
    - oneOf, anyOf, allOf (JSON Schema composition) - flattened to first option
    - $ref (references)
    - additionalProperties (Gemini uses strict schemas)
    - default (default values)
    - examples
    - $schema, $id (meta keywords)
    - definitions, $defs (schema definitions)
    - patternProperties, unevaluatedProperties
    - if, then, else, not (conditional schemas)
    
    Args:
        schema: JSON schema dict to sanitize
        
    Returns:
        Sanitized schema dict safe for Gemini API
    """
    if not isinstance(schema, dict):
        return schema
    
    # Keywords that Gemini doesn't support at all
    unsupported_keywords = {
        "oneOf", "anyOf", "allOf", "$ref", 
        "additionalProperties", "default", "examples",
        "$schema", "$id", "definitions", "$defs",
        "patternProperties", "unevaluatedProperties",
        "if", "then", "else", "not",
        "format", "title",  # Also exclude for cleaner schemas
    }
    
    result: Dict[str, Any] = {}
    
    for key, value in schema.items():
        # Skip completely unsupported keywords
        if key in unsupported_keywords:
            # For oneOf/anyOf/allOf, merge first option into result
            # This preserves 'type' and other essential fields
            if key in ("oneOf", "anyOf", "allOf"):
                if isinstance(value, list) and len(value) > 0:
                    first_option = value[0]
                    if isinstance(first_option, dict):
                        for opt_key, opt_val in first_option.items():
                            if opt_key not in result and opt_key not in unsupported_keywords:
                                result[opt_key] = sanitize_schema_for_gemini(opt_val)
            continue
        elif key == "properties" and isinstance(value, dict):
            # Recursively sanitize each property
            result[key] = {
                k: sanitize_schema_for_gemini(v) 
                for k, v in value.items()
            }
        elif key == "items" and isinstance(value, dict):
            # Recursively sanitize array items schema
            result[key] = sanitize_schema_for_gemini(value)
        else:
            result[key] = value
    
    return result


# Backwards compatibility alias
def clean_schema_for_gemini(
    schema: Dict[str, Any],
    flatten_complex_schemas: bool = True  # Changed default to True
) -> Dict[str, Any]:
    """Remove fields that Gemini doesn't support from JSON schema.
    
    DEPRECATED: Use sanitize_schema_for_gemini() instead.
    This function is kept for backwards compatibility.
    
    Args:
        schema: JSON schema dict to clean
        flatten_complex_schemas: Ignored, always flattens (for backwards compat)
    
    Returns:
        Cleaned schema dict
    """
    return sanitize_schema_for_gemini(schema)


# =============================================================================
# Streaming Utilities: Loop Detection, Defensive Retry, Usage Extraction
# =============================================================================

# Constants for loop detection
DEFAULT_MAX_RECENT_CHUNKS = 20  # Keep last N chunks for pattern detection
DEFAULT_MIN_REPETITIONS_FOR_LOOP = 5  # Need N identical chunks to confirm loop
DEFAULT_MAX_CONSECUTIVE_THOUGHT_CHUNKS = 100  # ~200-300s of pure thinking = likely stuck


class StreamingLoopDetector:
    """Detect repetitive patterns in streaming text that indicate infinite loops.
    
    Used by Gemini clients to detect when the model gets stuck repeating the
    same text in thinking mode, which often leads to MAX_TOKENS exhaustion.
    
    Usage:
        detector = StreamingLoopDetector()
        for chunk in stream:
            if detector.check_for_loop(chunk.text):
                raise RuntimeError("Loop detected")
    """
    
    def __init__(
        self,
        max_recent_chunks: int = DEFAULT_MAX_RECENT_CHUNKS,
        min_repetitions: int = DEFAULT_MIN_REPETITIONS_FOR_LOOP,
        min_chunk_length: int = 20
    ):
        """Initialize loop detector.
        
        Args:
            max_recent_chunks: Number of recent chunks to track
            min_repetitions: Minimum repetitions to confirm loop
            min_chunk_length: Minimum chunk length to consider (avoids false positives)
        """
        self.max_recent_chunks = max_recent_chunks
        self.min_repetitions = min_repetitions
        self.min_chunk_length = min_chunk_length
        self.recent_chunks: List[str] = []
    
    def check_for_loop(self, text: str) -> Optional[Tuple[str, int]]:
        """Check if text indicates a repetitive loop pattern.
        
        Args:
            text: Text chunk to check
            
        Returns:
            Tuple of (repeated_text, count) if loop detected, None otherwise
        """
        if len(text) < self.min_chunk_length:
            return None
        
        # Normalize text for comparison
        normalized = text.strip().lower()
        self.recent_chunks.append(normalized)
        
        # Keep only last N chunks
        if len(self.recent_chunks) > self.max_recent_chunks:
            self.recent_chunks.pop(0)
        
        # Check for repetitive pattern
        if len(self.recent_chunks) >= self.min_repetitions:
            count = self.recent_chunks.count(normalized)
            if count >= self.min_repetitions:
                return (text[:80] + "..." if len(text) > 80 else text, count)
        
        return None
    
    def reset(self) -> None:
        """Reset the detector state."""
        self.recent_chunks = []


class ThinkingProgressTracker:
    """Track progress during streaming to detect infinite thinking loops.
    
    Tracks whether chunks contain "progress" (non-thought content or tool calls)
    and detects when the model is stuck producing only thought content.
    
    Usage:
        tracker = ThinkingProgressTracker()
        for chunk in stream:
            has_progress = chunk.has_tool_call or chunk.has_text_content
            if tracker.check_stuck(has_progress):
                raise RuntimeError("Stuck in thinking loop")
    """
    
    def __init__(
        self,
        max_consecutive_no_progress: int = DEFAULT_MAX_CONSECUTIVE_THOUGHT_CHUNKS
    ):
        """Initialize progress tracker.
        
        Args:
            max_consecutive_no_progress: Max chunks without progress before stuck
        """
        self.max_consecutive = max_consecutive_no_progress
        self.consecutive_no_progress = 0
    
    def check_stuck(self, has_progress: bool, has_finish_reason: bool = False) -> bool:
        """Check if stream is stuck without progress.
        
        Args:
            has_progress: Whether this chunk had meaningful progress
            has_finish_reason: Whether this chunk had a finish_reason
            
        Returns:
            True if stuck (exceeded max consecutive no-progress chunks)
        """
        if has_progress or has_finish_reason:
            self.consecutive_no_progress = 0
            return False
        
        self.consecutive_no_progress += 1
        return self.consecutive_no_progress >= self.max_consecutive
    
    def reset(self) -> None:
        """Reset the tracker state."""
        self.consecutive_no_progress = 0


def extract_usage_from_metadata(usage_metadata: Any, use_camel_case: bool = False) -> Dict[str, Any]:
    """Extract usage info from Gemini metadata in OpenAI-compatible format.
    
    Handles both SDK objects (snake_case attributes) and HTTP responses 
    (snake_case or camelCase keys).
    
    Args:
        usage_metadata: Usage metadata from Gemini (SDK object or dict)
        use_camel_case: If True, expect camelCase keys (HTTP API format)
        
    Returns:
        OpenAI-compatible usage dict with prompt_tokens, completion_tokens, 
        total_tokens, and optionally prompt_tokens_details.cached_tokens
    """
    if not usage_metadata:
        return {}
    
    # Handle SDK objects (have attributes)
    if hasattr(usage_metadata, 'prompt_token_count'):
        usage = {
            "prompt_tokens": getattr(usage_metadata, 'prompt_token_count', 0),
            "completion_tokens": getattr(usage_metadata, 'candidates_token_count', 0),
            "total_tokens": getattr(usage_metadata, 'total_token_count', 0),
        }
        cached_tokens = getattr(usage_metadata, 'cached_content_token_count', 0)
    elif isinstance(usage_metadata, dict):
        # Handle HTTP responses - try both snake_case and camelCase
        if use_camel_case:
            usage = {
                "prompt_tokens": usage_metadata.get("promptTokenCount", 0),
                "completion_tokens": usage_metadata.get("candidatesTokenCount", 0),
                "total_tokens": usage_metadata.get("totalTokenCount", 0),
            }
            cached_tokens = usage_metadata.get("cachedContentTokenCount", 0)
        else:
            # Try snake_case first, fall back to camelCase
            usage = {
                "prompt_tokens": usage_metadata.get("prompt_token_count", 
                    usage_metadata.get("promptTokenCount", 0)),
                "completion_tokens": usage_metadata.get("candidates_token_count",
                    usage_metadata.get("candidatesTokenCount", 0)),
                "total_tokens": usage_metadata.get("total_token_count",
                    usage_metadata.get("totalTokenCount", 0)),
            }
            cached_tokens = usage_metadata.get("cached_content_token_count",
                usage_metadata.get("cachedContentTokenCount", 0))
    else:
        return {}
    
    # Add cached tokens if present
    if cached_tokens and cached_tokens > 0:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    
    return usage


def should_disable_thinking_on_retry(
    attempt: int,
    include_thoughts: Optional[bool]
) -> bool:
    """Determine if thinking should be disabled for a retry attempt.
    
    Implements defensive retry strategy: on ANY retry (attempt > 0),
    disable thinking entirely. Thinking loops are often the root cause
    of issues like MAX_TOKENS exhaustion, infinite loops, etc.
    
    Args:
        attempt: Current retry attempt (0 = first try)
        include_thoughts: Whether thinking was originally enabled
        
    Returns:
        True if thinking should be disabled
    """
    return attempt > 0 and bool(include_thoughts)
