"""Utilities for handling multimodal content in tool responses.

This module provides functions to encode and convert multimodal content
(images, audio) from tool responses into formats suitable for different
LLM providers.

The conversion happens at the LLM client level, allowing provider-specific
handling:
- Gemini: Native multimodal tool response (inline parts)
- OpenAI/Anthropic: Inject as synthetic user message after tool response

Usage:
    # For standard LLM clients (working with ChatMessage objects)
    from agent_system.utils.multimodal_tool_content import (
        encode_multimodal_item,
        create_injection_message_content,
        create_multimodal_injection,
        create_anthropic_multimodal_injection,
        check_vision_support
    )
    
    # For batch clients (working with serialized dict messages)
    from agent_system.utils.multimodal_tool_content import (
        create_multimodal_injection_from_dict,
        create_anthropic_multimodal_injection_from_dict,
        create_gemini_multimodal_parts_from_dict
    )
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm.models import MultimodalToolContent

logger = logging.getLogger(__name__)


# Default size limits
DEFAULT_MAX_IMAGE_SIZE_MB = 20.0
DEFAULT_MAX_AUDIO_SIZE_MB = 25.0

# OpenAI only supports these audio formats
OPENAI_SUPPORTED_AUDIO_FORMATS = {"wav", "mp3"}
OPENAI_AUDIO_CONVERSION_TARGET = "mp3"  # Convert unsupported formats to MP3


def _convert_audio_for_openai(
    audio_bytes: bytes,
    source_format: str,
) -> Tuple[bytes, str]:
    """Convert audio to OpenAI-compatible format if needed.
    
    OpenAI only supports WAV and MP3 for input_audio. This function
    converts unsupported formats (FLAC, OGG, etc.) to MP3.
    
    Args:
        audio_bytes: Raw audio bytes
        source_format: Source format (e.g., "flac", "ogg", "wav")
    
    Returns:
        Tuple of (converted_bytes, new_mime_type)
        If no conversion needed, returns original bytes with original mime_type
    """
    source_format = source_format.lower().lstrip(".")
    
    # Already supported - no conversion needed
    if source_format in OPENAI_SUPPORTED_AUDIO_FORMATS:
        mime_map = {"wav": "audio/wav", "mp3": "audio/mpeg"}
        return audio_bytes, mime_map.get(source_format, f"audio/{source_format}")
    
    # Need to convert to MP3
    try:
        from pydub import AudioSegment
        
        # Load audio from bytes
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format=source_format)
        
        # Export to MP3
        output_buffer = io.BytesIO()
        audio.export(output_buffer, format="mp3", bitrate="128k")
        output_buffer.seek(0)
        
        converted_bytes = output_buffer.read()
        logger.debug(
            "Converted audio from %s to MP3: %d bytes → %d bytes",
            source_format, len(audio_bytes), len(converted_bytes)
        )
        return converted_bytes, "audio/mpeg"
        
    except ImportError:
        logger.warning(
            "pydub not available for audio conversion. FLAC/OGG files may not work with OpenAI."
        )
        return audio_bytes, f"audio/{source_format}"
    except Exception as e:
        logger.error("Failed to convert audio from %s to MP3: %s", source_format, e)
        # Return original - let OpenAI reject it with a clear error
        return audio_bytes, f"audio/{source_format}"


@dataclass
class EncodedMultimodalContent:
    """Base64-encoded multimodal content ready for LLM consumption."""
    type: str  # "image", "audio", "video"
    mime_type: str  # e.g., "image/png", "audio/wav"
    data: str  # Base64 encoded content
    description: Optional[str] = None


@dataclass
class MultimodalError:
    """Error information for multimodal content that couldn't be encoded."""
    type: str  # "image", "audio", "video"
    path: str
    error: str  # Human-readable error message


