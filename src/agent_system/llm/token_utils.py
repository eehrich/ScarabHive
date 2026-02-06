"""Token estimation utilities for LLM interactions."""

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Union
from .models import ChatMessage


logger = logging.getLogger(__name__)

# Constants for multimodal token estimation (based on Gemini API documentation)
# https://ai.google.dev/gemini-api/docs/tokens
TOKENS_PER_IMAGE = 258  # Gemini: images ≤384px = 258 tokens, larger = 258 per 768x768 tile
TOKENS_PER_AUDIO_SECOND = 41  # Gemini: audio = 32 tokens/second, increased by ~30% for accuracy
TOKENS_PER_VIDEO_SECOND = 263  # Gemini: video = 263 tokens/second
# Base64-encoded data: ~4 characters per 3 bytes, ~4 chars per token → ~1 token per byte
TOKENS_PER_BASE64_CHAR = 0.25  # 4 base64 chars ≈ 1 token
# File path estimation for images (base64 fallback): bytes * 0.33
TOKENS_PER_FILE_BYTE = 0.33

# Audio file extensions
AUDIO_EXTENSIONS = {'.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac', '.wma', '.aiff', '.opus'}
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.webm', '.wmv', '.flv', '.m4v'}

# =============================================================================
# Media Duration Cache (to avoid re-reading files repeatedly)
# =============================================================================

@dataclass
class _CacheEntry:
    """Cache entry for media duration."""
    duration: float | None
    mtime: float  # File modification time for invalidation
    timestamp: float  # When this entry was created


class _MediaDurationCache:
    """Thread-safe LRU cache for media file durations with TTL.
    
    Uses file path + modification time as cache key to auto-invalidate
    when files change. Implements TTL and max size to prevent memory leaks.
    """
    
    def __init__(self, max_size: int = 1000, ttl_seconds: float = 3600.0):
        """Initialize cache.
        
        Args:
            max_size: Maximum number of entries (default 1000)
            ttl_seconds: Time-to-live in seconds (default 1 hour)
        """
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = Lock()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._access_order: list[str] = []  # For LRU eviction
    
    def get(self, path: str, mtime: float) -> float | None | type[_CacheEntry]:
        """Get cached duration if valid.
        
        Returns:
            - float: Cached duration in seconds
            - None: Cached "no duration available" result
            - _CacheEntry class (sentinel): Cache miss, needs computation
        """
        with self._lock:
            entry = self._cache.get(path)
            if entry is None:
                return _CacheEntry  # Cache miss
            
            now = time.time()
            
            # Check TTL
            if now - entry.timestamp > self._ttl:
                del self._cache[path]
                if path in self._access_order:
                    self._access_order.remove(path)
                return _CacheEntry  # Expired
            
            # Check if file was modified
            if entry.mtime != mtime:
                del self._cache[path]
                if path in self._access_order:
                    self._access_order.remove(path)
                return _CacheEntry  # File changed
            
            # Update access order for LRU
            if path in self._access_order:
                self._access_order.remove(path)
            self._access_order.append(path)
            
            return entry.duration
    
    def set(self, path: str, mtime: float, duration: float | None) -> None:
        """Store duration in cache."""
        with self._lock:
            # Evict oldest entries if at capacity
            while len(self._cache) >= self._max_size and self._access_order:
                oldest = self._access_order.pop(0)
                self._cache.pop(oldest, None)
            
            self._cache[path] = _CacheEntry(
                duration=duration,
                mtime=mtime,
                timestamp=time.time()
            )
            
            if path in self._access_order:
                self._access_order.remove(path)
            self._access_order.append(path)
    
    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
            self._access_order.clear()
    
    def stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "ttl_seconds": self._ttl
            }


# Global cache instance
_duration_cache = _MediaDurationCache(max_size=1000, ttl_seconds=3600.0)


def _get_audio_duration_seconds(path: Path) -> float | None:
    """Get audio duration in seconds using pydub.
    
    Results are cached for 1 hour to avoid repeatedly loading audio files.
    Falls back to None if pydub is not available or file cannot be read.
    """
    path_str = str(path)
    
    # Get file mtime for cache validation
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    
    # Check cache
    cached = _duration_cache.get(path_str, mtime)
    if cached is not _CacheEntry:
        return cached
    
    # Cache miss - compute duration
    duration: float | None = None
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_file(str(path))
        duration = len(audio) / 1000.0  # pydub returns milliseconds
    except Exception as e:
        logger.debug(f"Could not get audio duration for {path}: {e}")
        duration = None
    
    # Store in cache (including None results)
    _duration_cache.set(path_str, mtime, duration)
    return duration


