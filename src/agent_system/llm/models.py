from __future__ import annotations

from typing import Optional, Any, List, Dict, Union, Literal
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from enum import Enum


class LLMRateLimitError(Exception):
    """Raised when LLM rate limit is hit - triggers fallback to alternative profile."""
    def __init__(self, message: str, provider: str = "", model: str = "", retry_after: Optional[float] = None):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.retry_after = retry_after


class LLMQuotaExhaustedError(LLMRateLimitError):
    """Raised when daily/monthly quota is exhausted - triggers fallback."""
    pass


class LLMServerError(Exception):
    """Raised when LLM server returns 5xx after all retries are exhausted - triggers fallback."""
    def __init__(self, message: str, provider: str = "", model: str = "", status_code: int = 0):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.status_code = status_code


class LLMConnectionError(Exception):
    """Raised when the LLM endpoint is unreachable (connect/read timeout, network
    error) after all retries are exhausted - triggers fallback. Unlike
    LLMServerError there is never an HTTP response, so no status code exists."""
    def __init__(self, message: str, provider: str = "", model: str = ""):
        super().__init__(message)
        self.provider = provider
        self.model = model


class ContentType(str, Enum):
    """Types of content in multimodal messages."""
    TEXT = "text"
    IMAGE = "image"
    IMAGE_URL = "image_url"
    AUDIO = "audio"
    VIDEO = "video"


class ImageDetail(str, Enum):
    """Detail level for image processing (GPT-5 specific)."""
    AUTO = "auto"
    LOW = "low"
    HIGH = "high"


class ImageSource(BaseModel):
    """Image source for multimodal content."""
    model_config = ConfigDict(extra="allow")

    type: Literal["base64", "url"] = "base64"
    media_type: Optional[str] = None  # e.g., "image/jpeg", "image/png"
    data: Optional[str] = None  # base64-encoded data
    url: Optional[str] = None  # image URL


class ImageContent(BaseModel):
    """Image content for multimodal messages."""
    model_config = ConfigDict(extra="allow")

    type: Literal["image", "image_url"] = "image"
    source: Optional[ImageSource] = None  # Anthropic format
    image_url: Optional[Union[str, Dict[str, str]]] = None  # OpenAI format
    detail: Optional[ImageDetail] = None  # OpenAI image detail control
    name: Optional[str] = None  # Original filename


class TextContent(BaseModel):
    """Text content for multimodal messages."""
    model_config = ConfigDict(extra="allow")

    type: Literal["text"] = "text"
    text: str


class AudioContent(BaseModel):
    """Audio content for multimodal messages."""
    model_config = ConfigDict(extra="allow")

    type: Literal["audio"] = "audio"
    source: Optional[ImageSource] = None  # Reuse ImageSource for consistent structure
    audio_url: Optional[str] = None
    media_type: Optional[str] = None  # e.g., "audio/wav", "audio/mp3"
    name: Optional[str] = None  # Original filename
    duration_seconds: Optional[float] = None  # Audio duration for accurate token estimation


class VideoContent(BaseModel):
    """Video content for multimodal messages."""
    model_config = ConfigDict(extra="allow")

    type: Literal["video"] = "video"
    source: Optional[ImageSource] = None
    video_url: Optional[str] = None
    media_type: Optional[str] = None  # e.g., "video/mp4", "video/webm"


class TextFileContent(BaseModel):
    """Text file content for multimodal messages (displayed separately from main text)."""
    model_config = ConfigDict(extra="allow")

    type: Literal["text_file"] = "text_file"
    content: str  # File text content
    name: Optional[str] = None  # Original filename


class MultimodalToolContent(BaseModel):
    """Multimodal content returned by a tool for LLM analysis.
    
    This is attached to tool response messages (role='tool') and processed
    by LLM clients according to their capabilities:
    - Gemini: Native multimodal tool response
    - OpenAI/Anthropic: Injected as synthetic user message
    """
    type: str  # "image", "audio", "video"
    path: str  # Local file path to the content
    mime_type: str  # e.g., "image/png", "audio/wav"
    description: Optional[str] = None  # Optional description for context


# Union type for all content types
ContentItem = Union[TextContent, ImageContent, AudioContent, VideoContent, TextFileContent, str, Dict[str, Any]]


