"""Shared utilities for OpenAI API conversion.

Provides common conversion logic used by OpenAIClient, HTTPXClient,
and OpenAIBatchClient to normalize message content for OpenAI format.

OpenAI Chat Completions API only accepts specific content types:
- {"type": "text", "text": "..."}
- {"type": "image_url", "image_url": {"url": "...", "detail": "..."}}

Extra fields (like 'name' from ImageContent) will cause API errors.
Audio and video content types are not supported.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def normalize_content_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a single content item for OpenAI API.
    
    Args:
        item: Content item dict with 'type' field
        
    Returns:
        Normalized item dict, or None if item should be skipped
    """
    item_type = item.get("type", "")
    
    # Skip audio/video - not supported by Chat Completions API
    if item_type in ("audio", "input_audio", "video"):
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


def normalize_content_list(content: List[Any]) -> List[Dict[str, Any]]:
    """Normalize a list of content items for OpenAI API.
    
    Args:
        content: List of content items (dicts or strings)
        
    Returns:
        List of normalized content items
    """
    result = []
    for item in content:
        if isinstance(item, dict):
            normalized = normalize_content_item(item)
            if normalized is not None:
                result.append(normalized)
        elif isinstance(item, str):
            result.append({"type": "text", "text": item})
        else:
            # Try to convert to string
            result.append({"type": "text", "text": str(item)})
    return result


def normalize_message_content(content: Any) -> Any:
    """Normalize message content for OpenAI API.
    
    Handles both string content and list content.
    
    Args:
        content: Message content (str, list, or other)
        
    Returns:
        Normalized content - string, list of dicts, or empty string
    """
    if isinstance(content, str):
        return content
    
    if isinstance(content, list):
        normalized = normalize_content_list(content)
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
