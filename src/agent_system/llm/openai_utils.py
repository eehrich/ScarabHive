"""Shared utilities for OpenAI API conversion.

Provides common conversion logic used by OpenAIClient, HTTPXClient,
and OpenAIBatchClient to normalize message content for OpenAI format.

OpenAI Chat Completions API only accepts specific content types:
- {"type": "text", "text": "..."}
- {"type": "image_url", "image_url": {"url": "...", "detail": "..."}}

Extra fields (like 'name' from ImageContent) will cause API errors.
Audio and video content types are not supported by standard OpenAI,
but may be supported when routing to other providers (e.g., OpenRouter -> Gemini).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _convert_audio_to_input_audio(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert audio content to OpenRouter input_audio format.
    
    OpenRouter/Gemini expects:
        {"type": "input_audio", "input_audio": {"data": "base64...", "format": "mp3"}}
    
    Our AudioContent has:
        {"type": "audio", "audio_url": "data:audio/mp3;base64,XXXXX", ...}
    
    Args:
        item: Audio content item with audio_url
        
    Returns:
        Converted input_audio format item
    """
    audio_url = item.get("audio_url", "")
    
    # Handle data URL format: data:audio/mp3;base64,XXXXX
    if audio_url and audio_url.startswith("data:"):
        try:
            # Parse: data:audio/mp3;base64,XXXXX
            header, base64_data = audio_url.split(",", 1)
            # Extract mime type: audio/mp3 from data:audio/mp3;base64
            mime_part = header.replace("data:", "").split(";")[0]
            # Get format: mp3 from audio/mp3
            audio_format = mime_part.split("/")[-1] if "/" in mime_part else "wav"
            
            # Normalize format names
            format_map = {"mpeg": "mp3", "x-wav": "wav", "wave": "wav"}
            audio_format = format_map.get(audio_format, audio_format)
            
            logger.debug(f"Converted audio to input_audio format: {audio_format}")
            return {
                "type": "input_audio",
                "input_audio": {
                    "data": base64_data,
                    "format": audio_format
                }
            }
        except (ValueError, IndexError) as e:
            logger.warning(f"Failed to parse audio_url: {e}")
            # Fall through to return original item
    
    # If already in input_audio format, return as-is
    if item.get("type") == "input_audio":
        return item
    
    # Fallback: return original (may not work)
    logger.warning("Could not convert audio content, returning as-is")
    return item


def normalize_content_item(
    item: Dict[str, Any],
    allow_audio: bool = False,
    allow_video: bool = False
) -> Optional[Dict[str, Any]]:
    """Normalize a single content item for OpenAI API.
    
    Args:
        item: Content item dict with 'type' field
        allow_audio: If True, keep audio content (for Gemini via OpenRouter)
        allow_video: If True, keep video content (for Gemini via OpenRouter)
        
    Returns:
        Normalized item dict, or None if item should be skipped
    """
    item_type = item.get("type", "")
    
    # Handle audio content based on capability
    if item_type in ("audio", "input_audio"):
        if allow_audio:
            # Convert to input_audio format for OpenRouter/Gemini
            logger.debug(f"Converting {item_type} content to input_audio format")
            return _convert_audio_to_input_audio(item)
        else:
            logger.debug(f"Filtering {item_type} content - not supported by OpenAI Chat Completions API")
            return None
    
    # Handle video content based on capability
    if item_type == "video":
        if allow_video:
            logger.debug(f"Keeping {item_type} content - model supports video input")
            return item
        else:
            logger.debug(f"Filtering {item_type} content - not supported by OpenAI Chat Completions API")
            return None
    
    # Convert text_file to regular text with filename header
    if item_type == "text_file":
        name = item.get("name") or "file"
        content = item.get("content", "")
        return {"type": "text", "text": f"[File: {name}]\n{content}"}
    
    # Normalize image content - only keep accepted keys
    if item_type in ("image", "image_url"):
        image_url = item.get("image_url")
        if image_url:
            if isinstance(image_url, str):
                image_url = {"url": image_url}
            elif isinstance(image_url, dict):
                # Only keep 'url' and 'detail' keys - remove 'name' etc.
                image_url = {k: v for k, v in image_url.items() if k in ("url", "detail")}
            return {"type": "image_url", "image_url": image_url}
        return None
    
    # Normalize text content
    if item_type == "text":
        return {"type": "text", "text": item.get("text", "")}
    
    # Pass through unknown types as-is (may cause API error)
    return item


def normalize_content_list(
    content: List[Any],
    allow_audio: bool = False,
    allow_video: bool = False
) -> List[Dict[str, Any]]:
    """Normalize a list of content items for OpenAI API.
    
    Args:
        content: List of content items (dicts or strings)
        allow_audio: If True, keep audio content
        allow_video: If True, keep video content
        
    Returns:
        List of normalized content items
    """
    result = []
    for item in content:
        if isinstance(item, dict):
            normalized = normalize_content_item(item, allow_audio=allow_audio, allow_video=allow_video)
            if normalized is not None:
                result.append(normalized)
        elif isinstance(item, str):
            result.append({"type": "text", "text": item})
        else:
            # Try to convert to string
            result.append({"type": "text", "text": str(item)})
    return result


def normalize_message_content(
    content: Any,
    allow_audio: bool = False,
    allow_video: bool = False
) -> Any:
    """Normalize message content for OpenAI API.
    
    Handles both string content and list content.
    
    Args:
        content: Message content (str, list, or other)
        allow_audio: If True, keep audio content (for Gemini via OpenRouter)
        allow_video: If True, keep video content (for Gemini via OpenRouter)
        
    Returns:
        Normalized content - string, list of dicts, or empty string
    """
    if isinstance(content, str):
        return content
    
    if isinstance(content, list):
        normalized = normalize_content_list(content, allow_audio=allow_audio, allow_video=allow_video)
        if not normalized:
            return ""
        # If only one text item, simplify to string
        if len(normalized) == 1 and normalized[0].get("type") == "text":
            return normalized[0].get("text", "")
        return normalized
    
    # Non-string, non-list content - convert to string
    if content is not None:
        return str(content)
    return ""


def normalize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize a list of messages for OpenAI API.
    
    This is the main entry point for batch processing.
    
    Args:
        messages: List of message dicts
        
    Returns:
        List of messages with normalized content
    """
    result = []
    for msg in messages:
        msg_copy = dict(msg)
        content = msg_copy.get("content")
        
        if isinstance(content, list):
            normalized = normalize_content_list(content)
            msg_copy["content"] = normalized if normalized else ""
        
        result.append(msg_copy)
    
    return result
