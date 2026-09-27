"""Token estimation utilities for LLM interactions."""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence, Union

from ..paths import resolve_data_path
from .models import ChatMessage


logger = logging.getLogger(__name__)

# One image, whatever its size and form (file, inline payload, remote URL): the
# provider bills the picture, not the base64 characters or bytes it came in.
# 1120 is Gemini 3's flat per-image cost, measured on cover_artist in production
# (prompt_tokens deltas of ~1060-1172 per added image). Claude bills large images
# higher (up to ~4784 on the current models). The hooks take max(provider count,
# estimate), which corrects that under-count after one call; an over-count would
# never be corrected that way. So this is a measured value, not a bound.
TOKENS_PER_IMAGE = 1120
# https://ai.google.dev/gemini-api/docs/tokens
TOKENS_PER_AUDIO_SECOND = 41  # Gemini: audio = 32 tokens/second, increased by ~30% for accuracy
TOKENS_PER_VIDEO_SECOND = 263  # Gemini: video = 263 tokens/second
# Without a duration, audio and video are counted from their decoded size.
AUDIO_BYTES_PER_SECOND = 16 * 1024
VIDEO_BYTES_PER_SECOND = 100 * 1024

# Text: characters per token, fitted 2026-09-14 against real prompt_tokens of
# 2,770 production requests (DeepSeek v4, Gemini flash, GPT-5.6; message_debugger
# on the writer host). Held-out median error 2-5 % per model, 9 % for GPT, which
# it overestimates. The word ratios this replaces (1.3 / 1.5 / 1.8 tokens per word,
# "calibrated for cl100k") read real/estimate 1.36-1.47 on every one of them:
# 97 % of requests were underestimated by more than 10 %, largely because German
# prose splits into more tokens per word than English. Content type barely moves
# the per-character rate (prose, code and structured text fitted 0.29-0.31),
# JSON a little. Claude tokenizes denser still (real/estimate ~1.5); where it
# matters, the caller's provider count covers it. The fit is dominated by German
# prose: plain English, code and indented JSON come out about 25-35 % too high
# on OpenAI-style tokenizers. That errs on the safe side — the budgets built on
# this estimate trim a little early rather than overflow.
CHARS_PER_TOKEN = 3.3
JSON_CHARS_PER_TOKEN = 2.85

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
    - Images: TOKENS_PER_IMAGE, whatever the file size

    Falls back to size-based estimation if the duration cannot be read.
    
    Args:
        path: Path to the file
        file_type: Optional type hint ('audio', 'video', 'image')
        
    Returns:
        Estimated token count, or 0 if file doesn't exist or can't be read
    """
    try:
        # Read where the request encodes it from: a stored data/... path
        # lands in the data directory (multimodal_tool_content)
        file_path = resolve_data_path(path)
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
            estimated_seconds = file_size / AUDIO_BYTES_PER_SECOND
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
            estimated_seconds = file_size / VIDEO_BYTES_PER_SECOND
            tokens = int(estimated_seconds * TOKENS_PER_VIDEO_SECOND)
            logger.debug(f"Estimated {tokens:,} tokens for video {file_path} (fallback)")
            return tokens
        
        else:
            # Image or unknown: one image, however large the file (TOKENS_PER_IMAGE).
            tokens = TOKENS_PER_IMAGE
            logger.debug(f"Estimated {tokens:,} tokens for image {file_path}")
            return tokens

    except Exception as e:
        logger.debug(f"Could not estimate tokens for file {path}: {e}")
    return 0


def _field(item: Any, key: str) -> Any:
    """A key of a dict item, or an attribute of a pydantic one (extras included)."""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def inline_payload(item: Any) -> tuple[int, str] | None:
    """Base64 length of an item's inline payload and its mime type, without decoding.

    The shapes are those of extract_inline_media (source, inline_data, image_url
    as dict or string, audio_url, video_url), for dicts and pydantic items alike;
    that one decodes, and this runs on every call for every media item still in
    the conversation. A payload held as raw bytes counts as the base64 it is sent
    as. The mime is "" when the item names none.
    """
    for container, mime_key in (("source", "media_type"), ("inline_data", "mime_type")):
        box = _field(item, container)
        data = _field(box, "data")
        if isinstance(data, (str, bytes)) and data:
            length = len(data) if isinstance(data, str) else -(-len(data) // 3) * 4
            return length, str(_field(box, mime_key) or _field(item, "media_type")
                               or _field(item, "mime_type") or "")
    for key in ("image_url", "audio_url", "video_url"):
        url = _field(item, key)
        if isinstance(url, dict):
            url = url.get("url")
        cut = url.find(";base64,") if isinstance(url, str) else -1
        if cut >= 0:
            # Indices, not split(): the payload may be megabytes.
            head = url[:cut]
            return len(url) - cut - len(";base64,"), head[5:] if head.startswith("data:") else ""
    return None


def estimate_media_tokens(item: Any) -> int:
    """What a media item costs in the prompt, the same for every form it can take.

    An image costs TOKENS_PER_IMAGE as a file, as an inline payload in any of
    its shapes, or as a remote URL: the provider sees the same picture. Audio is
    counted by ``duration_seconds``, else by its decoded size; video by its
    decoded size. A payload-less audio or video item (a remote URL) is assumed
    to run 10 or 30 seconds. The medium comes from ``type``, and from the
    payload's mime only when ``type`` names none. Anything else costs 0.
    """
    kind = _field(item, "type")
    path = _field(item, "path")
    if path and kind in ("audio", "image", "video"):
        return estimate_file_tokens(path, file_type=kind)
    payload = inline_payload(item)
    # Compared, not looked up: ``type`` is not promised to be a hashable str.
    if kind in ("image", "image_url"):
        kind = "image"
    elif kind not in ("audio", "video"):
        kind = payload[1].split("/")[0] if payload else ""
    raw_bytes = payload[0] * 3 // 4 if payload else 0
    if kind == "audio":
        duration = _field(item, "duration_seconds")
        if duration is not None:
            return int(duration * TOKENS_PER_AUDIO_SECOND)
        if payload:
            return int(raw_bytes / AUDIO_BYTES_PER_SECOND * TOKENS_PER_AUDIO_SECOND)
        return TOKENS_PER_AUDIO_SECOND * 10
    if kind == "video":
        if payload:
            return int(raw_bytes / VIDEO_BYTES_PER_SECOND * TOKENS_PER_VIDEO_SECOND)
        return TOKENS_PER_VIDEO_SECOND * 30
    return TOKENS_PER_IMAGE if kind == "image" else 0


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


def estimate_tools_token_count(tools_schema: List[Dict[str, Any]]) -> int:
    """Estimate token count for tool definitions/schemas sent to the LLM.

    Tool definitions (function schemas) are sent with every LLM call and consume
    significant context window space. This function estimates their token cost
    based on name, description, and parameter schema sizes.

    - Each tool has ~10 tokens base overhead (type wrapper, function key, etc.)
    - Function name: ~1 token per 4 characters
    - Description: estimate_content_tokens (CHARS_PER_TOKEN)
    - Parameters JSON schema: estimate_json_tokens (JSON_CHARS_PER_TOKEN)

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

            description = func.get("description", "")
            if description:
                tool_tokens += estimate_content_tokens(description)

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
    # Both shapes, as the docstring below and the body have always said: the
    # annotation said ChatMessage only, so every caller holding the dicts a
    # session file stores had to lie to the type checker.
    messages: Sequence[Union[ChatMessage, Dict[str, Any]]],
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
            # Every media item on its own. A per-message fallback used to apply
            # only when no item had a payload, so a payload image dropped the
            # URL image next to it.
            if isinstance(msg_content, list):
                msg_tokens += sum(estimate_media_tokens(item) for item in msg_content)

        # Tool attachments (MultimodalToolContent, mostly file paths): encoded at
        # call time, but they are part of this request's cost now.
        if msg_multimodal:
            msg_tokens += sum(estimate_media_tokens(item) for item in msg_multimodal)

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

        # A tool result's content is already counted above, like any message's.
        # It used to be counted a second time here (estimate_tool_result_tokens).
        # Measured 2026-09-14 on 2,500 production requests: real prompt_tokens
        # per estimated token were 0.82 for tool-heavy requests and 1.36 for
        # text on DeepSeek v4 flash (0.85 vs 1.46 on v4 pro) — counted once, 1.39
        # vs 1.36 (1.46 vs 1.46). Only Claude, which tokenizes JSON densely,
        # came closer with the double count; it is not what production runs.
        if msg_tool_call_id:
            msg_tokens += 8  # tool call id overhead

        total_tokens += msg_tokens

    # Add tool definition tokens if tools are provided
    if tools:
        total_tokens += estimate_tools_token_count(tools)

    return total_tokens


def estimate_content_tokens(content: str) -> int:
    """Estimate tokens for text content (see CHARS_PER_TOKEN).

    Characters, not words: the word ratios this replaced were blind to how a
    tokenizer splits long German words and punctuation-dense text, and they
    scored a whole text by one content type — the class switched as soon as a
    single "from " or "class " appeared, and the same text jumped by 20 %.
    """
    if not content:
        return 0
    return int(len(content) / CHARS_PER_TOKEN)


def estimate_json_tokens(json_str: str) -> int:
    """Estimate tokens for JSON content: tool call arguments, tool schemas."""
    if not json_str:
        return 0
    return int(len(json_str) / JSON_CHARS_PER_TOKEN)
