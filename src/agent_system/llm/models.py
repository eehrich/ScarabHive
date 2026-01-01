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


class VideoContent(BaseModel):
    """Video content for multimodal messages."""
    model_config = ConfigDict(extra="allow")

    type: Literal["video"] = "video"
    source: Optional[ImageSource] = None
    video_url: Optional[str] = None
    media_type: Optional[str] = None  # e.g., "video/mp4", "video/webm"


# Union type for all content types
ContentItem = Union[TextContent, ImageContent, AudioContent, VideoContent, str, Dict[str, Any]]


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
        """Extract text content from message."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            texts = []
            for item in self.content:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, TextContent):
                    texts.append(item.text)
                elif hasattr(item, "type") and getattr(item, "type") == "text":
                    # Pydantic model with text
                    texts.append(getattr(item, "text", ""))
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

    async def chat(self, messages: list[ChatMessage], cancellation_token=None) -> str:
        raise NotImplementedError

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None) -> dict:
        raise NotImplementedError

    async def chat_tools_streaming(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None):
        """Stream LLM responses with tool calls.

        Yields chunks in the format:
        - {"type": "content_delta", "delta": str, "accumulated": str}
        - {"type": "tool_call_delta", "index": int, "delta": {...}, "accumulated": {...}}
        - {"type": "final", "assistant": {...}}

        Default implementation falls back to non-streaming.
        """
        result = await self.chat_tools(messages, tools, cancellation_token)
        yield {"type": "final", "assistant": result["assistant"]}

    def supports_streaming(self) -> bool:
        """Return True if this client implements true streaming."""
        return False
