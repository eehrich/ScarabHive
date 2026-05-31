"""
HTTPX-based LLM client with superior cancellation, timeout, and error handling.

This client uses HTTPX directly for better async control compared to the official
OpenAI client which has known hanging/timeout issues.
"""

import asyncio
import json
import logging
import random
import socket
from typing import Any, Optional
from dataclasses import dataclass

import httpx
import ssl

from agent_system.llm.clients import LLMClient
from agent_system.llm.models import LLMRateLimitError, LLMQuotaExhaustedError, LLMServerError
from agent_system.core.cancellation import CancellationToken
from agent_system.llm import openai_utils
from agent_system.llm.gemini_utils import sanitize_schema_for_gemini

logger = logging.getLogger(__name__)


@dataclass
class HTTPXTimeoutConfig:
    """Fine-grained timeout configuration for HTTPX client."""
    connect: float = 30.0      # Connection establishment timeout (incl. TLS handshake)
    read: float = 180.0        # Read timeout (waiting for response data)
    write: float = 10.0        # Write timeout (sending request data)
    pool: float = 5.0          # Pool timeout (getting connection from pool)


class HTTPXOpenAIClient(LLMClient):
    """
    HTTPX-based OpenAI API client with superior async handling.

    Advantages over official OpenAI client:
    - Native asyncio.CancelledError support (no polling required)
    - Fine-grained timeout control (connect, read, write, pool)
    - Direct HTTP error handling without exception wrapping
    - Better connection management and retry logic
    - Cleaner cancellation without complex task management
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_config: Optional[HTTPXTimeoutConfig] = None,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        rate_limit_backoff: float = 60.0,
        rate_limit_max_retries: int = 2,
        verify: Optional[bool] = None,
        context_window: Optional[int] = None,
        capabilities: Optional[dict] = None,
        parallel_tool_calls: bool = True,
        max_tokens: Optional[int] = None,
        safety_settings: Optional[dict[str, str]] = None,
        **extra_params
    ):
        # LLMClient doesn't have __init__, so no super() call needed
        self.model = model
        self.provider = "openai"
        self.context_window = context_window
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_config = timeout_config or HTTPXTimeoutConfig()
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.rate_limit_backoff = rate_limit_backoff
        self.rate_limit_max_retries = rate_limit_max_retries
        self.verify = verify
        self.parallel_tool_calls = parallel_tool_calls
        self.max_tokens = max_tokens  # Limit output tokens (None = provider default)
        self.extra_params = extra_params

        # Thinking/reasoning params — pop from extra_params to avoid raw injection
        self.thinking_level: str | None = self.extra_params.pop("thinking_level", None)
        self.thinking_budget: int | None = self.extra_params.pop("thinking_budget", None)

        # Service tier (Google Flex etc.) — set via config, injected as a
        # top-level field in the chat-completions payload. Common values:
        #   - "flex":     Google Flex Processing (cheaper, slower)
        #   - "standard": default speed/billing (often equivalent to omitting it)
        #   - "priority": fast queue (where supported, usually more expensive)
        # Popped out of extra_params so the field gets explicit per-call
        # handling (and a clear log surface) instead of opaque passthrough.
        self.service_tier: str | None = self.extra_params.pop("service_tier", None)

        # Provider routing (OpenRouter) — soft preference over the available backends.
        # Example: {"order": ["google-vertex", "google-ai-studio"], "allow_fallbacks": true}
        # Pinning to a sticky backend keeps OpenRouter's implicit prompt cache
        # warm (cache is backend-local; cross-backend load-balancing breaks it).
        # Popped from extra_params and injected as top-level "provider" field below.
        self.provider_routing: dict | None = self.extra_params.pop("provider_routing", None)

        # Store safety settings for Gemini content filtering (via OpenRouter)
        self.safety_settings = safety_settings

        self.capabilities = capabilities or {}
        self._verify: ssl.SSLContext | bool | None = None  # Normalized verify value
        
        # OpenRouter requires "usage": {"include": true} for detailed usage (cached_tokens, cost)
        # Other APIs reject this parameter with 400 Bad Request
        self._is_openrouter = "openrouter.ai" in base_url.lower()
        
        # Detect Gemini models via OpenRouter — need tool schema sanitization.
        # Gemini doesn't support certain JSON Schema keywords (additionalProperties,
        # default, format, title, oneOf, anyOf, etc.) in function declarations.
        # The native Gemini SDK client handles this via gemini_utils.sanitize_schema_for_gemini(),
        # but when routed through OpenRouter's OpenAI-compatible API, schemas pass through raw.
        self._is_gemini_via_openrouter = (
            self._is_openrouter and "gemini" in model.lower()
        )

        # Detect Anthropic/Claude models via OpenRouter — need cache_control injection.
        # OpenRouter passes cache_control through to Anthropic for prompt caching.
        self._is_anthropic_via_openrouter = (
            self._is_openrouter and "claude" in model.lower()
        )

        # Detect DeepSeek models — need special reasoning_content handling.
        # DeepSeek thinking mode requires reasoning_content on ALL assistant messages
        # (even empty string), otherwise returns HTTP 400.
        # See: https://api-docs.deepseek.com/guides/thinking_mode#tool-call
        _model_lower = model.lower()
        self._is_deepseek = (
            "deepseek" in _model_lower
            or "deepseek" in base_url.lower()
        )

        # Validate API type - HTTPX client only supports chat_completions
        if self.capabilities and hasattr(self.capabilities, 'default_api_type'):
            api_type = self.capabilities.default_api_type
            # Extract value from enum if it's an enum
            if hasattr(api_type, 'value'):
                api_type = api_type.value
            else:
                api_type = str(api_type) if api_type else 'chat_completions'

            if api_type != 'chat_completions':
                raise NotImplementedError(
                    f"HTTPX client only supports 'chat_completions' API. "
                    f"Requested API type: '{api_type}'. "
                    f"For Realtime API, use provider='openai' instead of 'openai_httpx'. "
                    f"Current model: {self.model}"
                )

        # Normalize verify: when explicitly False, create an SSLContext that disables
        # certificate verification. This is more robust across httpx/httpcore
        # backends and when using proxies that perform TLS interception.
        if self.verify is False:
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                self._verify = ctx
            except Exception:
                # Fall back to boolean False if SSLContext can't be created for any reason
                self._verify = False
        else:
            # Keep None or True as-is (None means httpx default behavior)
            self._verify = self.verify

        logger.debug(f"HTTPXOpenAIClient initialized model={model} base_url={base_url} verify={self._verify}")

        # Create timeout object for HTTPX
        self._timeout = httpx.Timeout(
            connect=self.timeout_config.connect,
            read=self.timeout_config.read,
            write=self.timeout_config.write,
            pool=self.timeout_config.pool
        )

        # HTTPX client will be created per request to ensure proper cleanup
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "ScarabHive-HTTPX/1.0"
        }

        # OpenRouter app attribution headers
        # See https://openrouter.ai/docs/api-reference/overview#headers
        # OpenRouter creates a unique app_id per (API-key, HTTP-Referer) pair.
        # We use set_app_title() to append the agent name to the referer,
        # giving each agent its own entry in the OpenRouter dashboard.
        self._openrouter_base_referer = "https://github.com/eehrich/ScarabHive"
        if self._is_openrouter:
            self._headers["HTTP-Referer"] = self._openrouter_base_referer
            self._headers["X-Title"] = "ScarabHive"

    def set_app_title(self, title: str) -> None:
        """Set per-agent OpenRouter app identity.

        OpenRouter keys apps by (API-key, HTTP-Referer). By appending the
        agent name to the referer URL each agent gets its own row in the
        OpenRouter activity dashboard.  X-Title is set to match so the
        dashboard shows a human-readable name.
        """
        if self._is_openrouter and title:
            self._headers["HTTP-Referer"] = f"{self._openrouter_base_referer}/{title}"
            self._headers["X-Title"] = title

    def _get_keepalive_socket_options(self) -> list:
        """Get TCP keep-alive socket options for the current platform.
        
        This prevents connection drops during long "thinking" pauses (e.g., DeepSeek reasoning).
        Especially important on Linux servers where firewalls/proxies may close idle connections.
        """
        options = [
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),  # Enable keep-alive
        ]
        # Linux-specific: set keep-alive timing (not available on all platforms)
        if hasattr(socket, 'TCP_KEEPIDLE'):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15))  # Start after 15s idle
        if hasattr(socket, 'TCP_KEEPINTVL'):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15))  # Probe every 15s
        if hasattr(socket, 'TCP_KEEPCNT'):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5))  # 5 probes before giving up
        return options

    def _create_multimodal_injection(self, tool_msg) -> Optional[dict]:
        """Create injected user message for multimodal tool content.
        
        Delegates to the central utility function in multimodal_tool_content.py.
        """
        from ..utils.multimodal_tool_content import create_multimodal_injection, check_vision_support
        
        # Check if model supports audio input
        supports_audio = False
        if self.capabilities:
            supports_audio = getattr(self.capabilities, 'audio_input', False)
        
        return create_multimodal_injection(
            tool_msg=tool_msg,
            supports_vision=check_vision_support(self.capabilities),
            model_name=self.model,
            supports_audio=supports_audio
        )

    # Fields accepted by the OpenAI Chat Completions API
    # Note: reasoning_content is NOT universally accepted — it's a DeepSeek extension.
    # It's handled separately in _postprocess_messages_for_provider().
    # reasoning_details is OpenRouter's pass-through of provider-side thinking blocks
    # (e.g. Gemini 3.x thought_signature). Must round-trip to upstream or Gemini 3.x
    # rejects subsequent turns with MALFORMED_FUNCTION_CALL (verified 2026-05-26).
    _API_MESSAGE_FIELDS = {"role", "content", "name", "tool_call_id", "tool_calls", "reasoning_details"}

    @staticmethod
    def _sanitize_tool_calls(tool_calls: list) -> list:
        """Strip non-standard fields from tool_calls.
        
        OpenRouter/Gemini may include extra fields like 'index' from the
        streaming delta format in non-streaming responses. When these
        contaminated tool_calls are re-sent to Gemini, they cause
        MALFORMED_FUNCTION_CALL errors.
        
        Standard OpenAI tool_call fields: id, type, function
        """
        sanitized = []
        for tc in tool_calls:
            clean = {
                "id": tc.get("id", ""),
                "type": tc.get("type", "function"),
                "function": tc.get("function", {})
            }
            # Preserve extra_content if present (Gemini thought_signature for round-trip)
            if "extra_content" in tc:
                clean["extra_content"] = tc["extra_content"]
            sanitized.append(clean)
        return sanitized

    @staticmethod
    def _sanitize_tools_for_gemini(tools: list) -> list:
        """Sanitize tool schemas for Gemini models via OpenRouter.
        
        Gemini's Function Declaration schema doesn't support JSON Schema keywords
        like additionalProperties, default, format, title, oneOf, anyOf, etc.
        The native Gemini SDK client handles this via gemini_utils.sanitize_schema_for_gemini(),
        but when routed through OpenRouter, schemas are passed through raw and cause
        MALFORMED_FUNCTION_CALL errors — especially with complex nested schemas.
        """
        sanitized = []
        for tool in tools:
            if tool.get("type") != "function" or "function" not in tool:
                sanitized.append(tool)
                continue
            func = tool["function"]
            clean_tool = {
                "type": "function",
                "function": {
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                }
            }
            params = func.get("parameters", {})
            if params:
                clean_tool["function"]["parameters"] = sanitize_schema_for_gemini(params)
            sanitized.append(clean_tool)
        return sanitized

    @classmethod
    def _sanitize_message_for_api(cls, d: dict) -> dict:
        """Keep only fields accepted by the OpenAI Chat Completions API.
        
        Strips internal BookKeeping fields like estimated_tokens, timestamp,
        content_format, etc. that would cause errors with strict providers
        like Gemini via OpenRouter.
        """
        clean = {k: v for k, v in d.items() if k in cls._API_MESSAGE_FIELDS}
        # Sanitize tool_calls sub-structures too
        if 'tool_calls' in clean and clean['tool_calls']:
            clean['tool_calls'] = cls._sanitize_tool_calls(clean['tool_calls'])
        # Preserve reasoning_content if present (handled by _postprocess_messages_for_provider)
        if 'reasoning_content' in d:
            clean['reasoning_content'] = d['reasoning_content']
        return clean

    def _apply_anthropic_cache_control(self, message_dicts: list) -> None:
        """Inject cache_control on system messages for Anthropic prompt caching via OpenRouter.

        Converts system message content to structured content blocks with
        ``cache_control: {"type": "ephemeral"}`` on the last block, enabling
        Anthropic prompt caching for 70-80% cost savings on repeated prefixes.

        Modifies message_dicts in-place.
        """
        for msg in message_dicts:
            if msg.get("role") != "system":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = [
                    {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                ]
            elif isinstance(content, list):
                # Add cache_control to the last text block
                for i in range(len(content) - 1, -1, -1):
                    if isinstance(content[i], dict) and content[i].get("type") == "text":
                        content[i]["cache_control"] = {"type": "ephemeral"}
                        break

    def _apply_anthropic_tool_cache_control(self, tools: list) -> None:
        """Add cache_control to the last tool definition for Anthropic prompt caching.

        Modifies tools in-place.
        """
        if tools:
            tools[-1]["cache_control"] = {"type": "ephemeral"}

    def _postprocess_messages_for_provider(self, message_dicts: list) -> None:
        """Post-process serialized messages for provider-specific requirements.

        DeepSeek thinking mode requires `reasoning_content` on ALL assistant messages
        (even empty string ""), otherwise returns HTTP 400:
          "Missing reasoning_content field in the assistant message"
        See: https://api-docs.deepseek.com/guides/thinking_mode#tool-call

        For non-DeepSeek providers, `reasoning_content` is stripped since it's not
        a standard OpenAI Chat Completions API field.

        Anthropic via OpenRouter: inject cache_control on system messages for
        prompt caching (70-80% cost savings).

        Modifies message_dicts in-place.
        """
        if self._is_anthropic_via_openrouter:
            self._apply_anthropic_cache_control(message_dicts)

        if self._is_deepseek:
            # DeepSeek: ensure ALL assistant messages have reasoning_content
            for msg in message_dicts:
                if msg.get("role") == "assistant":
                    msg.setdefault("reasoning_content", "")
        else:
            # Other providers: strip reasoning_content (non-standard field)
            for msg in message_dicts:
                msg.pop("reasoning_content", None)

        # Strip reasoning_details from all but the most recent assistant
        # message. Per Google's docs (and the long-standing note in
        # gemini_utils.py:548-552), thought signatures are validated for the
        # CURRENT turn; historical signatures don't help and accumulated
        # malformed blocks raise the surface area for 400s. Also a meaningful
        # token saving - encrypted blocks are routinely 4-8 KB each.
        #
        # NOT a fix for "Corrupted thought signature" 400s on its own. Those
        # are a known Gemini 3.x bug with parallel function call signatures
        # (Google's own forum acknowledges it, position-based and
        # non-deterministic). Workarounds documented elsewhere - cline's
        # approach of dropping tool_calls without reasoning_details, or
        # disabling parallel_tool_calls for Gemini 3.x - are alternatives
        # we have NOT implemented here.
        # See:
        #   https://ai.google.dev/gemini-api/docs/thought-signatures
        #   https://discuss.ai.google.dev/t/.../118936  (parallel-call bug)
        #   https://github.com/cline/cline/commit/a39f3cb  (drop-on-missing-RD)
        last_assistant_idx = -1
        for i, msg in enumerate(message_dicts):
            if msg.get("role") == "assistant":
                last_assistant_idx = i
        for i, msg in enumerate(message_dicts):
            if i != last_assistant_idx and msg.get("role") == "assistant":
                msg.pop("reasoning_details", None)

    def _build_reasoning_param(self) -> dict | None:
        """Build the ``reasoning`` parameter for providers that support it.

        OpenRouter (and the OpenAI o-series API) accept::

            "reasoning": {"effort": "high"}

        Maps ``thinking_level`` (from config) → ``effort`` value.
        ``thinking_budget`` is passed as ``max_tokens`` inside ``reasoning``
        when set (provider support varies).

        Returns:
            Dict suitable for ``payload["reasoning"]``, or *None* if no
            thinking parameters are configured.
        """
        if not self.thinking_level and not self.thinking_budget:
            return None

        reasoning: dict[str, Any] = {}
        if self.thinking_level:
            reasoning["effort"] = self.thinking_level
        if self.thinking_budget:
            reasoning["max_tokens"] = self.thinking_budget
        return reasoning

    def _filter_audio_from_content(self, content: Any) -> Any:
        """Filter and normalize content for OpenAI API.
        
        Uses shared openai_utils for consistent normalization across all OpenAI clients.
        Respects model capabilities - if model supports audio/video, keeps that content.
        
        Args:
            content: Message content (str, list, or dict)
            
        Returns:
            Normalized content for OpenAI API
        """
        # Check capabilities to determine what to allow
        allow_audio = False
        allow_video = False
        if self.capabilities:
            allow_audio = getattr(self.capabilities, 'audio_input', False)
            allow_video = getattr(self.capabilities, 'video_input', False)
        
        return openai_utils.normalize_message_content(
            content,
            allow_audio=allow_audio,
            allow_video=allow_video
        )

    async def chat(
        self,
        messages: list,
        cancellation_token: Optional[CancellationToken] = None
    ) -> str:
        """Send chat completion request without tools."""
        result = await self._make_request(messages, tools=[], cancellation_token=cancellation_token)
        return result.get("assistant", {}).get("content", "")

    async def chat_tools(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None,
        status_scope=None
    ) -> dict:
        """Send chat completion request with tools."""
        return await self._make_request(messages, tools=tools, cancellation_token=cancellation_token, status_scope=status_scope)

    async def chat_tools_streaming(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None,
        status_scope=None
    ):
        """Stream chat completion request with tools.

        Yields:
            dict: Streaming chunks with different types:
                {"type": "content_delta", "delta": str, "accumulated": str}
                {"type": "tool_call_delta", "index": int, "delta": dict}
                {"type": "final", "assistant": dict}
        """
        async for chunk in self._make_request_streaming(messages, tools=tools, cancellation_token=cancellation_token, status_scope=status_scope):
            yield chunk

    def supports_streaming(self) -> bool:
        """Check if this client supports streaming based on model capabilities."""
        # Check if capabilities explicitly disable streaming (handle both dict and object)
        if self.capabilities:
            if isinstance(self.capabilities, dict):
                return self.capabilities.get('streaming', True)
            elif hasattr(self.capabilities, 'streaming'):
                return self.capabilities.streaming
        return True  # Default to True if capabilities not set

    async def _report_status(self, status_scope, message: str) -> None:
        """Report status update if scope is available."""
        if status_scope is None:
            return
        try:
            await status_scope.progress(message)
        except Exception as e:
            logger.debug(f"Failed to report LLM status: {e}")

    async def _make_request(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None,
        status_scope=None
    ) -> dict:
        """Make the actual HTTP request with proper cancellation and error handling."""

        # Check if streaming is disabled in capabilities (handle both dict and object)
        streaming_enabled = True  # Default
        if self.capabilities:
            if isinstance(self.capabilities, dict):
                streaming_enabled = self.capabilities.get('streaming', True)
            elif hasattr(self.capabilities, 'streaming'):
                streaming_enabled = self.capabilities.streaming

        logger.debug(f"_make_request: streaming_enabled={streaming_enabled}, capabilities type={type(self.capabilities)}")

        if not streaming_enabled:
            # Use non-streaming request
            logger.debug("Using non-streaming request path")
            return await self._make_request_non_streaming(messages, tools, cancellation_token, status_scope)

        # Use streaming request (default behavior)
        logger.debug("Using streaming request path")
        final_result = None
        async for chunk in self._make_request_streaming(messages, tools, cancellation_token, status_scope):
            if chunk.get("type") == "final":
                # Extract all fields from final chunk (assistant, usage, etc.)
                final_result = {k: v for k, v in chunk.items() if k != "type"}
                break

        return final_result if final_result else {"assistant": {"role": "assistant", "content": ""}}

    async def _make_request_non_streaming(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None,
        status_scope=None
    ) -> dict:
        """Make non-streaming HTTP POST request for models that don't support streaming.

        Returns:
            dict: Response with 'assistant' key containing the assistant message
        """
        logger.debug(f"_make_request_non_streaming called for model {self.model}")

        # Build request payload - convert ChatMessage objects to dicts
        # NOTE: model_dump() is CPU-intensive for large messages (can take 150ms+ for 30+ messages)
        # Run in thread pool to avoid blocking event loop
        def _serialize_messages() -> list:
            result = []
            for msg in messages:
                if hasattr(msg, 'model_dump'):
                    d = msg.model_dump(exclude_none=True, mode='json')
                    # Remove multimodal_content from serialized dict - it's processed separately
                    d.pop('multimodal_content', None)
                    # Filter out audio/video content - not supported by Chat Completions API
                    if 'content' in d:
                        d['content'] = self._filter_audio_from_content(d['content'])
                    # Whitelist only API-accepted fields, sanitize tool_calls
                    d = HTTPXOpenAIClient._sanitize_message_for_api(d)
                    result.append(d)
                    
                    # Inject multimodal content as synthetic user message after tool response
                    if getattr(msg, 'role', None) == 'tool' and getattr(msg, 'multimodal_content', None):
                        injection = self._create_multimodal_injection(msg)
                        if injection:
                            result.append(injection)
                elif isinstance(msg, dict):
                    d = dict(msg)
                    if 'content' in d:
                        d['content'] = self._filter_audio_from_content(d['content'])
                    d = HTTPXOpenAIClient._sanitize_message_for_api(d)
                    result.append(d)
                else:
                    result.append(dict(msg))
            return result
        
        message_dicts = await asyncio.to_thread(_serialize_messages)
        self._postprocess_messages_for_provider(message_dicts)

        payload = {
            "model": self.model,
            "messages": message_dicts,
            "stream": False,  # ⚡ Disable streaming
            **self.extra_params
        }
        
        # OpenRouter: request detailed usage (cached_tokens, cost)
        if self._is_openrouter:
            payload["usage"] = {"include": True}

        # Thinking/reasoning config for thinking models (OpenRouter, DeepSeek, etc.)
        reasoning = self._build_reasoning_param()
        if reasoning:
            payload["reasoning"] = reasoning

        # Service tier (e.g. Google Flex via OpenRouter)
        if self.service_tier:
            payload["service_tier"] = self.service_tier

        # Provider routing (OpenRouter): bias toward a sticky backend so the
        # implicit prompt cache stays warm. Only honored by OpenRouter.
        if self.provider_routing and self._is_openrouter:
            payload["provider"] = self.provider_routing

        # Add max_tokens if configured (limits output length)
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens

        if tools:
            if self._is_gemini_via_openrouter:
                payload["tools"] = self._sanitize_tools_for_gemini(tools)
                logger.info(f"Sanitized {len(tools)} tool schemas for Gemini via OpenRouter (model={self.model})")
            else:
                payload["tools"] = tools
            # Anthropic via OpenRouter: add cache_control to last tool for prompt caching
            if self._is_anthropic_via_openrouter:
                self._apply_anthropic_tool_cache_control(payload["tools"])
            payload["tool_choice"] = "auto"
            # Gemini doesn't support parallel_tool_calls — it's an OpenAI-specific parameter.
            # OpenRouter may pass it through and confuse the Gemini backend.
            if self.parallel_tool_calls and not self._is_gemini_via_openrouter:
                payload["parallel_tool_calls"] = True

        # Gemini via OpenRouter: inject safety settings for content filtering
        if self._is_gemini_via_openrouter and self.safety_settings:
            payload["safety_settings"] = [
                {"category": category, "threshold": threshold}
                for category, threshold in self.safety_settings.items()
            ]

        url = f"{self.base_url}/chat/completions"

        # Notify pre-request hook (LLM-client level)
        import time as _time
        await self._notify_pre_request({
            "provider": "openai_httpx",
            "model": self.model,
            "url": url,
            "payload": payload,
            "is_streaming": False,
            "timestamp_ms": _time.time() * 1000,
        })

        # Retry logic with exponential backoff
        _request_start = _time.time()
        last_exception = None
        _effective_max = max(self.max_retries, self.rate_limit_max_retries)
        # One-shot self-healing retry for cross-backend thought-signature mismatch.
        # See _detect_body_400_signature_issue for the failure mode.
        _sig_retried = False
        for attempt in range(_effective_max + 1):
            # Check cancellation before each attempt
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")

            try:
                # Create fresh client for each request
                client_kwargs: dict[str, Any] = {"timeout": self._timeout}
                if getattr(self, "_verify", None) is not None:
                    client_kwargs["verify"] = self._verify

                async with httpx.AsyncClient(**client_kwargs) as client:
                    logger.debug(f"HTTPX non-streaming request attempt {attempt + 1}/{self.max_retries + 1} to {url}")

                    # Make regular POST request (not streaming)
                    response = await client.post(url=url, headers=self._headers, json=payload)

                    # Handle rate limiting (429) - longer backoff + jitter to avoid thundering herd
                    if response.status_code == 429:
                        retry_after = self._parse_retry_after(response.headers.get("retry-after"))
                        if attempt < self.rate_limit_max_retries:
                            base = retry_after or (self.rate_limit_backoff * (1.5 ** attempt))
                            jitter = base * random.uniform(0.0, 0.5)
                            backoff_time = base + jitter
                            logger.warning(f"Rate limited (429), retrying in {backoff_time:.0f}s (attempt {attempt + 1}/{self.rate_limit_max_retries})")
                            await self._report_status(status_scope, f"Rate limited, retry {attempt + 1}/{self.rate_limit_max_retries}: {self.model} (wait {backoff_time:.0f}s)")
                            await self._notify_retry("openai_httpx", self.model, url, False, "Rate limited (429)", attempt, self.rate_limit_max_retries + 1)
                            await self._cancellable_sleep(backoff_time, cancellation_token)
                            continue
                        # Retries exhausted - raise for fallback
                        error_text = response.text[:200] if response.text else ""
                        await self._report_status(status_scope, f"Rate limit exceeded: {self.model}")
                        # Notify response hook on error
                        _duration_ms = (_time.time() - _request_start) * 1000
                        await self._notify_post_response({
                            "provider": "openai_httpx", "model": self.model, "url": url,
                            "is_streaming": False, "duration_ms": _duration_ms,
                            "error": f"Rate limit: {error_text}", "timestamp_ms": _time.time() * 1000,
                        })
                        if "quota" in error_text.lower() or "exhausted" in error_text.lower():
                            raise LLMQuotaExhaustedError(
                                f"Quota exhausted: {error_text}",
                                provider="httpx", model=self.model, retry_after=retry_after
                            )
                        raise LLMRateLimitError(
                            f"Rate limit exceeded: {error_text}",
                            provider="httpx", model=self.model, retry_after=retry_after
                        )

                    # Handle server errors (5xx) - retry with exponential backoff
                    if response.status_code >= 500 and attempt < self.max_retries:
                        backoff_time = self.retry_backoff * (2 ** attempt)
                        logger.warning(f"Server error {response.status_code}, retrying in {backoff_time}s")
                        await self._report_status(status_scope, f"Server error, retry {attempt + 1}/{self.max_retries}: {self.model}")
                        await self._notify_retry("openai_httpx", self.model, url, False, f"Server error ({response.status_code})", attempt, self.max_retries + 1)
                        await self._cancellable_sleep(backoff_time, cancellation_token)
                        continue

                    # Check for HTTP errors (4xx client errors or exhausted retries)
                    if response.status_code >= 400:
                        error_text = response.text[:200] if response.text else ""
                        error_msg = f"HTTP {response.status_code}: {error_text}"
                        logger.error(f"HTTPX non-streaming request failed: {error_msg}")
                        _duration_ms = (_time.time() - _request_start) * 1000
                        await self._notify_post_response({
                            "provider": "openai_httpx", "model": self.model, "url": url,
                            "is_streaming": False, "duration_ms": _duration_ms,
                            "error": error_msg, "timestamp_ms": _time.time() * 1000,
                        })
                        if response.status_code >= 500:
                            raise LLMServerError(
                                error_msg, provider="httpx", model=self.model,
                                status_code=response.status_code,
                            )
                        raise httpx.HTTPStatusError(error_msg, request=response.request, response=response)

                    # Parse successful response.
                    # If JSON parsing fails (occasional truncated bodies seen
                    # from OpenRouter on large multi-MB requests), log enough
                    # diagnostics to discriminate between truncation, wrong
                    # content-type, and silent gateway errors before the
                    # outer except re-raises and triggers the retry.
                    try:
                        response_data = response.json()
                    except json.JSONDecodeError as _json_err:
                        body_bytes = response.content or b""
                        content_length_hdr = response.headers.get("content-length")
                        try:
                            cl_int = int(content_length_hdr) if content_length_hdr else None
                        except ValueError:
                            cl_int = None
                        truncated = cl_int is not None and len(body_bytes) < cl_int
                        # Sample body endpoints; encrypt-safe slicing on bytes
                        head = body_bytes[:300].decode("utf-8", errors="replace")
                        tail = body_bytes[-300:].decode("utf-8", errors="replace")
                        logger.warning(
                            "JSON decode failed on LLM response (likely truncated body). "
                            "model=%s status=%s content_type=%r content_length_hdr=%s "
                            "received_bytes=%d truncated=%s transfer_encoding=%r "
                            "cf_ray=%r server=%r error=%s",
                            self.model,
                            response.status_code,
                            response.headers.get("content-type"),
                            content_length_hdr,
                            len(body_bytes),
                            truncated,
                            response.headers.get("transfer-encoding"),
                            response.headers.get("cf-ray"),
                            response.headers.get("server"),
                            _json_err,
                        )
                        logger.warning("  body head[0:300]: %r", head)
                        logger.warning("  body tail[-300:]: %r", tail)
                        raise

                    # Body-level upstream 429 (e.g. OpenRouter proxying upstream
                    # rate-limit from OpenAI/Gemini Flex). Same backoff schedule
                    # as HTTP-status 429. Drop service_tier from the *local*
                    # payload (flex -> standard) on the first 429 so the retry
                    # tries the standard tier - without mutating self, which
                    # keeps the singleton clean for parallel requests.
                    _body_429_msg = self._detect_body_429(response_data)
                    if _body_429_msg and attempt < self.rate_limit_max_retries:
                        base = self.rate_limit_backoff * (1.5 ** attempt)
                        jitter = base * random.uniform(0.0, 0.5)
                        backoff_time = base + jitter
                        tier_note = ""
                        if payload.get("service_tier"):
                            dropped = payload.pop("service_tier")
                            tier_note = f", dropping service_tier={dropped!r}"
                        logger.warning(
                            f"Upstream 429 in response body ({_body_429_msg[:80]}), "
                            f"retrying in {backoff_time:.0f}s "
                            f"(attempt {attempt + 1}/{self.rate_limit_max_retries}){tier_note}"
                        )
                        await self._report_status(
                            status_scope,
                            f"Upstream 429, retry {attempt + 1}/{self.rate_limit_max_retries}: {self.model} (wait {backoff_time:.0f}s)"
                        )
                        await self._notify_retry(
                            "openai_httpx", self.model, url, False,
                            f"Upstream 429 body-error{tier_note}",
                            attempt, self.rate_limit_max_retries + 1,
                        )
                        await self._cancellable_sleep(backoff_time, cancellation_token)
                        continue

                    # Body-level 400: probable Gemini cross-backend thought-signature
                    # mismatch. OpenRouter routes Gemini requests between Vertex
                    # and AI Studio (per provider_routing.order + allow_fallbacks).
                    # Thought signatures are encrypted blobs keyed to the signing
                    # backend - the OTHER backend rejects them with "Corrupted
                    # thought signature". Vertex is strict and requires a valid
                    # signature; AI Studio is lenient but OR's translation layer
                    # can also mangle the signature mid-route.
                    #
                    # Recovery: replace `data` in every reasoning.encrypted block
                    # with Google's documented bypass token
                    # ("skip_thought_signature_validator"). Both Vertex and AI
                    # Studio recognize this string as a signal to skip signature
                    # validation. Structure (type, format, id, index) is left
                    # intact so OR's translation to Google's native format still
                    # works. One-shot: if the retry still 400s, fall through to
                    # the agent-level fallback chain.
                    _sig_issue = (
                        self._detect_body_400_signature_issue(response_data)
                        if not _sig_retried else None
                    )
                    if _sig_issue is not None:
                        n_patched = self._inject_signature_bypass(payload)
                        if n_patched > 0:
                            _sig_retried = True
                            response_backend = response_data.get("provider")
                            logger.warning(
                                "Body-400 retry: injecting signature bypass token "
                                "into reasoning_details (likely cross-backend "
                                "Vertex<->AI Studio routing mismatch). model=%s "
                                "response_backend=%r patched_blocks=%d detail=%r",
                                self.model, response_backend, n_patched,
                                _sig_issue[:200],
                            )
                            await self._report_status(
                                status_scope,
                                f"Signature bypass retry: {self.model}",
                            )
                            await self._notify_retry(
                                "openai_httpx", self.model, url, False,
                                "body-400 signature bypass",
                                attempt, self.max_retries + 1,
                            )
                            continue

                    # Detect Gemini MALFORMED_FUNCTION_CALL — a transient model error
                    # where identical payloads can succeed or fail non-deterministically.
                    # Retry instead of returning an empty response to the agent.
                    if self._is_gemini_malformed_response(response_data) and attempt < self.max_retries:
                        backoff_time = self.retry_backoff * (2 ** attempt)
                        logger.warning(
                            f"Gemini MALFORMED_FUNCTION_CALL (transient), "
                            f"retrying in {backoff_time}s (attempt {attempt + 1}/{self.max_retries + 1})"
                        )
                        await self._report_status(
                            status_scope,
                            f"Gemini malformed response, retry {attempt + 1}/{self.max_retries}: {self.model}"
                        )
                        await self._notify_retry("openai_httpx", self.model, url, False, "MALFORMED_FUNCTION_CALL", attempt, self.max_retries + 1, response_data=response_data)
                        await self._cancellable_sleep(backoff_time, cancellation_token)
                        continue

                    # Notify post-response hook with successful response
                    _duration_ms = (_time.time() - _request_start) * 1000
                    _usage = response_data.get("usage")
                    _finish = None
                    if response_data.get("choices"):
                        _finish = response_data["choices"][0].get("finish_reason")
                    await self._notify_post_response({
                        "provider": "openai_httpx", "model": self.model, "url": url,
                        "is_streaming": False, "duration_ms": _duration_ms,
                        "response_data": response_data,
                        "usage": _usage, "finish_reason": _finish,
                        "timestamp_ms": _time.time() * 1000,
                    })

                    # Use centralized response formatting (handles usage, tool_calls, etc.)
                    return self._format_response(response_data)

            except httpx.HTTPStatusError:
                raise  # Re-raise HTTP errors immediately
            except asyncio.CancelledError:
                raise  # Re-raise cancellation
            except Exception as e:
                last_exception = e
                err_label = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                if attempt < self.max_retries:
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(
                        f"Request failed (attempt {attempt + 1}/{self.max_retries + 1}) "
                        f"model={self.model} url={url}: {err_label}. Retrying in {backoff_time}s"
                    )
                    await self._report_status(status_scope, f"Request failed, retry {attempt + 1}/{self.max_retries}: {self.model} ({err_label})")
                    await self._notify_retry("openai_httpx", self.model, url, False, err_label, attempt, self.max_retries + 1)
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                else:
                    logger.error(
                        f"Request failed after {self.max_retries + 1} attempts "
                        f"model={self.model} url={url}: {err_label}"
                    )
                    await self._report_status(status_scope, f"Request failed after retries: {self.model} ({err_label})")
                    raise Exception(f"HTTP request failed after {self.max_retries + 1} attempts ({err_label}) url={url}") from last_exception

        # Should never reach here
        raise Exception(f"HTTP request failed after {self.max_retries + 1} attempts: {last_exception!r}") from last_exception

    async def _make_request_streaming(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None,
        status_scope=None
    ):
        """Make streaming HTTP request that yields chunks.

        Yields:
            dict: Chunks with types: content_delta, tool_call_delta, final
        """

        # Build request payload - convert ChatMessage objects to dicts
        # NOTE: model_dump() is CPU-intensive for large messages (can take 150ms+ for 30+ messages)
        # Run in thread pool to avoid blocking event loop
        def _serialize_messages() -> list:
            result = []
            for msg in messages:
                if hasattr(msg, 'model_dump'):
                    d = msg.model_dump(exclude_none=True, mode='json')
                    # Filter out audio/video content - not supported by Chat Completions API
                    if 'content' in d:
                        d['content'] = self._filter_audio_from_content(d['content'])
                    # Whitelist only API-accepted fields, sanitize tool_calls
                    d = HTTPXOpenAIClient._sanitize_message_for_api(d)
                    result.append(d)
                    
                    # Inject multimodal content as synthetic user message after tool response
                    if getattr(msg, 'role', None) == 'tool' and getattr(msg, 'multimodal_content', None):
                        injection = self._create_multimodal_injection(msg)
                        if injection:
                            result.append(injection)
                elif isinstance(msg, dict):
                    d = dict(msg)
                    if 'content' in d:
                        d['content'] = self._filter_audio_from_content(d['content'])
                    d = HTTPXOpenAIClient._sanitize_message_for_api(d)
                    result.append(d)
                else:
                    result.append(dict(msg))
            return result
        
        message_dicts = await asyncio.to_thread(_serialize_messages)
        self._postprocess_messages_for_provider(message_dicts)

        payload = {
            "model": self.model,
            "messages": message_dicts,
            "stream": True,  # ⚡ Enable streaming
            "stream_options": {"include_usage": True},  # Request usage stats in stream
            **self.extra_params
        }
        
        # OpenRouter: request detailed usage (cached_tokens, cost)
        if self._is_openrouter:
            payload["usage"] = {"include": True}

        # Thinking/reasoning config for thinking models (OpenRouter, DeepSeek, etc.)
        reasoning = self._build_reasoning_param()
        if reasoning:
            payload["reasoning"] = reasoning

        # Service tier (e.g. Google Flex via OpenRouter)
        if self.service_tier:
            payload["service_tier"] = self.service_tier

        # Provider routing (OpenRouter): bias toward a sticky backend so the
        # implicit prompt cache stays warm. Only honored by OpenRouter.
        if self.provider_routing and self._is_openrouter:
            payload["provider"] = self.provider_routing

        # Add max_tokens if configured (limits output length)
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens

        if tools:
            if self._is_gemini_via_openrouter:
                payload["tools"] = self._sanitize_tools_for_gemini(tools)
                logger.info(f"Sanitized {len(tools)} tool schemas for Gemini via OpenRouter (streaming, model={self.model})")
            else:
                payload["tools"] = tools
            # Anthropic via OpenRouter: add cache_control to last tool for prompt caching
            if self._is_anthropic_via_openrouter:
                self._apply_anthropic_tool_cache_control(payload["tools"])
            payload["tool_choice"] = "auto"
            # Gemini doesn't support parallel_tool_calls — it's an OpenAI-specific parameter.
            if self.parallel_tool_calls and not self._is_gemini_via_openrouter:
                payload["parallel_tool_calls"] = True

        # Gemini via OpenRouter: inject safety settings for content filtering
        if self._is_gemini_via_openrouter and self.safety_settings:
            payload["safety_settings"] = [
                {"category": category, "threshold": threshold}
                for category, threshold in self.safety_settings.items()
            ]

        url = f"{self.base_url}/chat/completions"

        # Notify pre-request hook (LLM-client level)
        import time as _time
        await self._notify_pre_request({
            "provider": "openai_httpx",
            "model": self.model,
            "url": url,
            "payload": payload,
            "is_streaming": True,
            "timestamp_ms": _time.time() * 1000,
        })

        # Accumulators for building complete response
        accumulated_content: list[str] = []
        accumulated_reasoning: list[str] = []  # For reasoning_content (DeepSeek, OpenAI o-series)
        accumulated_tool_calls: dict[int, dict[str, Any]] = {}  # index -> tool call data
        # OpenRouter delivers Gemini 3.x thought_signature inside reasoning_details
        # blocks (format=google-gemini-v1). Must round-trip on next turn or upstream
        # returns MALFORMED_FUNCTION_CALL. We keep blocks keyed by index so deltas
        # from the same block accumulate cleanly.
        accumulated_reasoning_details: dict[int, dict[str, Any]] = {}
        accumulated_usage = None  # usage information from final chunk
        _last_finish_reason: str | None = None  # Track finish_reason from chunks

        # Retry logic with exponential backoff
        _streaming_request_start = _time.time()
        last_exception: Exception | None = None
        _effective_max = max(self.max_retries, self.rate_limit_max_retries)
        # One-shot self-healing retry for cross-backend thought-signature
        # mismatch (mirrors the non-streaming path). See
        # _detect_body_400_signature_issue + _inject_signature_bypass.
        _sig_retried = False
        for attempt in range(_effective_max + 1):
            # Check cancellation before each attempt
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")

            # Body-level 429 retry signal — set inside chunk parsing when the
            # upstream wraps a rate-limit in a normal HTTP 200 SSE chunk.
            # Picked up after the async with block exits.
            _body_429_retry_msg: Optional[str] = None
            # Body-level 400 signature-bypass retry signal — same pattern as
            # _body_429_retry_msg, but for cross-backend signature mismatch.
            _body_400_sig_retry_msg: Optional[str] = None

            try:
                # Create fresh client for each request to avoid connection issues
                # Enable TCP keep-alive to prevent connection drops during long "thinking" pauses
                # This is especially important on Linux servers where firewalls/proxies may
                # close idle connections after ~30s
                socket_options = self._get_keepalive_socket_options()
                
                # Configure HTTP transport with socket options
                # Note: http2=True can help avoid some SSL shutdown issues on certain platforms
                # but may cause compatibility issues with some APIs, so we stick with HTTP/1.1
                transport = httpx.AsyncHTTPTransport(
                    retries=0,  # We handle retries ourselves
                    socket_options=socket_options,
                    # Disable HTTP/2 to avoid potential compatibility issues
                    http2=False
                )
                
                client_kwargs: dict[str, Any] = {
                    "timeout": self._timeout,
                    "transport": transport
                }
                # Only include verify if explicitly configured (None means use httpx default)
                if getattr(self, "_verify", None) is not None:
                    client_kwargs["verify"] = self._verify

                # Create client - we'll handle cleanup carefully to avoid SSL shutdown segfaults
                client = httpx.AsyncClient(**client_kwargs)
                try:
                    logger.debug(f"HTTPX streaming request attempt {attempt + 1}/{self.max_retries + 1} to {url}")

                    # Make streaming request
                    async with client.stream("POST", url=url, headers=self._headers, json=payload) as response:
                        # Check status code (don't use raise_for_status() - it tries to read the body)
                        if response.status_code == 429:
                            retry_after = self._parse_retry_after(response.headers.get("retry-after"))
                            if attempt < self.rate_limit_max_retries:
                                base = retry_after or (self.rate_limit_backoff * (1.5 ** attempt))
                                jitter = base * random.uniform(0.0, 0.5)
                                backoff_time = base + jitter
                                logger.warning(f"Rate limited (429), retrying in {backoff_time:.0f}s (attempt {attempt + 1}/{self.rate_limit_max_retries})")
                                await self._report_status(status_scope, f"Rate limited, retry {attempt + 1}/{self.rate_limit_max_retries}: {self.model} (wait {backoff_time:.0f}s)")
                                await self._notify_retry("openai_httpx", self.model, url, True, "Rate limited (429)", attempt, self.rate_limit_max_retries + 1)
                                await self._cancellable_sleep(backoff_time, cancellation_token)
                                continue
                            # Retries exhausted - raise for fallback
                            error_body = await response.aread()
                            error_text = error_body.decode()[:200] if error_body else ""
                            await self._report_status(status_scope, f"Rate limit exceeded: {self.model}")
                            if "quota" in error_text.lower() or "exhausted" in error_text.lower():
                                raise LLMQuotaExhaustedError(
                                    f"Quota exhausted: {error_text}",
                                    provider="httpx", model=self.model, retry_after=retry_after
                                )
                            raise LLMRateLimitError(
                                f"Rate limit exceeded: {error_text}",
                                provider="httpx", model=self.model, retry_after=retry_after
                            )

                        # Handle server errors (5xx) - retry with exponential backoff
                        if response.status_code >= 500 and attempt < self.max_retries:
                            backoff_time = self.retry_backoff * (2 ** attempt)
                            logger.warning(f"Server error {response.status_code}, retrying in {backoff_time}s")
                            await self._report_status(status_scope, f"Server error, retry {attempt + 1}/{self.max_retries}: {self.model}")
                            await self._notify_retry("openai_httpx", self.model, url, True, f"Server error ({response.status_code})", attempt, self.max_retries + 1)
                            await self._cancellable_sleep(backoff_time, cancellation_token)
                            continue

                        # Check for errors without reading body (streaming response)
                        if response.status_code >= 400:
                            # Read the error body for streaming responses
                            error_body = await response.aread()
                            error_text = error_body.decode('utf-8', errors='replace')
                            error_msg = f"HTTP {response.status_code}: {error_text[:200]}"
                            logger.error(f"HTTPX streaming request failed: {error_msg}")
                            if response.status_code >= 500:
                                raise LLMServerError(
                                    error_msg, provider="httpx", model=self.model,
                                    status_code=response.status_code,
                                )
                            raise httpx.HTTPStatusError(error_msg, request=response.request, response=response)

                        # Parse SSE stream with chunk timeout
                        # Use aiter_bytes() instead of aiter_lines() because aiter_lines()
                        # can block indefinitely inside httpcore when server keeps connection
                        # open but stops sending data. With aiter_bytes() we get smaller chunks
                        # and our timeout actually works.
                        chunk_timeout = self.timeout_config.read
                        # aiter_bytes() is async iterator that we can iterate over directly
                        line_buffer = ""
                        
                        # Create async iterator manually to apply timeout per chunk
                        byte_stream = response.aiter_bytes()
                        
                        while True:
                            if cancellation_token and cancellation_token.is_cancelled:
                                raise asyncio.CancelledError("Request cancelled during streaming")
                            
                            try:
                                # Get next chunk with timeout
                                chunk_bytes = await asyncio.wait_for(byte_stream.__anext__(), timeout=chunk_timeout)
                                line_buffer += chunk_bytes.decode('utf-8', errors='replace')
                            except StopAsyncIteration:
                                # Stream completed - process any remaining data in buffer
                                break
                            except asyncio.TimeoutError:
                                logger.warning(f"HTTPX stream chunk timeout after {chunk_timeout}s")
                                raise httpx.RemoteProtocolError(f"Stream stalled - no data for {chunk_timeout}s")
                            
                            # Process complete lines from buffer
                            while '\n' in line_buffer:
                                line, line_buffer = line_buffer.split('\n', 1)
                                line = line.strip()
                                
                                if not line or not line.startswith("data: "):
                                    continue

                                data = line[6:]  # Remove "data: " prefix

                                if data == "[DONE]":
                                    # Stream finished - yield final result
                                    assistant = {
                                        "role": "assistant",
                                        "content": "".join(accumulated_content) if accumulated_content else ""
                                    }

                                    # Add reasoning_content if any (DeepSeek, OpenAI o-series)
                                    if accumulated_reasoning:
                                        assistant["reasoning_content"] = "".join(accumulated_reasoning)

                                    # Add tool calls if any
                                    if accumulated_tool_calls:
                                        tool_calls_list = [accumulated_tool_calls[idx] for idx in sorted(accumulated_tool_calls.keys())]
                                        assistant["tool_calls"] = tool_calls_list
                                    if accumulated_reasoning_details:
                                        assistant["reasoning_details"] = [
                                            accumulated_reasoning_details[idx]
                                            for idx in sorted(accumulated_reasoning_details.keys())
                                        ]

                                    final_result = {"assistant": assistant}

                                    # Add usage if available
                                    if accumulated_usage:
                                        final_result["usage"] = accumulated_usage

                                    # Notify post-response hook for streaming
                                    _s_duration = (_time.time() - _streaming_request_start) * 1000
                                    await self._notify_post_response({
                                        "provider": "openai_httpx", "model": self.model,
                                        "url": url, "is_streaming": True,
                                        "duration_ms": _s_duration,
                                        "usage": accumulated_usage,
                                        "finish_reason": _last_finish_reason,
                                        "timestamp_ms": _time.time() * 1000,
                                    })

                                    yield {"type": "final", **final_result}
                                    return  # Success - exit retry loop

                                try:
                                    chunk_data = json.loads(data)
                                except Exception:
                                    logger.debug(f"Failed to parse chunk data: {data[:100]}")
                                    continue

                                # Upstream-error chunk (e.g. OpenRouter wrapping a
                                # provider 429 as a body-error inside SSE). Signal
                                # the outer attempt loop to retry and break out of
                                # the chunk parser cleanly.
                                _body_err_msg = self._detect_body_429(chunk_data)
                                if _body_err_msg:
                                    _body_429_retry_msg = _body_err_msg
                                    break  # exit "while '\n' in line_buffer"

                                # Body-level 400 (Gemini cross-backend signature
                                # mismatch) - mirror of the non-streaming path.
                                if not _sig_retried:
                                    _sig_err = self._detect_body_400_signature_issue(chunk_data)
                                    if _sig_err:
                                        _body_400_sig_retry_msg = _sig_err
                                        break

                                # Track usage if available in chunk
                                if "usage" in chunk_data:
                                    accumulated_usage = chunk_data["usage"]

                                # Process chunk
                                choices = chunk_data.get("choices", [])
                                if not choices:
                                    continue

                                choice = choices[0]
                                delta = choice.get("delta", {})

                                # Track finish_reason from chunks
                                _fr = choice.get("finish_reason")
                                if _fr:
                                    _last_finish_reason = _fr

                                # Handle reasoning_content delta (DeepSeek, OpenAI o-series thinking)
                                # This comes BEFORE the actual content in thinking models
                                if "reasoning_content" in delta and delta["reasoning_content"]:
                                    accumulated_reasoning.append(delta["reasoning_content"])
                                    yield {
                                        "type": "thinking_delta",
                                        "delta": delta["reasoning_content"],
                                        "accumulated": "".join(accumulated_reasoning)
                                    }

                                # Capture reasoning_details verbatim (Gemini 3.x thought_signature).
                                # OpenRouter delivers the encrypted signature here keyed by index;
                                # required on round-trip or upstream returns MALFORMED_FUNCTION_CALL.
                                if "reasoning_details" in delta and delta["reasoning_details"]:
                                    for rd in delta["reasoning_details"]:
                                        rd_index = rd.get("index", 0)
                                        if rd_index in accumulated_reasoning_details:
                                            # Merge subsequent fragments — append `data` if both have it
                                            existing = accumulated_reasoning_details[rd_index]
                                            for k, v in rd.items():
                                                if k == "data" and existing.get("data"):
                                                    existing["data"] += v
                                                else:
                                                    existing[k] = v
                                        else:
                                            accumulated_reasoning_details[rd_index] = dict(rd)

                                # Handle content delta
                                if "content" in delta and delta["content"]:
                                    accumulated_content.append(delta["content"])
                                    yield {
                                        "type": "content_delta",
                                        "delta": delta["content"],
                                        "accumulated": "".join(accumulated_content)
                                    }

                                # Handle tool call deltas
                                if "tool_calls" in delta:
                                    for tc_delta in delta["tool_calls"]:
                                        index = tc_delta.get("index", 0)

                                        # Initialize tool call buffer if needed
                                        if index not in accumulated_tool_calls:
                                            accumulated_tool_calls[index] = {
                                                "id": "",
                                                "type": "function",
                                                "function": {"name": "", "arguments": ""}
                                            }

                                        # Accumulate deltas
                                        if "id" in tc_delta:
                                            accumulated_tool_calls[index]["id"] = tc_delta["id"]

                                        if "function" in tc_delta:
                                            func_delta = tc_delta["function"]
                                            if "name" in func_delta:
                                                accumulated_tool_calls[index]["function"]["name"] += func_delta["name"]
                                            if "arguments" in func_delta:
                                                accumulated_tool_calls[index]["function"]["arguments"] += func_delta["arguments"]

                                        # Yield delta with accumulated state
                                        yield {
                                            "type": "tool_call_delta",
                                            "index": index,
                                            "delta": tc_delta,
                                            "accumulated": accumulated_tool_calls[index]
                                        }

                            # Body-429 signaled from inside chunk parser — abort
                            # the chunk-fetching loop so the outer attempt loop
                            # can apply backoff and retry the whole request.
                            if _body_429_retry_msg:
                                break  # exits the outer "while True" chunk fetcher
                            # Body-400 signature-bypass signaled — same pattern.
                            if _body_400_sig_retry_msg:
                                break

                        # After stream ends, process any remaining data in buffer
                        # This handles the case where the last chunk doesn't end with \n
                        # or where [DONE] is in the buffer but wasn't processed yet
                        if line_buffer.strip():
                            for line in line_buffer.split('\n'):
                                line = line.strip()
                                if not line or not line.startswith("data: "):
                                    continue
                                data = line[6:]
                                if data == "[DONE]":
                                    # Found [DONE] in remaining buffer
                                    assistant = {
                                        "role": "assistant",
                                        "content": "".join(accumulated_content) if accumulated_content else ""
                                    }
                                    if accumulated_reasoning:
                                        assistant["reasoning_content"] = "".join(accumulated_reasoning)
                                    if accumulated_tool_calls:
                                        tool_calls_list = [accumulated_tool_calls[idx] for idx in sorted(accumulated_tool_calls.keys())]
                                        assistant["tool_calls"] = tool_calls_list
                                    if accumulated_reasoning_details:
                                        assistant["reasoning_details"] = [
                                            accumulated_reasoning_details[idx]
                                            for idx in sorted(accumulated_reasoning_details.keys())
                                        ]
                                    final_result = {"assistant": assistant}
                                    if accumulated_usage:
                                        final_result["usage"] = accumulated_usage
                                    _s_duration = (_time.time() - _streaming_request_start) * 1000
                                    await self._notify_post_response({
                                        "provider": "openai_httpx", "model": self.model,
                                        "url": url, "is_streaming": True,
                                        "duration_ms": _s_duration, "usage": accumulated_usage,
                                        "finish_reason": _last_finish_reason,
                                        "timestamp_ms": _time.time() * 1000,
                                    })
                                    yield {"type": "final", **final_result}
                                    return
                                # Try to parse remaining JSON chunks
                                try:
                                    chunk_data = json.loads(data)
                                    if "usage" in chunk_data:
                                        accumulated_usage = chunk_data["usage"]
                                    choices = chunk_data.get("choices", [])
                                    if choices:
                                        delta = choices[0].get("delta", {})
                                        if "reasoning_content" in delta and delta["reasoning_content"]:
                                            accumulated_reasoning.append(delta["reasoning_content"])
                                        if "content" in delta and delta["content"]:
                                            accumulated_content.append(delta["content"])
                                except Exception:
                                    pass
                        
                        # Stream ended without [DONE] - yield final result anyway
                        # This can happen with some API implementations
                        logger.warning("Stream ended without [DONE] marker, yielding accumulated content")
                        assistant: dict[str, Any] = {
                            "role": "assistant",
                            "content": "".join(accumulated_content) if accumulated_content else ""
                        }
                        if accumulated_reasoning:
                            assistant["reasoning_content"] = "".join(accumulated_reasoning)
                        if accumulated_tool_calls:
                            tool_calls_list = [accumulated_tool_calls[idx] for idx in sorted(accumulated_tool_calls.keys())]
                            assistant["tool_calls"] = tool_calls_list
                        if accumulated_reasoning_details:
                            assistant["reasoning_details"] = [
                                accumulated_reasoning_details[idx]
                                for idx in sorted(accumulated_reasoning_details.keys())
                            ]
                        final_result = {"assistant": assistant}
                        if accumulated_usage:
                            final_result["usage"] = accumulated_usage
                        _s_duration = (_time.time() - _streaming_request_start) * 1000
                        await self._notify_post_response({
                            "provider": "openai_httpx", "model": self.model,
                            "url": url, "is_streaming": True,
                            "duration_ms": _s_duration, "usage": accumulated_usage,
                            "finish_reason": _last_finish_reason,
                            "timestamp_ms": _time.time() * 1000,
                        })
                        yield {"type": "final", **final_result}
                        return  # Success - exit retry loop
                finally:
                    # Safely close client with timeout to avoid SSL shutdown segfaults
                    # This is critical on Linux with OpenSSL 3.x where SSL_shutdown can hang
                    try:
                        await asyncio.wait_for(client.aclose(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning("Client close timed out, forcing close")
                        # Force close without waiting for SSL shutdown
                        try:
                            await asyncio.shield(asyncio.sleep(0))  # Give event loop a tick
                        except Exception:
                            pass
                    except Exception as close_err:
                        logger.debug(f"Error during client close (ignored): {close_err}")

                # Body-level 400 signature-bypass retry for streaming (mirrors
                # the non-streaming path). Flag was set inside the SSE chunk
                # parser; inject the bypass token here and retry the same
                # attempt slot. One-shot per request via _sig_retried.
                if _body_400_sig_retry_msg and not _sig_retried:
                    n_patched = self._inject_signature_bypass(payload)
                    if n_patched > 0:
                        _sig_retried = True
                        logger.warning(
                            "Body-400 stream retry: injecting signature bypass "
                            "token into reasoning_details (likely cross-backend "
                            "Vertex<->AI Studio routing mismatch). model=%s "
                            "patched_blocks=%d detail=%r",
                            self.model, n_patched, _body_400_sig_retry_msg[:200],
                        )
                        await self._report_status(
                            status_scope,
                            f"Signature bypass retry: {self.model}",
                        )
                        await self._notify_retry(
                            "openai_httpx", self.model, url, True,
                            "body-400 signature bypass (stream)",
                            attempt, self.max_retries + 1,
                        )
                        continue

                # Body-level upstream 429 retry for streaming (mirrors the
                # non-streaming path). The flag was set inside the SSE chunk
                # parser; here we apply backoff and drop service_tier from the
                # *local* payload (flex -> standard) on the first 429 so the
                # retry tries the standard tier - without mutating
                # self.service_tier (singleton-safe).
                if _body_429_retry_msg and attempt < self.rate_limit_max_retries:
                    base = self.rate_limit_backoff * (1.5 ** attempt)
                    jitter = base * random.uniform(0.0, 0.5)
                    backoff_time = base + jitter
                    tier_note = ""
                    if payload.get("service_tier"):
                        dropped = payload.pop("service_tier")
                        tier_note = f", dropping service_tier={dropped!r}"
                    logger.warning(
                        f"Upstream 429 in stream chunk ({_body_429_retry_msg[:80]}), "
                        f"retrying in {backoff_time:.0f}s "
                        f"(attempt {attempt + 1}/{self.rate_limit_max_retries}){tier_note}"
                    )
                    await self._report_status(
                        status_scope,
                        f"Upstream 429, retry {attempt + 1}/{self.rate_limit_max_retries}: {self.model} (wait {backoff_time:.0f}s)"
                    )
                    await self._notify_retry(
                        "openai_httpx", self.model, url, True,
                        f"Upstream 429 body-error{tier_note}",
                        attempt, self.rate_limit_max_retries + 1,
                    )
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                    continue

            except asyncio.CancelledError:
                # Re-raise cancellation without wrapping
                logger.info("HTTPX streaming request cancelled by user")
                raise

            except httpx.TimeoutException as e:
                last_exception = e
                if attempt < self.max_retries:
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Request timeout, retrying in {backoff_time}s: {e}")
                    await self._report_status(status_scope, f"Timeout, retry {attempt + 1}/{self.max_retries}: {self.model}")
                    await self._notify_retry("openai_httpx", self.model, url, True, f"Timeout: {e}", attempt, self.max_retries + 1)
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                    continue
                else:
                    logger.error(f"Request timed out after {self.max_retries + 1} attempts: {e}")
                    await self._report_status(status_scope, f"Timeout after retries: {self.model}")
                    raise Exception(f"Request timed out: {e}") from e

            except httpx.HTTPStatusError as e:
                last_exception = e  # type: ignore[assignment]  # Can be HTTPStatusError, TimeoutException, or NetworkError
                if e.response.status_code >= 500 and attempt < self.max_retries:
                    # Server error - retry
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Server error {e.response.status_code}, retrying in {backoff_time}s")
                    await self._report_status(status_scope, f"Server error, retry {attempt + 1}/{self.max_retries}: {self.model}")
                    await self._notify_retry("openai_httpx", self.model, url, True, f"Server error ({e.response.status_code})", attempt, self.max_retries + 1)
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                    continue
                else:
                    # Client error or max retries exceeded
                    # Error message already in exception (we read it before raising in streaming mode)
                    error_msg = str(e)
                    logger.error(f"HTTP error (streaming): {error_msg}")
                    await self._report_status(status_scope, f"HTTP error: {self.model}")
                    raise Exception(error_msg) from e

            except (httpx.NetworkError, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                last_exception = e  # type: ignore[assignment]  # Multiple exception types possible
                if attempt < self.max_retries:
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Network/protocol error (stream interrupted), retrying in {backoff_time}s: {e}")
                    await self._report_status(status_scope, f"Network error, retry {attempt + 1}/{self.max_retries}: {self.model}")
                    await self._notify_retry("openai_httpx", self.model, url, True, f"Network error: {e}", attempt, self.max_retries + 1)
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                    continue
                else:
                    logger.error(f"Network/protocol error after {self.max_retries + 1} attempts: {e}")
                    await self._report_status(status_scope, f"Network error after retries: {self.model}")
                    raise Exception(f"Network/protocol error: {e}") from e

        # Should never reach here, but just in case
        raise Exception(f"Request failed after {self.max_retries + 1} attempts") from last_exception

    def _parse_retry_after(self, retry_after: Optional[str]) -> Optional[float]:
        """Parse Retry-After header value."""
        if not retry_after:
            return None

        try:
            # Can be seconds or HTTP date, we only handle seconds for simplicity
            return float(retry_after)
        except ValueError:
            return None

    def _parse_error_response(self, response: httpx.Response) -> str:
        """Extract error message from HTTP error response."""
        try:
            error_data = response.json()
            if "error" in error_data:
                error_info = error_data["error"]
                if isinstance(error_info, dict):
                    return error_info.get("message", f"HTTP {response.status_code}")
                else:
                    return str(error_info)
        except Exception:
            pass

        return f"HTTP {response.status_code}: {response.text[:200]}"

    def _is_gemini_malformed_response(self, response_data: dict) -> bool:
        """Check if a Gemini response contains a MALFORMED_FUNCTION_CALL error.

        This is a transient, non-deterministic Gemini model error where the model
        fails to generate valid function call JSON. Identical payloads can succeed
        or fail randomly. The response comes as HTTP 200 with finish_reason="error"
        and native_finish_reason="MALFORMED_FUNCTION_CALL", with no usable content
        or tool_calls.

        Returns True only when the response is truly unusable (no tool_calls present).
        If tool_calls ARE present despite the error, returns False so they can be used.
        """
        if not self._is_gemini_via_openrouter:
            return False

        choices = response_data.get("choices", [])
        if not choices:
            return False

        choice = choices[0]
        native_reason = choice.get("native_finish_reason", "")
        if native_reason != "MALFORMED_FUNCTION_CALL":
            return False

        # If tool_calls are present despite the error, they're usually usable
        message = choice.get("message", {})
        if message.get("tool_calls"):
            return False

        return True

    @staticmethod
    def _detect_body_429(response_data: dict) -> Optional[str]:
        """Detect upstream-429 returned as body-error in an HTTP 200 response.

        OpenRouter (and similar proxies) frequently wrap upstream provider
        rate-limits in a normal-looking HTTP 200 response with payload shape:
            {"error": {"code": 429, "message": "...too many requests..."}}

        Returns the upstream error message when detected (truthy for callers),
        or None otherwise. Treated separately from HTTP-status 429 because
        httpx's status-based retry path doesn't see body-level errors.
        """
        if not isinstance(response_data, dict):
            return None
        err = response_data.get("error")
        if not isinstance(err, dict):
            return None
        code = str(err.get("code", ""))
        msg = str(err.get("message", ""))
        is_429 = (
            code == "429"
            or "429" in code
            or "rate" in msg.lower()
            or "too many requests" in msg.lower()
        )
        return msg if is_429 else None

    @staticmethod
    def _detect_body_400_signature_issue(response_data: dict) -> Optional[str]:
        """Detect a body-level 400 that LIKELY indicates a Gemini thought-signature
        cross-backend mismatch.

        Background: For Gemini via OpenRouter, OR routes requests between Vertex
        and AI Studio backends (per provider_routing.order with allow_fallbacks=true).
        Thought signatures are encrypted blobs keyed to the signing backend - Vertex
        cannot decrypt AI Studio's signatures and vice versa. When OR routes a
        follow-up request to a different backend than the one that signed the last
        assistant turn, Google rejects with "Corrupted thought signature" 400.
        Vertex is strict; AI Studio is lenient and tolerates absence.

        Detection is permissive on purpose: OR often masks the underlying Google
        error to a generic "Provider returned error". We therefore treat ANY
        body-level 400 as a candidate for the signature-strip retry, gated by the
        actual presence of reasoning_details in the payload (otherwise stripping
        does nothing).

        Returns the underlying error string (truthy) for any body-level 400, or
        None for non-400 bodies. The caller decides whether to retry based on
        whether the payload still has reasoning_details to strip.
        """
        if not isinstance(response_data, dict):
            return None
        err = response_data.get("error")
        if not isinstance(err, dict):
            return None
        code = str(err.get("code", ""))
        if code != "400" and "400" not in code:
            return None
        msg = err.get("message", "")
        meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
        raw = meta.get("raw") if isinstance(meta, dict) else None
        # Compose a useful description for logging
        if raw:
            return f"{msg} | raw={raw[:300]}"
        return msg or "Provider returned 400"

    # Google-documented bypass token. Recognized by both Vertex AI (strict)
    # and AI Studio (lenient) as a signal to skip thought_signature validation.
    # Used in reactive recovery when OpenRouter's translation layer or its
    # backend-routing switch (Vertex <-> AI Studio) has produced a signature
    # the target backend can't validate. AI Studio also accepts
    # "context_engineering_is_the_way_to_go" but Vertex does not - so we use
    # the universal one.
    _GEMINI_SIGNATURE_BYPASS = "skip_thought_signature_validator"

    @classmethod
    def _inject_signature_bypass(cls, payload: dict) -> int:
        """Replace the `data` field of every `reasoning.encrypted` block in the
        payload's assistant messages with Google's documented bypass token.

        Returns the number of blocks that were patched. Used by the reactive
        retry on body-level 400 - the structural shape of reasoning_details is
        preserved (type, format, id, index untouched) so OpenRouter still
        translates each block into a Google `thoughtSignature` part, but the
        target backend now sees the bypass token instead of a cross-backend
        signature it can't decrypt.

        Note: only `reasoning.encrypted` blocks carry signatures. `reasoning.text`
        blocks are left alone - they're descriptive text, not signed material.
        """
        patched = 0
        for msg in payload.get("messages") or []:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            rds = msg.get("reasoning_details")
            if not rds:
                continue
            for block in rds:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "reasoning.encrypted":
                    continue
                if block.get("data") == cls._GEMINI_SIGNATURE_BYPASS:
                    continue  # already patched
                block["data"] = cls._GEMINI_SIGNATURE_BYPASS
                patched += 1
        return patched

    def _format_response(self, response_data: dict) -> dict:
        """Format OpenAI API response to our standard format."""
        try:
            logger.debug(f"Formatting response_data keys: {list(response_data.keys())}")

            # Check for provider-level errors returned inside a 200 response body
            # (e.g. OpenRouter proxying upstream errors from Qwen/Alibaba, Gemini, etc.)
            if "error" in response_data:
                error_info = response_data["error"]
                error_msg = error_info.get("message", "Unknown upstream error") if isinstance(error_info, dict) else str(error_info)
                error_code = error_info.get("code", "unknown") if isinstance(error_info, dict) else "unknown"
                logger.warning(f"Provider returned error in response body: [{error_code}] {error_msg}")
                return {"assistant": {"role": "assistant", "content": "", "error": {"message": error_msg, "type": f"upstream_error_{error_code}"}}}

            choices = response_data.get("choices", [])
            if not choices:
                return {"assistant": {"role": "assistant", "content": ""}}

            choice = choices[0]
            message = choice.get("message", {})

            # Log finish_reason for debugging (Gemini MALFORMED_FUNCTION_CALL shows up here)
            finish_reason = choice.get("finish_reason")
            tool_calls = message.get("tool_calls")

            if finish_reason and finish_reason not in ("stop", "tool_calls", "end_turn"):
                # Gemini sometimes returns MALFORMED_FUNCTION_CALL but still includes
                # valid tool_calls in the response. This is a known Gemini output bug
                # where the model partially fails to generate one of several function
                # calls. If we got usable tool_calls, log at debug and continue normally.
                native_reason = choice.get("native_finish_reason", "")
                if tool_calls:
                    tc_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                    logger.debug(
                        f"Non-standard finish_reason '{finish_reason}' "
                        f"(native: {native_reason}) but {len(tool_calls)} tool_calls "
                        f"present — using them (model={self.model}): {tc_names}"
                    )
                else:
                    logger.warning(
                        f"Non-standard finish_reason: {finish_reason} "
                        f"(native: {native_reason}, model={self.model})"
                    )
                    import json as _json
                    try:
                        choice_str = _json.dumps(choice, ensure_ascii=False, default=str)[:2000]
                        logger.warning(f"Error response choice data: {choice_str}")
                    except Exception:
                        logger.warning(f"Error response choice (raw): {choice}")

            # Build assistant response
            assistant = {
                "role": "assistant",
                "content": message.get("content", "") or ""
            }

            # Add reasoning_content if present (DeepSeek, OpenAI o-series)
            reasoning_content = message.get("reasoning_content")
            if reasoning_content:
                assistant["reasoning_content"] = reasoning_content

            # Add tool calls if present — sanitize to standard fields only.
            # Note: tool_calls was already extracted above for finish_reason handling.
            if tool_calls:
                assistant["tool_calls"] = HTTPXOpenAIClient._sanitize_tool_calls(tool_calls)

            # Capture reasoning_details verbatim. For Gemini 3.x via OpenRouter
            # this carries the encrypted thought_signature that MUST be sent back
            # on subsequent turns or Google returns MALFORMED_FUNCTION_CALL.
            reasoning_details = message.get("reasoning_details")
            if reasoning_details:
                assistant["reasoning_details"] = reasoning_details

            # Track usage if available
            usage = response_data.get("usage", {})
            logger.debug(f"Extracted usage from response_data: {usage}")

            result = {"assistant": assistant}

            if usage:
                # Pass through complete usage data (OpenAI may include additional details like cached_tokens, reasoning_tokens etc.)
                result["usage"] = usage
                logger.debug(f"Added usage to result: {result['usage']}")
            else:
                logger.debug("No usage data in response_data")

            return result

        except Exception as e:
            logger.error(f"Failed to format response: {e}, raw data: {response_data}")
            return {"assistant": {"role": "assistant", "content": ""}}


# Factory function for easy integration
def create_httpx_openai_client(
    model: str,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    verify: Optional[bool] = None,
    **kwargs
) -> HTTPXOpenAIClient:
    """Create HTTPX-based OpenAI client with sensible defaults."""
    return HTTPXOpenAIClient(
        model=model,
        api_key=api_key,
        base_url=base_url,
        verify=verify,
        **kwargs
    )