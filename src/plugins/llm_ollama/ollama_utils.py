"""Utility functions for Ollama API content normalization.

Ollama uses a different format than OpenAI/Anthropic:
- Images are in a separate 'images' field (base64 strings)
- Content is always a string, not an array
- No support for audio/video

Format:
{
    "role": "user",
    "content": "What is in this image?",
    "images": ["<base64-encoded-data>"]
}
"""
from typing import Any, Optional


def extract_base64_from_data_url(data_url: str) -> Optional[str]:
    """Extract base64 data from a data URL.
    
    Args:
        data_url: Data URL like "data:image/jpeg;base64,/9j/4AAQ..."
        
    Returns:
        Base64 data string or None if invalid
    """
    if not data_url.startswith("data:"):
        return None
    
    try:
        # Format: data:image/jpeg;base64,<data>
        _, data_part = data_url.split(",", 1)
        return data_part
    except (ValueError, IndexError):
        return None


def extract_images_from_content(content: Any) -> tuple[str, list[str]]:
    """Extract images and text from multimodal content.
    
    Ollama expects images in a separate 'images' field as base64 strings.
    
    Args:
        content: Message content - string or list of content items
        
    Returns:
        Tuple of (text_content, list_of_base64_images)
    """
    if isinstance(content, str):
        return content, []
    
    if not isinstance(content, list):
        return str(content) if content else "", []
    
    text_parts = []
    images = []
    
    for item in content:
        if isinstance(item, str):
            text_parts.append(item)
            continue
            
        if not isinstance(item, dict):
            continue
            
        item_type = item.get("type", "")
        
        if item_type == "text":
            text = item.get("text", "")
            if text:
                text_parts.append(text)
                
        elif item_type == "text_file":
            # Convert text_file to text with filename header
            name = item.get("name") or "file"
            file_content = item.get("content", "")
            text_parts.append(f"[File: {name}]\n{file_content}")
            
        elif item_type == "image_url":
            # Extract base64 from data URL or skip regular URLs
            image_url = item.get("image_url", {})
            url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url)
            
            if url.startswith("data:"):
                base64_data = extract_base64_from_data_url(url)
                if base64_data:
                    images.append(base64_data)
            # Note: Ollama doesn't support URL images, only base64
            
        elif item_type == "image":
            # Anthropic-style base64 image
            source = item.get("source", {})
            if source.get("type") == "base64":
                data = source.get("data", "")
                if data:
                    images.append(data)
                    
        # Skip audio/video - not supported by Ollama
    
    return "\n".join(text_parts) if text_parts else "", images


def normalize_message(message: dict[str, Any]) -> dict[str, Any]:
    """Normalize a message for Ollama API format.
    
    Extracts images from content and puts them in separate 'images' field.
    
    Args:
        message: Message dict with role, content, etc.
        
    Returns:
        Normalized message dict for Ollama API
    """
    result = {}
    
    # Copy standard fields
    if "role" in message:
        result["role"] = message["role"]
    
    # Handle content - extract images
    content = message.get("content")
    text_content, images = extract_images_from_content(content)
    
    result["content"] = text_content
    
    if images:
        result["images"] = images
    
    # Copy tool_calls if present
    if "tool_calls" in message:
        result["tool_calls"] = message["tool_calls"]
    
    # Copy tool_call_id for tool responses
    if "tool_call_id" in message:
        result["tool_call_id"] = message["tool_call_id"]
    
    # Copy name if present
    if "name" in message:
        result["name"] = message["name"]
        # /api/chat names a tool result's tool in `tool_name`; `name` is not
        # a field it reads.
        if message.get("role") == "tool":
            result["tool_name"] = message["name"]

    # A thinking model's reasoning goes back in the field it came out of.
    # The chat template decides how much of it to render (typically only the
    # current turn's, which is what a tool-calling model needs to continue).
    if message.get("role") == "assistant" and message.get("reasoning_content"):
        result["thinking"] = message["reasoning_content"]

    return result


def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize a list of messages for Ollama API.
    
    Args:
        messages: List of message dicts
        
    Returns:
        List of normalized messages for Ollama API
    """
    return [normalize_message(msg) for msg in messages]
