from __future__ import annotations

from contextvars import ContextVar
from typing import Optional, Any, List, Dict, Union, Literal
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from enum import Enum


class LLMRateLimitError(Exception):
    """Raised when LLM rate limit is hit - triggers fallback to alternative profile.

    *retried*: the client already waited and asked again on its own 429 retries -- the agent loop's
    short wait (rate_limit_retry_seconds) would only repeat that, so it moves on at once.
    """
    def __init__(self, message: str, provider: str = "", model: str = "", retry_after: Optional[float] = None,
                 retried: bool = False):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.retry_after = retry_after
        self.retried = retried


class LLMQuotaExhaustedError(LLMRateLimitError):
    """Raised when daily/monthly quota is exhausted - triggers fallback."""
    pass


class LLMServerError(Exception):
    """Raised when the LLM server answers 5xx, or fails without one (status_code 0: an answer that cannot be read, say), after
    all retries are exhausted - triggers fallback."""
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
    # The file's content when the tool returned it: a later turn still shows
    # that, not what a rerender to the same path wrote (multimodal_tool_content).
    sha256: Optional[str] = None


# Union type for all content types
ContentItem = Union[TextContent, ImageContent, AudioContent, VideoContent, TextFileContent, str, Dict[str, Any]]


#: Fields of a ChatMessage that are ours, not the conversation's: never sent to a provider.
#: A client that serialises the whole message pops these (a list per client drifted).
PRIVATE_MESSAGE_FIELDS = frozenset({"injected_by", "prefixed_by", "rd_orphaned", "served_by", "reasoning_model", "request_id",
                                    "tool_request_ids", "step", "tools_key"})
#: The private fields a provider's converter reads before it builds its wire
#: format: whose reasoning a message carries, whether its chain is broken, and
#: who wrote a note (a wake opens a conversation, a hook's block does not).
#: The batch queue keeps them on the way to a backend; every backend builds
#: its request from the fields it knows and sends none of them.
CONVERTER_FIELDS = frozenset({"reasoning_model", "rd_orphaned", "injected_by"})


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
    # Model that produced reasoning_details. Set by the agent loop, never sent
    # to a provider; a call to another model leaves those artifacts out of its
    # payload (utils/reasoning_artifacts.produced_by_another_model).
    reasoning_model: Optional[str] = None
    # Anthropic extended/adaptive thinking blocks, verbatim as returned
    # (thinking+signature / redacted_thinking+data) in the model's original
    # order. Inside a tool-use turn these MUST be echoed back COMPLETE and
    # unmodified; a PARTIAL echo is a 400 ("thinking or redacted_thinking
    # blocks in the latest assistant message cannot be modified"), so never
    # filter, dedupe or reorder them. Deliberately NOT reusing
    # reasoning_details: that list is whitelisted into the OpenRouter payload
    # and rewritten by the chain repairs there -- both would corrupt these.
    # A history mutation drops them whole like reasoning_details
    # (utils/reasoning_artifacts): a block is bound to the conversation
    # that produced it. Sent only by the native Anthropic client and batch.
    thinking_blocks: Optional[List[Dict[str, Any]]] = None
    # Model that produced them; a record, not a filter. Another Claude model
    # takes them back unchanged (Anthropic: a different model's blocks are
    # accepted, the API handles them), so a fallback switch sends them too.
    thinking_model: Optional[str] = None
    # OpenRouter backend that served this turn (display name from
    # openrouter_metadata). The next request of the
    # run puts that backend first in provider.order: it holds the prompt cache
    # and is the only one that can verify the replayed encrypted reasoning
    # (llm_openai_compat.httpx_client.routing_pinned_to_last_backend). Never
    # sent to a provider. A model switch needs no reset: the pin only reorders
    # the NEW model's own backend list, so a stale name matches nothing or
    # picks among backends that model may use anyway.
    served_by: Optional[str] = None
    # Fingerprint of the tool list the request that produced this answer
    # carried. Claude 5.x binds its thinking to that list; another list later
    # unbinds it (llm_loop.fallback). Never sent to a provider.
    tools_key: Optional[str] = None
    # Hook injection tracking: identifies which plugin injected this message.
    # Used by injection hooks to find and replace their previous injections
    # instead of fragile content-based matching.
    injected_by: Optional[str] = None
    # What a hook put in FRONT of this message's own text, by the hook's
    # injected_by name -> the exact prefix (simple_prompt_inject "task_start").
    # The message stays the caller's -- injected_by would turn the task into a
    # note that every turn counter skips -- so the hook finds its prefix here
    # to keep, replace or withdraw it. Never sent.
    prefixed_by: Optional[Dict[str, str]] = None
    # The id of the run this message opened, on the first message a run stores. A
    # session read back tells its runs apart by it. Never sent.
    request_id: Optional[str] = None
    # On an assistant message with tool calls: the request id each call's tool runs
    # under, by call id, stamped as the tools start -- a run a tool starts carries
    # that id as its prefix (<id>_async_..., <id>_sub_...), so a session read back,
    # even while the call still waits, finds a sub-agent's run under the call that
    # started it. Never sent.
    tool_request_ids: Optional[Dict[str, str]] = None
    # On an assistant message: the step of the run's loop that produced it, numbered as
    # the run's live events number their steps. A session read back shows its steps by
    # it -- counting the messages instead falls behind after a step that stored nothing
    # (an empty answer is dropped). Never sent.
    step: Optional[int] = None

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


