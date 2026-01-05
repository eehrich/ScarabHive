"""Shared utilities for Gemini API conversion.

Provides common conversion logic used by both GeminiClient (HTTP-based) 
and GeminiSDKClient (SDK-based) to convert OpenAI format to Gemini format.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


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
            
            content: dict[str, Any] = {
                "role": "tool",
                "parts": parts
            }
            contents.append(content)  # type: ignore[arg-type]
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
                
                if thought_sig:
                    part["thoughtSignature"] = thought_sig
                    logger.debug(f"[Gemini] Restored thoughtSignature for {func_name}")
                # IMPORTANT: Do NOT set skip_thought_signature_validator for historical function calls
                # Gemini will handle this automatically. Setting it explicitly can cause
                # MALFORMED_FUNCTION_CALL errors when the model tries to validate historical calls.
                
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

    return system_instruction, contents


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

        # Add parameters if present, but clean them for Gemini compatibility
        params = func.get("parameters", {})
        if params:
            # Deep copy to avoid modifying original
            clean_params = clean_schema_for_gemini(params)
            declaration["parameters"] = clean_params

        function_declarations.append(declaration)

    return function_declarations


def clean_schema_for_gemini(
    schema: Dict[str, Any],
    flatten_complex_schemas: bool = False
) -> Dict[str, Any]:
    """Remove fields that Gemini doesn't support from JSON schema.
    
    Args:
        schema: JSON schema dict to clean
        flatten_complex_schemas: If True, flatten oneOf/anyOf/allOf constructs
            (required for SDK compatibility, optional for HTTP API)
    
    Returns:
        Cleaned schema dict
    """
    if not isinstance(schema, dict):
        return schema
    
    # Create a copy to avoid modifying original
    cleaned: Dict[str, Any] = {}
    
    # Fields to exclude (Gemini doesn't support these OpenAI-specific fields)
    exclude_fields = {"additionalProperties", "$schema", "$defs", "definitions"}
    
    # Additional fields for SDK compatibility
    if flatten_complex_schemas:
        exclude_fields.update({"default", "examples", "format", "title"})
    
    for key, value in schema.items():
        if key in exclude_fields:
            continue
        
        # Handle schema composition (oneOf/anyOf/allOf)
        if key in ("oneOf", "anyOf", "allOf"):
            if flatten_complex_schemas and isinstance(value, list) and len(value) > 0:
                # Flatten: use first option and merge into parent
                first_option = value[0]
                if isinstance(first_option, dict):
                    for opt_key, opt_value in first_option.items():
                        if opt_key not in cleaned:
                            cleaned[opt_key] = clean_schema_for_gemini(
                                opt_value, flatten_complex_schemas
                            ) if isinstance(opt_value, dict) else opt_value
            # If not flattening, just skip these fields
            continue
            
        if isinstance(value, dict):
            cleaned[key] = clean_schema_for_gemini(value, flatten_complex_schemas)
        elif isinstance(value, list):
            cleaned[key] = [
                clean_schema_for_gemini(item, flatten_complex_schemas) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            cleaned[key] = value
    
    return cleaned