class ChatMessage(BaseModel):
    """Chat message supporting both text-only and multimodal content.

    Examples:
        # Text-only (backward compatible)
        ChatMessage(role="user", content="Hello")

        # Multimodal with structured content
        ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64_data
                    }
                }
            ]
        )

        # OpenAI format
        ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "Describe this image"},
                {
                    "type": "image_url",
                    "image_url": "https://example.com/image.jpg"
                }
            ]
        )
    """
    role: str
    content: Optional[Union[str, List[ContentItem]]] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    content_format: Optional[str] = None  # 'text', 'html', 'markdown', 'ansi', etc.
    timestamp: Optional[datetime] = None  # Timestamp when message was created
    # Multimodal content from tool responses - processed by LLM clients
    multimodal_content: Optional[List[MultimodalToolContent]] = None
    # Reasoning/thinking content from models like DeepSeek, OpenAI o-series
    reasoning_content: Optional[str] = None
    # Provider-side encrypted thinking blocks that MUST round-trip to upstream.
    # Specifically: Gemini 3.x thought_signature (carried in OpenRouter's
    # reasoning_details list with format=google-gemini-v1). Dropping it causes
    # MALFORMED_FUNCTION_CALL on the next turn (verified 2026-05-26).
    reasoning_details: Optional[List[Dict[str, Any]]] = None
    # Set by utils/reasoning_artifacts.invalidate_reasoning_artifacts when a
    # history mutation (compaction/summarization) removed this message's
    # reasoning-chain predecessors. Honored per reasoning_details_mode in the
    # LLM client (keep_all strips the now-unverifiable chain remnant) and
    # never sent to providers (client pops it before building the payload).
    rd_orphaned: Optional[bool] = None
    # Anthropic extended/adaptive thinking blocks, verbatim as returned
    # (thinking+signature / redacted_thinking+data) in the model's original
    # order. Inside a tool-use turn these MUST be echoed back COMPLETE and
    # unmodified; a PARTIAL echo is a 400 ("thinking or redacted_thinking
    # blocks in the latest assistant message cannot be modified"), so never
    # filter, dedupe or reorder them. Deliberately NOT reusing
    # reasoning_details: that list is mutated whole-list by
    # reasoning_artifacts, is whitelisted into the OpenRouter payload, and its
    # keep_last mode prunes older entries — all three would corrupt these.
    # Read only by anthropic_client.
    thinking_blocks: Optional[List[Dict[str, Any]]] = None
    # Model that produced them. Signatures are model-bound: another model
    # ignores them silently but still bills them as input, so replay is
    # skipped on mismatch (fallback chains DO move messages between models).
    thinking_model: Optional[str] = None
    # Hook injection tracking: identifies which plugin injected this message.
    # Used by injection hooks to find and replace their previous injections
    # instead of fragile content-based matching.
    injected_by: Optional[str] = None

    def is_multimodal(self) -> bool:
        """Check if message contains multimodal content."""
        if isinstance(self.content, list):
            for item in self.content:
                if isinstance(item, (ImageContent, AudioContent, VideoContent)):
                    return True
                elif hasattr(item, "type"):
                    # Pydantic model - check type attribute
                    content_type = getattr(item, "type", "")
                    if content_type in ("image", "image_url", "audio", "video"):
                        return True
                elif not isinstance(item, (str, TextContent)):
                    return True
        return False

    def get_text_content(self) -> str:
        """Extract text content from message (including text file content)."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            texts = []
            for item in self.content:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, TextContent):
                    texts.append(item.text)
                elif isinstance(item, TextFileContent):
                    # Include text file content with filename header
                    filename = item.name or "file"
                    texts.append(f"[File: {filename}]\n{item.content}")
                elif hasattr(item, "type"):
                    item_type = getattr(item, "type", "")
                    if item_type == "text":
                        # Pydantic model with text
                        texts.append(getattr(item, "text", ""))
                    elif item_type == "text_file":
                        # Dict-style text file content
                        filename = getattr(item, "name", None) or "file"
                        content = getattr(item, "content", "")
                        texts.append(f"[File: {filename}]\n{content}")
            return " ".join(texts)
        return ""

    def has_images(self) -> bool:
        """Check if message contains images."""
        if isinstance(self.content, list):
            for item in self.content:
                if isinstance(item, ImageContent):
                    return True
                elif hasattr(item, "type"):
                    # Pydantic model - check type attribute
                    content_type = getattr(item, "type", "")
                    if content_type in ("image", "image_url"):
                        return True
        return False

    def count_images(self) -> int:
        """Count number of images in message."""
        if not isinstance(self.content, list):
            return 0
        count = 0
        for item in self.content:
            if isinstance(item, ImageContent):
                count += 1
            elif hasattr(item, "type"):
                # Pydantic model - check type attribute
                content_type = getattr(item, "type", "")
                if content_type in ("image", "image_url"):
                    count += 1
        return count


class LLMClient:
    """Base class for LLM clients with streaming support."""

    # Optional hook callbacks — set by the hook integration layer.
    # These are invoked at the LLM-client level to capture exact API payloads.
    _on_pre_llm_request: Any = None   # async callable(payload_info: dict) -> None
    _on_post_llm_response: Any = None  # async callable(response_info: dict) -> None

    #: Wall-clock ms of the last SUCCESSFUL response, stashed by
    #: _notify_post_response. Server-level post_llm_call hooks (e.g.
    #: context_usage_tracker) read it off the client to attribute call latency.
    _last_response_duration_ms: Any = None

    def set_llm_hooks(
        self,
        on_pre_request: Any = None,
        on_post_response: Any = None,
    ) -> None:
        """Set LLM-level hook callbacks.
        
        Called by the hook integration layer to wire up request/response logging.
        
        Args:
            on_pre_request: Async callback(info_dict) called before each API request.
            on_post_response: Async callback(info_dict) called after each API response.
        """
        self._on_pre_llm_request = on_pre_request
        self._on_post_llm_response = on_post_response

    async def _notify_pre_request(self, payload_info: Dict[str, Any]) -> None:
        """Notify pre-request hook if set. Errors are swallowed to not break LLM calls."""
        # Clear the prior call's latency at request start: a success path that
        # never notifies then leaves latency=None (honest) instead of inheriting
        # the previous call's value. _notify_post_response re-sets it on success.
        self._last_response_duration_ms = None
        if self._on_pre_llm_request:
            try:
                await self._on_pre_llm_request(payload_info)
            except Exception as e:
                import logging
                logging.getLogger(__name__).debug(f"pre_llm_request hook error: {e}")

    async def _notify_post_response(self, response_info: Dict[str, Any]) -> None:
        """Notify post-response hook if set. Errors are swallowed to not break LLM calls."""
        # Stash the served call's latency for server-level hooks. Skip
        # retry/error notifications (they carry an "error") so the value
        # reflects the response actually returned. Normal use runs one
        # chat_tools per client instance at a time → last-value is unambiguous.
        if not response_info.get("error") and response_info.get("duration_ms") is not None:
            self._last_response_duration_ms = response_info.get("duration_ms")
        if self._on_post_llm_response:
            try:
                await self._on_post_llm_response(response_info)
            except Exception as e:
                import logging
                logging.getLogger(__name__).debug(f"post_llm_response hook error: {e}")

    async def _notify_retry(
        self,
        provider: str,
        model: str,
        url: str,
        is_streaming: bool,
        error_msg: str,
        attempt: int,
        max_attempts: int,
        duration_ms: Optional[float] = None,
        response_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Notify post-response hook about a failed retry attempt.

        Creates a POST_LLM_RESPONSE notification with error prefixed by retry
        info, making intermediate failures visible in the message debugger.
        """
        import time as _time
        info: Dict[str, Any] = {
            "provider": provider,
            "model": model,
            "url": url,
            "is_streaming": is_streaming,
            "error": f"[RETRY {attempt + 1}/{max_attempts}] {error_msg}",
            "finish_reason": "retry",
            "timestamp_ms": _time.time() * 1000,
        }
        if duration_ms is not None:
            info["duration_ms"] = duration_ms
        if response_data is not None:
            info["response_data"] = response_data
        await self._notify_post_response(info)

    async def _cancellable_sleep(
        self,
        duration: float,
        cancellation_token,
        check_interval: float = 0.5
    ) -> None:
        """Sleep that can be cancelled.
        
        Instead of blocking for the full duration, checks cancellation
        periodically and raises CancelledError if cancelled.
        
        Args:
            duration: Total sleep duration in seconds
            cancellation_token: Token to check for cancellation
            check_interval: How often to check cancellation (seconds)
        """
        import asyncio
        import logging
        logger = logging.getLogger(__name__)
        
        if not cancellation_token:
            await asyncio.sleep(duration)
            return
        
        elapsed = 0.0
        while elapsed < duration:
            if cancellation_token.is_cancelled:
                logger.info(f"[LLMClient] Sleep interrupted by cancellation after {elapsed:.1f}s")
                raise asyncio.CancelledError("Request cancelled during retry wait")
            
            # Sleep for check_interval or remaining time, whichever is smaller
            sleep_time = min(check_interval, duration - elapsed)
            await asyncio.sleep(sleep_time)
            elapsed += sleep_time

    def set_app_title(self, title: str) -> None:
        """Set the application title for providers that support it (e.g., OpenRouter X-Title).

        Override in subclasses that can use this information.
        """

    async def chat(self, messages: list[ChatMessage], cancellation_token=None, status_scope=None) -> str:
        raise NotImplementedError

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None) -> dict:
        raise NotImplementedError

    async def chat_tools_streaming(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None):
        """Stream LLM responses with tool calls.

        Yields chunks in the format:
        - {"type": "content_delta", "delta": str, "accumulated": str}
        - {"type": "tool_call_delta", "index": int, "delta": {...}, "accumulated": {...}}
        - {"type": "final", "assistant": {...}}

        Default implementation falls back to non-streaming.
        
        Args:
            messages: Chat messages
            tools: Tool definitions
            cancellation_token: Optional cancellation token
            status_scope: Optional status scope for progress reporting (batch status, etc.)
        """
        result = await self.chat_tools(messages, tools, cancellation_token, status_scope)
        yield {"type": "final", "assistant": result["assistant"]}

    def supports_streaming(self) -> bool:
        """Return True if this client implements true streaming."""
        return False