def task_text(task: Union[str, ChatMessage]) -> str:
    """The text of a run's task, for logs and results: the text parts of a
    message joined, ``[multimodal input]`` for one without any. Not
    get_text_content: attached file contents do not belong in a log line."""
    if not isinstance(task, ChatMessage):
        return task
    if isinstance(task.content, str):
        return task.content
    if isinstance(task.content, list):
        parts = [getattr(item, "text", "") for item in task.content
                 if hasattr(item, "type") and getattr(item, "type") == "text"]
        if parts:
            return " ".join(parts)
    return "[multimodal input]"


#: What the caller knows about why it abandons a streaming call, set just
#: before it closes the stream: {"fields": {...}, "reported": False}. The
#: client's own end report (the abandon) takes the fields and marks it
#: reported, so the call leaves ONE row with the client's usage and the
#: caller's reason. The stream closes in the caller's task, so the value is
#: visible there.
abandon_report: ContextVar[Optional[Dict[str, Any]]] = ContextVar("llm_abandon_report", default=None)


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

    #: The agent this client serves (set_app_title). Keys the backend a
    #: gateway routed its calls to (backend_affinity); None remembers nothing.
    served_agent: Optional[str] = None
    #: The llm_system profile this client was built for (llm.factory), None for
    #: one built another way. A run switched to this client hands it to the
    #: sub-agents it starts (llm/caller_llm.py).
    profile_name: Optional[str] = None
    #: How long that backend stays the one a new run starts on. None = the
    #: default of backend_affinity, 0 = off. Set from the model entry.
    provider_affinity_minutes: Optional[float] = None
    #: How long a step waits after a 429 before it asks this client once more instead of moving
    #: to the fallback (llm_loop/fallback.py). Set from the model entry by registry.build_client;
    #: 0 for a client built another way, the batch wrapper included (a queued job is not a call
    #: to ask again in seconds).
    rate_limit_retry_seconds: float = 0.0
    #: The response_format kinds this client's ROUTE has a wire field for
    #: (structured_output.JSON_SCHEMA / JSON_OBJECT) -- a fact about the API,
    #: not about a model. Empty for every client that has not wired the field,
    #: so it is never handed one (structured_output.supports_response_format).
    response_format_kinds: tuple = ()

    def supports_response_format(self, response_format: Any) -> bool:
        """Whether this client puts ``response_format`` on the wire: its route has the field
        AND its model entry declares the capability (capabilities.structured_output, for a
        schema and for plain JSON mode alike). A client whose route depends on more (a realtime
        session) narrows this."""
        from .structured_output import declared_support

        kind = getattr(response_format, "type", None)
        return (kind in self.response_format_kinds
                and declared_support(getattr(self, "capabilities", None), kind))

    def _require_response_format(self, response_format: Any) -> None:
        """A client handed a format it cannot put on the wire refuses the call -- dropping the
        field would return free text as if the provider had constrained it."""
        if response_format is not None:
            from .structured_output import require_response_format

            require_response_format(self, response_format)

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
                self._report_hook_failure("pre_llm_request", e)

    async def _notify_post_response(self, response_info: Dict[str, Any]) -> None:
        """Notify post-response hook if set. Errors are swallowed to not break LLM calls."""
        # Stash the served call's latency for server-level hooks. Skip
        # retry/error notifications (they carry an "error") so the value
        # reflects the response actually returned. Normal use runs one
        # chat_tools per client instance at a time → last-value is unambiguous.
        pending = abandon_report.get()
        if pending is not None and response_info.get("error") and not pending.get("reported"):
            response_info = {**response_info, **pending.get("fields", {})}
            pending["reported"] = True
        if not response_info.get("error") and response_info.get("duration_ms") is not None:
            self._last_response_duration_ms = response_info.get("duration_ms")
        # Here, not in the hooks: those are wired only while hooks are on.
        # Only the gateway's own choice: an answer from a backend WE pinned
        # (the run's history or this very affinity) says nothing new, and
        # remembering it kept one failover alive for every later run of the
        # type -- measured 03.11.2026: 608 runs held on the pricier second
        # backend for 17 h after a single failover.
        backend = (response_info.get("routing") or {}).get("selected")
        if (backend and self.served_agent and response_info.get("model")
                and not response_info.get("error")
                and not response_info.get("provider_pinned")):
            from .backend_affinity import remember
            remember(self.served_agent, response_info["model"], backend)
        if self._on_post_llm_response:
            try:
                await self._on_post_llm_response(response_info)
            except Exception as e:
                self._report_hook_failure("post_llm_response", e)

    #: Hook phases already reported as broken — per class, not per instance:
    #: clients are built per agent, and the cause is global (a signature
    #: change, a bad consumer), so one line is the point.
    _hook_failures_reported: set = set()

    def _report_hook_failure(self, phase: str, error: Exception) -> None:
        """Swallowing is right, staying silent is not.

        The call must survive a broken hook — but at DEBUG the breakage is
        invisible, and a dead dispatch means the message debugger (and every
        cost/latency consumer) quietly stops seeing LLM traffic. That exact
        silence hid a HIGH bug in the TTS client. Once per phase and error
        type, then back to DEBUG.
        """
        import logging

        log = logging.getLogger(__name__)
        marker = f"{phase}:{type(error).__name__}"
        if marker in LLMClient._hook_failures_reported:
            log.debug("%s hook error: %s", phase, error)
            return
        LLMClient._hook_failures_reported.add(marker)
        log.warning(
            "%s hooks are NOT being dispatched (%s: %s) — the message "
            "debugger and other hook consumers are blind to these calls. "
            "Reported once per error type.", phase, type(error).__name__, error)

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
        """Name the agent this client serves.

        Providers that support it show the name (OpenRouter X-Title);
        subclasses that do extend this and call it. It also keys the backend
        a gateway routed the agent's calls to (``recent_backend``).
        """
        self.served_agent = title or None

    def recent_backend(self) -> Optional[str]:
        """The backend the gateway chose for this client's agent on its model lately.

        Where a run with no history of its own starts: see backend_affinity.
        """
        from .backend_affinity import recent
        return recent(self.served_agent, getattr(self, "model", None),
                      self.provider_affinity_minutes)

    def forget_backend(self) -> None:
        """Stop starting this agent's calls on that backend -- it refused one."""
        from .backend_affinity import forget
        forget(self.served_agent, getattr(self, "model", None))

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

        A client that wires structured output takes ``response_format=`` (a
        structured_output.ResponseFormat) on ``chat_tools`` and this method, and
        lists the kinds in ``response_format_kinds``. Callers pass it only when
        set and only after ``supports_response_format`` said yes, so a client
        without the keyword is never handed one.

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
