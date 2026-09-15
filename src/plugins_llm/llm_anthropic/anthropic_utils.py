"""Shared utilities for Anthropic API conversion.

Provides common conversion logic used by AnthropicClient and AnthropicBatchClient
to normalize message content for Anthropic format.

Anthropic Messages API accepts:
- {"type": "text", "text": "..."}
- {"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}
- {"type": "image", "source": {"type": "url", "url": "..."}}

Audio and video content types are not supported.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def normalize_content_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a single content item for Anthropic API.
    
    Args:
        item: Content item dict
        
    Returns:
        Normalized item dict for Anthropic, or None if should be skipped
    """
    item_type = item.get("type", "")
    
    # Skip audio/video - Anthropic doesn't support them
    if item_type in ("audio", "input_audio", "video"):
        logger.debug(f"Filtering {item_type} content - not supported by Anthropic")
        return None
    
    # Convert text_file to text
    if item_type == "text_file":
        name = item.get("name") or "file"
        content = item.get("content", "")
        return {"type": "text", "text": f"[File: {name}]\n{content}"}
    
    # Text content - pass through
    if item_type == "text":
        return {"type": "text", "text": item.get("text", "")}
    
    # Image content - convert to Anthropic format
    if item_type in ("image", "image_url"):
        return _convert_image_content(item)
    
    # Pass through unknown types
    return item


def _convert_image_content(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert image content to Anthropic format.
    
    Args:
        item: Image content item (OpenAI or Anthropic format)
        
    Returns:
        Anthropic-formatted image dict, or None if conversion fails
    """
    item_type = item.get("type", "")
    
    # Handle image_url format (OpenAI style)
    if item_type == "image_url" or item.get("image_url"):
        image_url = item.get("image_url", {})
        url = image_url if isinstance(image_url, str) else image_url.get("url", "")
        
        if url.startswith("data:"):
            # Parse data URL: data:image/jpeg;base64,/9j/4AAQ...
            try:
                header, data = url.split(",", 1)
                media_type = header.split(":")[1].split(";")[0]
                return {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data
                    }
                }
            except (ValueError, IndexError):
                logger.warning(f"Failed to parse data URL: {url[:50]}...")
                return None
        elif url:
            # External URL
            return {
                "type": "image",
                "source": {
                    "type": "url",
                    "url": url
                }
            }
    
    # Handle source format (already Anthropic style)
    if item_type == "image":
        source = item.get("source", {})
        source_type = source.get("type", "")
        
        if source_type == "base64":
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": source.get("media_type", "image/jpeg"),
                    "data": source.get("data", "")
                }
            }
        elif source_type == "url":
            return {
                "type": "image",
                "source": {
                    "type": "url",
                    "url": source.get("url", "")
                }
            }
    
    return None


def usage_to_openai(usage: Any) -> Dict[str, Any]:
    """Map Anthropic usage (SDK object or JSON dict) onto OpenAI semantics.

    Anthropic's ``input_tokens`` counts only the input that was neither read
    from nor written to the cache; reads and writes arrive beside it. Every
    consumer (pricing, context usage tracker, compaction gates, CLI footer)
    expects ``prompt_tokens`` to be the WHOLE input with reads and writes as
    parts of it -- the format OpenRouter delivers for the same models.

    Every field may be None: MessageDeltaUsage declares them Optional, and a
    batch result line can carry nulls.
    """
    if isinstance(usage, dict):
        get = usage.get
    else:
        def get(name: str) -> Any:
            return getattr(usage, name, None)
    uncached = get("input_tokens") or 0
    read = get("cache_read_input_tokens") or 0
    write = get("cache_creation_input_tokens") or 0
    output = get("output_tokens") or 0
    prompt = uncached + read + write
    result: Dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
    }
    if read or write:
        result["prompt_tokens_details"] = {
            "cached_tokens": read,
            "cache_creation_tokens": write,
        }
    return result


def normalize_content_list(content: List[Any]) -> List[Dict[str, Any]]:
    """Normalize a list of content items for Anthropic API.
    
    Args:
        content: List of content items
        
    Returns:
        List of normalized content items for Anthropic
    """
    result = []
    for item in content:
        if isinstance(item, str):
            result.append({"type": "text", "text": item})
        elif isinstance(item, dict):
            normalized = normalize_content_item(item)
            if normalized is not None:
                result.append(normalized)
    return result