def _get_video_duration_seconds(path: Path) -> float | None:
    """Get video duration in seconds using ffprobe or fallback.
    
    Results are cached for 1 hour. Falls back to None if video cannot be analyzed.
    """
    path_str = str(path)
    
    # Get file mtime for cache validation
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    
    # Check cache
    cached = _duration_cache.get(path_str, mtime)
    if cached is not _CacheEntry:
        return cached
    
    # Cache miss - compute duration
    duration: float | None = None
    try:
        import subprocess
        import json
        # Try ffprobe first (most reliable)
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', 
             '-show_format', str(path)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            duration = float(data.get('format', {}).get('duration', 0))
            if duration <= 0:
                duration = None
    except Exception as e:
        logger.debug(f"ffprobe failed for {path}: {e}")
        duration = None
    
    # Store in cache
    _duration_cache.set(path_str, mtime, duration)
    return duration


def estimate_file_tokens(path: Union[str, Path], file_type: str | None = None) -> int:
    """Estimate tokens for a media file sent to LLM.
    
    Uses duration-based estimation for audio/video (per Gemini API docs):
    - Audio: 41 tokens per second (32 base + ~30% buffer)
    - Video: 263 tokens per second
    - Images: 258 tokens (small) or 258 per 768x768 tile (large)
    
    Falls back to size-based estimation for images or if duration cannot be read.
    
    Args:
        path: Path to the file
        file_type: Optional type hint ('audio', 'video', 'image')
        
    Returns:
        Estimated token count, or 0 if file doesn't exist or can't be read
    """
    try:
        file_path = Path(path)
        if not file_path.exists():
            return 0
        
        suffix = file_path.suffix.lower()
        
        # Determine file type from extension or hint
        is_audio = file_type == 'audio' or suffix in AUDIO_EXTENSIONS
        is_video = file_type == 'video' or suffix in VIDEO_EXTENSIONS
        
        if is_audio:
            duration = _get_audio_duration_seconds(file_path)
            if duration is not None:
                tokens = int(duration * TOKENS_PER_AUDIO_SECOND)
                logger.debug(f"Estimated {tokens:,} tokens for audio {file_path} ({duration:.1f}s)")
                return tokens
            # Fallback to rough estimate: ~16KB per second for typical audio
            file_size = file_path.stat().st_size
            estimated_seconds = file_size / (16 * 1024)
            tokens = int(estimated_seconds * TOKENS_PER_AUDIO_SECOND)
            logger.debug(f"Estimated {tokens:,} tokens for audio {file_path} (fallback from {file_size:,} bytes)")
            return tokens
            
        elif is_video:
            duration = _get_video_duration_seconds(file_path)
            if duration is not None:
                tokens = int(duration * TOKENS_PER_VIDEO_SECOND)
                logger.debug(f"Estimated {tokens:,} tokens for video {file_path} ({duration:.1f}s)")
                return tokens
            # Fallback: ~100KB per second for typical video
            file_size = file_path.stat().st_size
            estimated_seconds = file_size / (100 * 1024)
            tokens = int(estimated_seconds * TOKENS_PER_VIDEO_SECOND)
            logger.debug(f"Estimated {tokens:,} tokens for video {file_path} (fallback)")
            return tokens
        
        else:
            # Image or unknown: use size-based estimation
            file_size = file_path.stat().st_size
            # For images, estimate tiles: small images = 258, larger = more tiles
            # Rough heuristic: files >100KB likely need multiple tiles
            if file_size < 100 * 1024:
                tokens = TOKENS_PER_IMAGE
            else:
                # Estimate number of 768x768 tiles (each ~300KB for typical JPEG)
                estimated_tiles = max(1, file_size // (300 * 1024))
                tokens = estimated_tiles * TOKENS_PER_IMAGE
            logger.debug(f"Estimated {tokens:,} tokens for image {file_path} ({file_size:,} bytes)")
            return tokens
            
    except Exception as e:
        logger.debug(f"Could not estimate tokens for file {path}: {e}")
    return 0


def estimate_inline_data_tokens(item: Union[dict, Any]) -> int:
    """Estimate tokens for inline base64-encoded data in multimodal content.
    
    Gemini and other LLMs accept inline_data with base64-encoded images/audio.
    These contribute significantly to token counts.
    
    For audio: Uses duration_seconds if available (much more accurate than base64 size).
    Audio is counted at ~41 tokens/second per Gemini API docs.
    
    Also handles MultimodalToolContent with file paths (the actual base64
    encoding happens at LLM call time, but we estimate from file size).
    
    Args:
        item: Content item (dict or pydantic model) with potential inline data
        
    Returns:
        Estimated token count for the inline data, or 0 if no inline data
    """
    if isinstance(item, dict):
        # PRIORITY: Check for duration_seconds first (most accurate for audio)
        # This is set by encode_audio_to_data_url BEFORE base64 encoding
        duration = item.get('duration_seconds')
        if duration is not None and item.get('type') == 'audio':
            tokens = int(duration * TOKENS_PER_AUDIO_SECOND)
            logger.debug(f"Audio tokens from duration_seconds: {duration:.1f}s = {tokens:,} tokens")
            return tokens
        
        # Check various inline data formats
        # Format 1: {"type": "image", "source": {"type": "base64", "data": "..."}}
        source = item.get('source', {})
        if isinstance(source, dict) and source.get('type') == 'base64':
            data_str = source.get('data', '')
            if data_str:
                return int(len(data_str) * TOKENS_PER_BASE64_CHAR)
        
        # Format 2: {"type": "audio", "audio_url": "data:audio/wav;base64,..."}
        # Since we don't have duration_seconds here, fall back to size-based estimation
        audio_url = item.get('audio_url', '')
        if isinstance(audio_url, str) and ';base64,' in audio_url:
            data_str = audio_url.split(';base64,', 1)[1]
            if data_str:
                # Estimate duration from base64 size: decode to bytes, then ~16KB/second
                import base64
                try:
                    raw_bytes = len(base64.b64decode(data_str))
                    estimated_seconds = raw_bytes / (16 * 1024)
                    tokens = int(estimated_seconds * TOKENS_PER_AUDIO_SECOND)
                    logger.debug(f"Audio tokens from size fallback: ~{estimated_seconds:.1f}s = {tokens:,} tokens")
                    return tokens
                except Exception:
                    # Last resort: rough estimate
                    return int(len(data_str) * 0.01)  # Very rough fallback
        
        # Format 3: {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        image_url = item.get('image_url', {})
        if isinstance(image_url, dict):
            url = image_url.get('url', '')
            if isinstance(url, str) and ';base64,' in url:
                data_str = url.split(';base64,', 1)[1]
                if data_str:
                    return int(len(data_str) * TOKENS_PER_BASE64_CHAR)
        
        # Format 4: Direct inline_data (Gemini native format after conversion)
        inline_data = item.get('inline_data', {})
        if isinstance(inline_data, dict):
            data_str = inline_data.get('data', '')
            mime_type = inline_data.get('mime_type', '')
            # If data is bytes, estimate from length
            if isinstance(data_str, bytes):
                # Check if audio
                if mime_type.startswith('audio/'):
                    estimated_seconds = len(data_str) / (16 * 1024)
                    return int(estimated_seconds * TOKENS_PER_AUDIO_SECOND)
                return len(data_str) // 3  # ~3 bytes per token for binary data
            elif data_str:
                # Check if audio - use duration estimation
                if mime_type.startswith('audio/'):
                    import base64
                    try:
                        raw_bytes = len(base64.b64decode(data_str))
                        estimated_seconds = raw_bytes / (16 * 1024)
                        return int(estimated_seconds * TOKENS_PER_AUDIO_SECOND)
                    except Exception:
                        pass
                return int(len(data_str) * TOKENS_PER_BASE64_CHAR)
        
        # Format 5: MultimodalToolContent with file path (converted to base64 at LLM call)
        # {"type": "audio"|"image"|"video", "path": "/path/to/file", "mime_type": "..."}
        file_path = item.get('path', '')
        item_type = item.get('type', '')
        if file_path and item_type in ('audio', 'image', 'video'):
            return estimate_file_tokens(file_path, file_type=item_type)
    
    elif hasattr(item, 'duration_seconds') and hasattr(item, 'type'):
        # Pydantic AudioContent with duration_seconds
        duration = getattr(item, 'duration_seconds', None)
        item_type = getattr(item, 'type', '')
        if duration is not None and item_type == 'audio':
            tokens = int(duration * TOKENS_PER_AUDIO_SECOND)
            logger.debug(f"Audio tokens from duration_seconds (pydantic): {duration:.1f}s = {tokens:,} tokens")
            return tokens
    
    if hasattr(item, 'source'):
        # Pydantic model with source attribute
        source = getattr(item, 'source', None)
        if source and hasattr(source, 'type') and getattr(source, 'type') == 'base64':
            data_str = getattr(source, 'data', '')
            if data_str and isinstance(data_str, str):
                return int(len(data_str) * TOKENS_PER_BASE64_CHAR)
    
    if hasattr(item, 'path'):
        # Pydantic model with path attribute (MultimodalToolContent)
        file_path = getattr(item, 'path', '')
        item_type = getattr(item, 'type', '')
        if file_path and item_type in ('audio', 'image', 'video'):
            return estimate_file_tokens(file_path, file_type=item_type)
    
    return 0


def extract_text_from_content(content: Union[str, List[Any], Any]) -> str:
    """Extract text content from potentially multimodal message content.
    
    Handles:
    - Plain string content (most common)
    - List of content items (multimodal: text, image, audio, etc.)
    - Dict content items
    
    Args:
        content: Message content (str, list, or other)
        
    Returns:
        Extracted text as string
    """
    if content is None:
        return ""
    
    if isinstance(content, str):
        return content
    
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get('type', '')
                if item_type == 'text':
                    text_parts.append(item.get('text', ''))
                elif item_type == 'text_file':
                    # Include text file content for token counting
                    text_parts.append(item.get('content', ''))
                # For image/audio, we don't extract text but they contribute to tokens
            elif hasattr(item, 'type'):
                # Pydantic model (TextContent, ImageContent, etc.)
                item_type = getattr(item, 'type', '')
                if item_type == 'text':
                    text_parts.append(getattr(item, 'text', ''))
                elif item_type == 'text_file':
                    # Include text file content for token counting
                    text_parts.append(getattr(item, 'content', ''))
        return ' '.join(text_parts)
    
    # Fallback for other types
    return str(content)


def count_multimodal_items(content: Union[str, List[Any], Any]) -> dict:
    """Count multimodal items in content and estimate their token contribution.
    
    Args:
        content: Message content
        
    Returns:
        Dict with counts and token estimates:
        {'images': N, 'audio': N, 'video': N, 'inline_data_tokens': N}
    """
    counts = {'images': 0, 'audio': 0, 'video': 0, 'inline_data_tokens': 0}
    
    if not isinstance(content, list):
        return counts
    
    for item in content:
        if isinstance(item, dict):
            item_type = item.get('type', '')
        elif hasattr(item, 'type'):
            item_type = getattr(item, 'type', '')
        else:
            continue
        
        # Check for inline data tokens first (this is the big one!)
        inline_tokens = estimate_inline_data_tokens(item)
        if inline_tokens > 0:
            counts['inline_data_tokens'] += inline_tokens
        
        # Count item types (only for fallback when no inline data)
        if item_type in ('image', 'image_url'):
            counts['images'] += 1
        elif item_type == 'audio':
            counts['audio'] += 1
        elif item_type == 'video':
            counts['video'] += 1
    
    return counts


def estimate_tools_token_count(tools_schema: List[Dict[str, Any]]) -> int:
    """Estimate token count for tool definitions/schemas sent to the LLM.

    Tool definitions (function schemas) are sent with every LLM call and consume
    significant context window space. This function estimates their token cost
    based on name, description, and parameter schema sizes.

    Based on empirical measurement against OpenAI's cl100k_base tokenizer:
    - Each tool has ~10 tokens base overhead (type wrapper, function key, etc.)
    - Function name: ~1 token per 4 characters
    - Description: word-based estimation at 1.3 tokens/word (natural language)
    - Parameters JSON schema: JSON-aware estimation (structural + content tokens)

    Args:
        tools_schema: List of OpenAI-format tool definitions, each like:
            {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}

    Returns:
        Estimated total token count for all tool definitions
    """
    if not tools_schema:
        return 0

    total = 0
    for tool in tools_schema:
        # Base overhead per tool: {"type": "function", "function": {...}} wrapper
        tool_tokens = 10

        func = tool.get("function", {}) if isinstance(tool, dict) else {}
        if func:
            # Function name
            name = func.get("name", "")
            if name:
                tool_tokens += max(1, len(name) // 4)

            # Description - use word-based estimation (natural language)
            description = func.get("description", "")
            if description:
                words = len(description.split())
                tool_tokens += int(words * 1.3)

            # Parameters schema - JSON structure with keys, types, descriptions
            params = func.get("parameters", {})
            if params:
                import json
                try:
                    params_str = json.dumps(params)
                    tool_tokens += estimate_json_tokens(params_str)
                except (TypeError, ValueError):
                    # Fallback: rough estimate from string representation
                    tool_tokens += len(str(params)) // 4

            # Strict mode flag
            if func.get("strict"):
                tool_tokens += 2

        total += tool_tokens

    return total


def estimate_token_count(
    messages: List[ChatMessage],
    tools: Optional[List[Dict[str, Any]]] = None
) -> int:
    """Enhanced token count estimation with improved accuracy for different content types.

    Supports multimodal content (images, audio, video) in addition to text.
    Accepts both ChatMessage objects and dict messages for flexibility.
    Optionally includes tool definition tokens when tools are provided.

    Args:
        messages: List of chat messages (ChatMessage or dict) to estimate tokens for
        tools: Optional list of tool schemas to include in estimation

    Returns:
        Estimated total token count (messages + tools if provided)
    """
    total_tokens = 0

    for msg in messages:
        msg_tokens = 0
        
        # Handle both ChatMessage objects and dicts
        if isinstance(msg, dict):
            msg_content = msg.get('content')
            msg_multimodal = msg.get('multimodal_content')
            msg_tool_calls = msg.get('tool_calls')
            msg_tool_call_id = msg.get('tool_call_id')
        else:
            msg_content = msg.content
            msg_multimodal = getattr(msg, 'multimodal_content', None)
            msg_tool_calls = getattr(msg, 'tool_calls', None)
            msg_tool_call_id = getattr(msg, 'tool_call_id', None)

        # Base overhead for message structure (role, formatting, etc.)
        msg_tokens += 4  # Base message overhead

        # Count content tokens with content-type aware ratios
        if msg_content:
            # Extract text content (handles multimodal lists)
            text_content = extract_text_from_content(msg_content)
            msg_tokens += estimate_content_tokens(text_content)
            
            # Add tokens for multimodal items (images, audio, video)
            multimodal_counts = count_multimodal_items(msg_content)
            
            # PRIORITY: Use actual inline_data tokens if available (much more accurate!)
            if multimodal_counts['inline_data_tokens'] > 0:
                msg_tokens += multimodal_counts['inline_data_tokens']
            else:
                # Fallback to rough estimates only when no inline data detected
                msg_tokens += multimodal_counts['images'] * TOKENS_PER_IMAGE
                # Audio tokens estimated at ~25 tokens/second, assume ~10 seconds average
                msg_tokens += multimodal_counts['audio'] * (TOKENS_PER_AUDIO_SECOND * 10)
                # Video similar to audio but larger
                msg_tokens += multimodal_counts['video'] * (TOKENS_PER_AUDIO_SECOND * 30)

        # Count multimodal_content from tool responses (MultimodalToolContent with file paths)
        # This is crucial! Files are encoded to base64 at LLM call time, but we need to count them NOW
        if msg_multimodal:
            for mm_item in msg_multimodal:
                item_tokens = estimate_inline_data_tokens(mm_item)
                if item_tokens > 0:
                    msg_tokens += item_tokens
                    logger.debug(f"Counted {item_tokens:,} tokens for multimodal_content item")
                else:
                    # Fallback for items without detectable file/data
                    item_type = getattr(mm_item, 'type', '') if hasattr(mm_item, 'type') else mm_item.get('type', '')
                    if item_type == 'image':
                        msg_tokens += TOKENS_PER_IMAGE
                    elif item_type == 'audio':
                        msg_tokens += TOKENS_PER_AUDIO_SECOND * 10
                    elif item_type == 'video':
                        msg_tokens += TOKENS_PER_AUDIO_SECOND * 30

        # Count tool calls with detailed breakdown
        if msg_tool_calls:
            for tc in msg_tool_calls:
                # Tool call overhead (id, type, function wrapper)
                msg_tokens += 10

                func = tc.get("function", {})
                func_name = func.get("name", "")
                if func_name:  # Only count if name exists
                    msg_tokens += len(func_name) // 4  # Function names are typically short

                # Tool arguments - often JSON, handle differently
                args_str = str(func.get("arguments", ""))
                if args_str:
                    msg_tokens += estimate_json_tokens(args_str)

        # Count tool results (these can be the biggest consumers)
        if msg_tool_call_id:
            # Tool call ID overhead
            msg_tokens += 8
            # Tool result content - extract text for multimodal
            content = extract_text_from_content(msg_content) if msg_content else ""
            if content:
                msg_tokens += estimate_tool_result_tokens(content)

        total_tokens += msg_tokens

    # Add tool definition tokens if tools are provided
    if tools:
        total_tokens += estimate_tools_token_count(tools)

    return total_tokens


def estimate_content_tokens(content: str) -> int:
    """Estimate tokens for message content using word-based ratios.

    Args:
        content: Text content to estimate

    Returns:
        Estimated token count
    """
    if not content:
        return 0

    # Word-based estimation (more accurate than character-based for formatted text)
    words = len(content.split())
    content_len = len(content)
    
    # Sanity check: if content is large but word count is suspiciously low,
    # it's likely minified JSON/code without whitespace. Fall back to char-based.
    # Typical text has ~5 chars/word, so if ratio > 50, content lacks whitespace.
    if content_len > 1000 and words > 0:
        chars_per_word = content_len / words
        if chars_per_word > 50:
            # Minified content: use character-based estimate
            # ~4 characters per token for cl100k_base tokenizer
            return content_len // 4

    # Detect content type for better estimation
    # Ratios calibrated for OpenAI cl100k_base tokenizer (GPT-4/5)
    if is_code_content(content):
        # Code: higher token density due to symbols, operators, keywords
        # Ratio: ~1.5 tokens per word (empirically measured)
        return int(words * 1.5)
    elif is_structured_data(content):
        # JSON/XML: compact structure, many punctuation tokens
        # Ratio: ~1.8 tokens per word (empirically measured)
        return int(words * 1.8)
    else:
        # Natural language: standard ratio
        # Ratio: ~1.3 tokens per word for cl100k_base (was 0.75, too low)
        return int(words * 1.3)


def estimate_json_tokens(json_str: str) -> int:
    """Estimate tokens for JSON content using word and structure analysis.

    Args:
        json_str: JSON string to estimate

    Returns:
        Estimated token count
    """
    if not json_str:
        return 0

    # Remove JSON structural characters to count actual content words
    content_only = re.sub(r'[{}\[\]":,]', ' ', json_str)
    words = len(content_only.split())

    # Count structural tokens (each structural char is usually a token)
    structural_chars = json_str.count('{') + json_str.count('}') + \
                      json_str.count('[') + json_str.count(']') + \
                      json_str.count('"') + json_str.count(':') + \
                      json_str.count(',')

    # JSON tokens = structural tokens + content words * ratio
    # Increased ratio from 0.8 to 1.3 for cl100k_base accuracy
    return structural_chars + int(words * 1.3)


def estimate_tool_result_tokens(content: str) -> int:
    """Estimate tokens for tool results using content-aware word counting.

    Args:
        content: Tool result content to estimate

    Returns:
        Estimated token count
    """
    if not content:
        return 0

    # Tool results can be JSON, plain text, HTML, etc.
    if content.strip().startswith('{') or content.strip().startswith('['):
        # Likely JSON response
        return estimate_json_tokens(content)
    elif '<' in content and '>' in content:
        # Likely HTML/XML - high token density due to tags
        words = len(content.split())
        return int(words * 1.7)  # HTML has many tag tokens (increased from 1.4)
    elif is_code_content(content):
        # Code output
        words = len(content.split())
        return int(words * 1.5)  # Increased from 1.2
    else:
        # Plain text tool results
        words = len(content.split())
        return int(words * 1.3)  # Increased from 0.75 for cl100k_base


def is_code_content(content: str) -> bool:
    """Detect if content is likely code.

    Args:
        content: Text content to check

    Returns:
        True if content appears to be code
    """
    code_indicators = [
        'def ', 'function ', 'class ', 'import ', 'from ',
        '=>', '&&', '||', '{}', '[]', '()', 'const ', 'let ', 'var ',
        'if (', 'for (', 'while (', 'switch (', 'catch (', 'try {'
    ]

    # Count code-like patterns
    code_score = sum(1 for indicator in code_indicators if indicator in content)

    # Also check character density of symbols common in code
    symbol_chars = sum(1 for c in content if c in '{}[]();=+-*/<>!')
    symbol_ratio = symbol_chars / len(content) if content else 0

    return code_score >= 2 or symbol_ratio > 0.15


def is_structured_data(content: str) -> bool:
    """Detect if content is structured data like JSON, XML, YAML.

    Args:
        content: Text content to check

    Returns:
        True if content appears to be structured data
    """
    content = content.strip()
    return (
        (content.startswith('{') and content.endswith('}')) or
        (content.startswith('[') and content.endswith(']')) or
        content.startswith('<') and content.endswith('>') or
        '\n- ' in content  # YAML-like lists
    )