def extract_inline_media(item: Any) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Pull the payload back out of a message content item — the inverse of the
    injection builders in this module.

    Media sits in a conversation in one of five shapes, all written by the
    functions below (or by multimodal_processor.encode_*_to_data_url):

    1. ``{"source": {"data": <base64 str|bytes>, "media_type": ...}}``  Anthropic
    2. ``{"image_url": {"url": "data:<mime>;base64,..."}}``             OpenAI
    3. ``{"inline_data": {"data": ..., "mime_type": ...}}``             Gemini
    4. ``{"audio_url": "data:<mime>;base64,..."}``                      OpenAI audio
    5. ``{"video_url": "data:<mime>;base64,..."}``

    Items that carry only a file path (MultimodalToolContent) or a remote URL
    have no inline payload and yield ``(None, mime, name)``.

    Args:
        item: Content item — dict or pydantic model (ImageContent, AudioContent, …).

    Returns:
        (raw bytes or None, mime type or None, original filename or None)

    Raises:
        ValueError: a base64 payload is present but cannot be decoded.
    """
    if isinstance(item, dict):
        d: Any = item
    elif hasattr(item, "model_dump"):
        d = item.model_dump()
    else:
        return None, None, None
    if not isinstance(d, dict):
        return None, None, None

    name = d.get("name")
    mime = d.get("media_type") or d.get("mime_type")

    def _from_data_url(url: Any) -> Tuple[Optional[str], Optional[str]]:
        if isinstance(url, str) and ";base64," in url:
            head, payload = url.split(";base64,", 1)
            return payload, (head[5:] if head.startswith("data:") else None)
        return None, None

    b64: Optional[str] = None

    for container, mime_key in ((d.get("source"), "media_type"),
                                (d.get("inline_data"), "mime_type")):
        if isinstance(container, dict) and container.get("data"):
            data = container["data"]
            mime = container.get(mime_key) or mime
            if isinstance(data, bytes):
                return data, mime, name
            b64 = data
            break

    if b64 is None:
        image_url = d.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        for candidate in (url, d.get("audio_url"), d.get("video_url")):
            payload, url_mime = _from_data_url(candidate)
            if payload:
                b64, mime = payload, (url_mime or mime)
                break

    if b64 is None:
        # Last resort: payload directly on the item. Not written by any builder
        # in this module, but older conversation entries and hand-built items
        # carry it, and dropping the shape here silently disables both
        # deduplication and pre-compaction storage for them.
        loose = d.get("data")
        if isinstance(loose, bytes):
            return loose, mime, name
        if isinstance(loose, str) and loose:
            b64 = loose

    if b64 is None:
        return None, mime, name
    # binascii.Error is a ValueError subclass — a corrupt payload surfaces
    # instead of silently becoming "no media here".
    return base64.b64decode(b64), mime, name


def encode_multimodal_item(
    item: "MultimodalToolContent | Dict[str, Any]",
    max_size_mb: Optional[float] = None
) -> Optional[EncodedMultimodalContent]:
    """Encode a single multimodal item to base64.
    
    Args:
        item: MultimodalToolContent or dict with type, path, mime_type, description
        max_size_mb: Maximum file size in MB (default: type-specific limits)
    
    Returns:
        EncodedMultimodalContent or None if encoding fails/skipped
    """
    # Handle both Pydantic model and dict
    if hasattr(item, "path"):
        path = Path(item.path)
        content_type = item.type
        mime_type = item.mime_type
        description = getattr(item, "description", None)
    else:
        path = Path(item.get("path", ""))
        content_type = item.get("type", "image")
        mime_type = item.get("mime_type", "application/octet-stream")
        description = item.get("description")
    
    if not path.exists():
        logger.warning("Multimodal file not found: %s", path)
        return None
    
    # Determine size limit
    if max_size_mb is None:
        max_size_mb = (
            DEFAULT_MAX_AUDIO_SIZE_MB if content_type == "audio" 
            else DEFAULT_MAX_IMAGE_SIZE_MB
        )
    
    # Check size
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > max_size_mb:
        logger.warning(
            "Multimodal file too large: %s (%.1f MB > %.1f MB limit)",
            path, size_mb, max_size_mb
        )
        return None
    
    # Read file bytes
    try:
        file_bytes = path.read_bytes()
    except Exception as e:
        logger.error("Failed to read multimodal file %s: %s", path, e)
        return None
    
    # For audio: convert to OpenAI-compatible format if needed
    if content_type == "audio":
        source_format = path.suffix.lstrip(".").lower() or mime_type.split("/")[-1]
        file_bytes, mime_type = _convert_audio_for_openai(file_bytes, source_format)
    
    # Base64 encode
    data = base64.b64encode(file_bytes).decode("utf-8")
    
    logger.debug(
        "Encoded multimodal content: %s (%s, %.1f MB)",
        path, mime_type, size_mb
    )
    
    return EncodedMultimodalContent(
        type=content_type,
        mime_type=mime_type,
        data=data,
        description=description
    )


def create_injection_message_content(
    tool_name: Optional[str],
    tool_call_id: Optional[str],
    encoded_items: List[EncodedMultimodalContent],
    supports_audio: bool = False
) -> List[Dict[str, Any]]:
    """Create content array for injected user message (OpenAI format).
    
    This creates the content for a synthetic "user" message that will be
    inserted after the tool response message. The format is OpenAI-compatible
    with text prefix and image_url entries.
    
    Args:
        tool_name: Name of the tool that generated the content
        tool_call_id: Optional call ID for traceability
        encoded_items: List of encoded multimodal items
        supports_audio: Whether the model supports audio input (gpt-4o-audio-preview)
    
    Returns:
        List of content items for ChatMessage.content
    """
    # Build prefix text. tool_name is None when the caller already printed
    # it in a preceding note block -- interpolating None showed the model a
    # literal "TOOL OUTPUT: None".
    if tool_name:
        prefix = f"📎 [TOOL OUTPUT: {tool_name}"
        if tool_call_id:
            prefix += f", call_id={tool_call_id}"
        prefix += "]"
    elif tool_call_id:
        prefix = f"📎 [TOOL OUTPUT, call_id={tool_call_id}]"
    else:
        prefix = "📎 [TOOL OUTPUT]"
    
    # Add descriptions
    descriptions = [e.description for e in encoded_items if e.description]
    if descriptions:
        prefix += "\n" + "\n".join(descriptions)
    
    content: List[Dict[str, Any]] = [{"type": "text", "text": prefix}]
    
    for item in encoded_items:
        if item.type == "image":
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{item.mime_type};base64,{item.data}"
                }
            })
        elif item.type == "audio":
            if supports_audio:
                # OpenAI audio input format - only for audio-capable models
                audio_format = _get_audio_format(item.mime_type)
                content.append({
                    "type": "input_audio",
                    "input_audio": {
                        "data": item.data,
                        "format": audio_format
                    }
                })
            else:
                # Model doesn't support audio - add as text note
                content.append({
                    "type": "text",
                    "text": f"\n[Audio file: {item.description or 'attached'} - audio input not supported by this model]"
                })
        # Video: Most providers don't support video yet, add as text note
        elif item.type == "video":
            content.append({
                "type": "text",
                "text": f"\n[Video file: {item.description or 'attached'}]"
            })
    
    return content


def create_gemini_parts(
    text_content: str,
    encoded_items: List[EncodedMultimodalContent]
) -> List[Dict[str, Any]]:
    """Create parts array for Gemini native multimodal tool response.
    
    Gemini supports multimodal content directly in functionResponse.parts,
    so we don't need to inject a separate user message.
    
    Args:
        text_content: The text content of the tool response
        encoded_items: List of encoded multimodal items
    
    Returns:
        List of parts for Gemini functionResponse
    """
    parts: List[Dict[str, Any]] = [{"text": text_content}]
    
    for item in encoded_items:
        parts.append({
            "inlineData": {
                "mimeType": item.mime_type,
                "data": item.data
            }
        })
    
    return parts


def _get_audio_format(mime_type: str) -> str:
    """Get OpenAI audio format from MIME type.
    
    Note: After encoding, unsupported formats (FLAC, OGG) have already
    been converted to MP3, so mime_type should be audio/mpeg.
    """
    mime_to_format = {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/mp3": "mp3",
        "audio/mpeg": "mp3",
        # These should not appear after conversion, but map to mp3 as fallback
        "audio/flac": "mp3",
        "audio/ogg": "mp3",
        "audio/webm": "mp3",
    }
    return mime_to_format.get(mime_type.lower(), "wav")


# `should_inject_multimodal(provider)` lived here and hardcoded
# `native_providers = {"gemini", "google"}` — provider dispatch in core code,
# and it had ZERO production callers (only its own tests): every client
# already decides for itself, by calling the injection helper that fits its
# wire format. Removed 2026-08-26 rather than kept as a name for a later
# caller to trust; the list was already stale (`gemini_sdk` was missing).


def create_multimodal_injection(
    tool_msg: Any,
    supports_vision: bool,
    model_name: str = "unknown",
    supports_audio: bool = False
) -> Optional[Dict[str, Any]]:
    """Create injected user message for multimodal tool content (OpenAI-compatible format).
    
    This is the central function used by OpenAI, HTTPX, and Anthropic clients
    to handle multimodal content in tool responses.
    
    Handles:
    - Normal items: Encode to base64 and inject
    - Missing files: Inject error message explaining file not found
    
    Args:
        tool_msg: ChatMessage with role='tool' and multimodal_content
        supports_vision: Whether the model supports vision/image_input
        model_name: Model name for logging
        supports_audio: Whether the model supports audio input (e.g., gpt-4o-audio-preview)
    
    Returns:
        dict for user message or None if no content to inject
    """
    multimodal_content = getattr(tool_msg, 'multimodal_content', None)
    if not multimodal_content:
        return None
    
    tool_name = getattr(tool_msg, 'name', None) or 'tool'
    tool_call_id = getattr(tool_msg, 'tool_call_id', None)
    
    # Collect info about all items (for both vision and non-vision models)
    items_info = []
    error_items = []
    
    for item in multimodal_content:
        item_type = getattr(item, 'type', None) or (item.get('type') if isinstance(item, dict) else 'unknown')
        desc = getattr(item, 'description', None) or (item.get('description') if isinstance(item, dict) else None)
        path = getattr(item, 'path', None) or (item.get('path') if isinstance(item, dict) else None)
        
        if path and not Path(path).exists():
            # File doesn't exist - this is an error!
            error_items.append({
                'type': item_type,
                'path': path,
                'error': f"File not found: {path}"
            })
        else:
            # Normal item
            if desc:
                items_info.append(f"- {item_type}: {desc}")
            elif path:
                items_info.append(f"- {item_type}: {path}")
            else:
                items_info.append(f"- {item_type}")
    
    if not supports_vision:
        # Model doesn't support vision - inject text note about available content
        logger.debug(
            "Model %s doesn't support vision - injecting text note instead of multimodal",
            model_name
        )
        text_content = f"📎 [TOOL OUTPUT: {tool_name}]\n"
        text_content += f"Generated {len(multimodal_content)} multimodal item(s):\n"
        text_content += "\n".join(items_info)
        
        # Add error items info
        if error_items:
            text_content += "\n\n⚠️ Missing files:\n"
            for item in error_items:
                text_content += f"- {item['type']}: {item['error']}\n"
        
        text_content += "\n\n⚠️ Note: The current model does not support vision/multimodal input. "
        text_content += "The generated content is available at the file paths above but cannot be displayed to you."
        
        return {"role": "user", "content": text_content}
    
    # Model supports vision - encode and inject images
    try:
        encoded_items = []
        text_notes = []  # Additional text notes for error items
        
        for item in multimodal_content:
            path = getattr(item, 'path', None) or (item.get('path') if isinstance(item, dict) else None)
            
            if path and not Path(path).exists():
                # File doesn't exist - add error note
                item_type = item.get('type') if isinstance(item, dict) else getattr(item, 'type', 'unknown')
                text_notes.append(f"⚠️ {item_type} file not found: {path}")
                continue
            
            encoded = encode_multimodal_item(item)
            if encoded:
                encoded_items.append(encoded)
        
        # If we have some encoded items OR some notes to show
        if encoded_items or text_notes:
            content = []
            
            # Add any error/compacted notes first
            if text_notes:
                content.append({
                    "type": "text",
                    "text": f"📎 [TOOL OUTPUT: {tool_name}]\n" + "\n".join(text_notes)
                })
            
            # Add encoded multimodal content
            if encoded_items:
                injection_content = create_injection_message_content(
                    tool_name=tool_name if not text_notes else None,  # Don't repeat tool name
                    tool_call_id=tool_call_id,
                    encoded_items=encoded_items,
                    supports_audio=supports_audio
                )
                content.extend(injection_content)
            
            return {"role": "user", "content": content}
        
        return None
        
    except Exception as e:
        logger.warning("Failed to create multimodal injection: %s", e)
        return None


def create_anthropic_multimodal_injection(
    tool_msg: Any,
    supports_vision: bool,
    model_name: str = "unknown"
) -> Optional[Dict[str, Any]]:
    """Create injected user message for Anthropic format.
    
    Anthropic uses a slightly different format for images (source.type="base64").
    
    Args:
        tool_msg: ChatMessage with role='tool' and multimodal_content
        supports_vision: Whether the model supports vision/image_input
        model_name: Model name for logging
    
    Returns:
        dict for user message in Anthropic format or None
    """
    multimodal_content = getattr(tool_msg, 'multimodal_content', None)
    if not multimodal_content:
        return None
    
    tool_name = getattr(tool_msg, 'name', None) or 'tool'
    tool_call_id = getattr(tool_msg, 'tool_call_id', None)
    
    if not supports_vision:
        # Same text fallback as OpenAI
        return create_multimodal_injection(tool_msg, supports_vision=False, model_name=model_name)
    
    # Model supports vision - encode in Anthropic format
    try:
        content_blocks: List[Dict[str, Any]] = []
        
        # Add text prefix
        prefix = f"📎 [TOOL OUTPUT: {tool_name}"
        if tool_call_id:
            prefix += f", call_id={tool_call_id}"
        prefix += "]"
        
        descriptions = []
        encoded_items = []
        
        for item in multimodal_content:
            encoded = encode_multimodal_item(item)
            if encoded:
                encoded_items.append(encoded)
                if encoded.description:
                    descriptions.append(encoded.description)
        
        if not encoded_items:
            return None
        
        if descriptions:
            prefix += "\n" + "\n".join(descriptions)
        
        content_blocks.append({"type": "text", "text": prefix})
        
        # Add images in Anthropic format
        for item in encoded_items:
            if item.type == "image":
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": item.mime_type,
                        "data": item.data
                    }
                })
            # Audio/Video: Anthropic doesn't support these yet, add as text note
            else:
                content_blocks.append({
                    "type": "text",
                    "text": f"\n[{item.type.title()} file: {item.description or 'attached'}]"
                })
        
        return {"role": "user", "content": content_blocks}
        
    except Exception as e:
        logger.warning("Failed to create Anthropic multimodal injection: %s", e)
        return None


def check_vision_support(capabilities: Any) -> bool:
    """Check if model capabilities include vision/image_input support.
    
    Args:
        capabilities: ModelCapabilities or dict with capability info
    
    Returns:
        True if vision is supported
    """
    if not capabilities:
        return False
    
    # Check for vision capability in different formats
    if hasattr(capabilities, 'image_input') and capabilities.image_input:
        return True
    if hasattr(capabilities, 'capabilities') and 'image_input' in getattr(capabilities, 'capabilities', []):
        return True
    if isinstance(capabilities, dict):
        return capabilities.get('image_input', False) or 'image_input' in capabilities.get('capabilities', [])
    
    return False


# =============================================================================
# Batch Client Utilities (work with serialized dict messages)
# =============================================================================

def create_multimodal_injection_from_dict(
    tool_msg_dict: Dict[str, Any],
    supports_vision: bool,
    model_name: str = "unknown",
    supports_audio: bool = False
) -> Optional[Dict[str, Any]]:
    """Create injected user message from a serialized tool message dict.
    
    This is used by batch clients where messages are already serialized to dicts
    via Pydantic's model_dump(mode='json').
    
    Args:
        tool_msg_dict: Dict with role='tool', content, and optionally multimodal_content
        supports_vision: Whether the model supports vision/image_input
        model_name: Model name for logging
        supports_audio: Whether the model supports audio input (e.g., gpt-4o-audio-preview)
    
    Returns:
        dict for user message or None if no content to inject
    """
    multimodal_content = tool_msg_dict.get('multimodal_content')
    if not multimodal_content:
        return None
    
    tool_name = tool_msg_dict.get('name') or 'tool'
    tool_call_id = tool_msg_dict.get('tool_call_id')
    
    if not supports_vision:
        # Model doesn't support vision - inject text note about available content
        logger.debug(
            "Model %s doesn't support vision - injecting text note instead of multimodal",
            model_name
        )
        items_info = []
        for item in multimodal_content:
            item_type = item.get('type', 'unknown')
            desc = item.get('description')
            path = item.get('path')
            if desc:
                items_info.append(f"- {item_type}: {desc}")
            elif path:
                items_info.append(f"- {item_type}: {path}")
            else:
                items_info.append(f"- {item_type}")
        
        text_content = f"📎 [TOOL OUTPUT: {tool_name}]\n"
        text_content += f"Generated {len(multimodal_content)} multimodal item(s):\n"
        text_content += "\n".join(items_info)
        text_content += "\n\n⚠️ Note: The current model does not support vision/multimodal input. "
        text_content += "The generated content is available at the file paths above but cannot be displayed to you."
        
        return {"role": "user", "content": text_content}
    
    # Model supports vision - encode and inject images
    try:
        encoded_items = []
        for item in multimodal_content:
            # item is already a dict
            encoded = encode_multimodal_item(item)
            if encoded:
                encoded_items.append(encoded)
        
        if not encoded_items:
            return None
        
        content = create_injection_message_content(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            encoded_items=encoded_items,
            supports_audio=supports_audio
        )
        
        return {"role": "user", "content": content}
        
    except Exception as e:
        logger.warning("Failed to create multimodal injection from dict: %s", e)
        return None


def create_anthropic_multimodal_injection_from_dict(
    tool_msg_dict: Dict[str, Any],
    supports_vision: bool,
    model_name: str = "unknown"
) -> Optional[Dict[str, Any]]:
    """Create injected user message from serialized dict for Anthropic format.
    
    This is used by Anthropic batch client where messages are serialized dicts.
    
    Args:
        tool_msg_dict: Dict with role='tool', content, and optionally multimodal_content
        supports_vision: Whether the model supports vision/image_input
        model_name: Model name for logging
    
    Returns:
        dict for user message in Anthropic format or None
    """
    multimodal_content = tool_msg_dict.get('multimodal_content')
    if not multimodal_content:
        return None
    
    tool_name = tool_msg_dict.get('name') or 'tool'
    tool_call_id = tool_msg_dict.get('tool_call_id')
    
    if not supports_vision:
        # Same text fallback
        return create_multimodal_injection_from_dict(tool_msg_dict, supports_vision=False, model_name=model_name)
    
    # Model supports vision - encode in Anthropic format
    try:
        content_blocks: List[Dict[str, Any]] = []
        
        # Add text prefix
        prefix = f"📎 [TOOL OUTPUT: {tool_name}"
        if tool_call_id:
            prefix += f", call_id={tool_call_id}"
        prefix += "]"
        
        descriptions = []
        encoded_items = []
        
        for item in multimodal_content:
            encoded = encode_multimodal_item(item)
            if encoded:
                encoded_items.append(encoded)
                if encoded.description:
                    descriptions.append(encoded.description)
        
        if not encoded_items:
            return None
        
        if descriptions:
            prefix += "\n" + "\n".join(descriptions)
        
        content_blocks.append({"type": "text", "text": prefix})
        
        # Add images in Anthropic format
        for item in encoded_items:
            if item.type == "image":
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": item.mime_type,
                        "data": item.data
                    }
                })
            # Audio/Video: Anthropic doesn't support these yet, add as text note
            else:
                content_blocks.append({
                    "type": "text",
                    "text": f"\n[{item.type.title()} file: {item.description or 'attached'}]"
                })
        
        return {"role": "user", "content": content_blocks}
        
    except Exception as e:
        logger.warning("Failed to create Anthropic multimodal injection from dict: %s", e)
        return None


def create_gemini_multimodal_parts_from_dict(
    tool_msg_dict: Dict[str, Any]
) -> Optional[List["EncodedMultimodalContent"]]:
    """Encode multimodal content from serialized dict for Gemini inline parts.
    
    This is used by Gemini batch client where messages are serialized dicts.
    Gemini supports native multimodal in tool responses via inlineData parts.
    
    Args:
        tool_msg_dict: Dict with role='tool' and optionally multimodal_content
    
    Returns:
        List of EncodedMultimodalContent items or None
    """
    multimodal_content = tool_msg_dict.get('multimodal_content')
    if not multimodal_content:
        return None
    
    try:
        encoded_items = []
        for item in multimodal_content:
            encoded = encode_multimodal_item(item)
            if encoded:
                encoded_items.append(encoded)
        
        return encoded_items if encoded_items else None
        
    except Exception as e:
        logger.warning("Failed to encode multimodal content for Gemini batch: %s", e)
        return None
