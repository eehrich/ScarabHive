from __future__ import annotations

from typing import Optional, Any, cast
import json
import re
import asyncio
import contextlib
import random
import logging
import time as _time
import httpx
from collections.abc import Mapping

from agent_system.llm.tls import httpx_verify
from agent_system.utils.id import short_id
from agent_system.llm.message_roles import (
    DEVELOPER, NOTE_CLOSE, NOTE_OPEN, USER, as_note, conversation_opener, resolve_rung,
    rung_for_position,
)
from agent_system.llm.models import (
    PRIVATE_MESSAGE_FIELDS, ChatMessage, LLMClient, LLMConnectionError, LLMQuotaExhaustedError,
    LLMRateLimitError, LLMServerError,
)
from agent_system.config.models import ModelCapabilitiesConfig
from agent_system.llm.structured_output import JSON_OBJECT, JSON_SCHEMA, ResponseFormat
from plugins.llm_common import cancellation, openai_utils
from plugins.llm_common.structured_output import chat_completions_response_format


def _ending_error(error: BaseException) -> str:
    """The post_llm_response error text for a request that ended by *error*."""
    if isinstance(error, asyncio.CancelledError):
        return str(error) or "cancelled"
    if isinstance(error, GeneratorExit):
        return "stream abandoned by the caller"
    return str(error) or type(error).__name__


def _status_of(error: BaseException) -> Optional[int]:
    """The HTTP status of an SDK or httpx status error, else None."""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _seconds(value: Any) -> Optional[float]:
    """A rate-limit header as seconds: ``20``, ``1.5s``, ``200ms``, ``6m0s``, ``1h2m``."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    try:
        return float(v)
    except ValueError:
        pass
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", v)
    if not parts or "".join(number + unit for number, unit in parts) != v:
        return None
    unit_seconds = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    return sum(float(number) * unit_seconds[unit] for number, unit in parts)


def _rate_limit_hint(error: BaseException) -> Optional[float]:
    """The wait a 429 names: Retry-After, else the later of OpenAI's two reset headers."""
    headers = getattr(getattr(error, "response", None), "headers", None)
    if not isinstance(headers, Mapping):
        return None
    hint = _seconds(headers.get("retry-after"))
    if hint is None:
        hint = max((h for h in (_seconds(headers.get("x-ratelimit-reset-requests")),
                                _seconds(headers.get("x-ratelimit-reset-tokens"))) if h), default=None)
    return hint or None


def _handshake_refusal(error: BaseException, url: str) -> Optional[httpx.HTTPStatusError]:
    """A Realtime handshake the server answered with a status other than 101, as httpx's status error."""
    from websockets.exceptions import InvalidStatus

    if not isinstance(error, InvalidStatus):
        return None
    status = error.response.status_code
    request = httpx.Request("GET", url)
    body = (error.response.body or b"").decode("utf-8", "replace")[:500]
    return httpx.HTTPStatusError(f"HTTP {status}: {body}".rstrip(": "), request=request,
                                 response=httpx.Response(status, text=body, request=request))


def _is_quota(text: str) -> bool:
    """An exhausted quota (OpenAI: insufficient_quota, "exceeded your current quota"): no wait helps."""
    text = text.lower()
    return "quota" in text or "exhausted" in text


def _usage_dict(usage: Any) -> Optional[dict]:
    """The SDK's usage object in the shape the hooks and the cost readers take."""
    if not usage:
        return None
    out: dict[str, Any] = {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                           "total_tokens": usage.total_tokens}
    details = getattr(usage, "prompt_tokens_details", None)
    if details:
        out["prompt_tokens_details"] = {"cached_tokens": getattr(details, "cached_tokens", 0) or 0}
    details = getattr(usage, "completion_tokens_details", None)
    if details:
        # As reported: a detail of the completion, never computed from it.
        out["completion_tokens_details"] = {"reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0}
    return out


class OpenAIAsyncClient(LLMClient):
    """Async client using OpenAI SDK.

    Can talk to:
      - OpenAI (default base)
      - OpenAI-compatible servers (e.g., Ollama) via base_url="http://host:port/v1"
    """

    #: Chat Completions carries both as ``response_format`` (the realtime session has no such
    #: field: supports_response_format says no there).
    response_format_kinds = (JSON_SCHEMA, JSON_OBJECT)

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None, default_extra: Optional[dict] = None, timeout: Optional[float] = None, *, max_attempts: int = 5, base_backoff: float = 2.0, min_backoff: float = 2.0, backoff_cap: float = 300.0, verify: Optional[bool] = None, context_window: Optional[int] = None, capabilities: Optional[ModelCapabilitiesConfig] = None, max_tokens: Optional[int] = None) -> None:
        try:
            from openai import AsyncOpenAI  # type: ignore
        except Exception as e:
            raise RuntimeError("openai package required for OpenAIAsyncClient") from e
        self._AsyncOpenAI = AsyncOpenAI
        # max_retries 0: the retries are this client's own (_retry_wait). The
        # SDK's two hidden ones multiplied them (5 attempts sent 15 requests on
        # a 429) and reached no hook and no status line.
        kwargs: dict = {"api_key": api_key, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        if timeout is not None:
            kwargs["timeout"] = timeout

        httpx_client = None
        if verify is not None:
            try:
                import httpx as _httpx
                httpx_client = _httpx.AsyncClient(verify=httpx_verify(verify), timeout=timeout)
            except Exception as e:
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to create custom httpx client for OpenAI, will use SDK default: {e}", exc_info=True)
                httpx_client = None

        if httpx_client is not None:
            try:
                import inspect
                params = inspect.signature(AsyncOpenAI).parameters
                supported = None
                for param in ("httpx_client", "http_client", "client"):
                    if param in params:
                        supported = param
                        break
                if supported:
                    _kwargs = dict(kwargs)
                    _kwargs[supported] = httpx_client
                    self._client = AsyncOpenAI(**_kwargs)
                else:
                    logger = logging.getLogger(__name__)
                    logger.debug("OpenAI AsyncClient does not support custom httpx_client parameter, falling back to default")
                    self._client = AsyncOpenAI(**kwargs)
            except Exception as e:
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to initialize OpenAI client with custom httpx_client: {e}", exc_info=True)
                self._client = AsyncOpenAI(**kwargs)
        else:
            self._client = AsyncOpenAI(**kwargs)

        try:
            _logger = logging.getLogger(__name__)
            _logger.debug("OpenAIAsyncClient initialized model=%s base_url=%s timeout=%s verify=%s custom_httpx=%s", model, kwargs.get('base_url'), timeout, verify, httpx_client is not None)
        except Exception as e:
            _logger = logging.getLogger(__name__)
            _logger.debug(f"Failed to log OpenAIAsyncClient initialization: {e}")

        self._retry_max_attempts = int(max_attempts)
        self._retry_base_backoff = float(base_backoff)
        self._retry_min_backoff = float(min_backoff)
        self._retry_backoff_cap = float(backoff_cap)
        self.model = model
        self.api_key = api_key  # Store for Realtime API
        self.provider = "openai"
        self.context_window = context_window
        self.max_tokens = max_tokens  # Limit output tokens (None = provider default)
        self._default_extra = default_extra or {}
        self._timeout = timeout
        self.capabilities = capabilities  # Pydantic model or None
        
        self._base_url = base_url or ""
        self._verify = verify  # the Realtime WebSocket needs it too

    def _apply_developer_rung(self, message_dicts: list) -> list:
        """Rewrite developer messages to the rung this endpoint takes.

        Chat Completions has the role -- "with o1 models and newer, `developer`
        messages replace the previous `system` messages" -- so nothing moves by
        default. The step exists because ``base_url`` can point this client at
        something else, and because a model entry must be able to lower it in
        one declared place rather than per call site.
        """
        rung = resolve_rung(getattr(self.capabilities, "developer_role", None),
                            ceiling=DEVELOPER, default=DEVELOPER,
                            route=f"the endpoint at {self._base_url or 'api.openai.com'}")
        # No early return on the developer rung: the LAST developer message rides
        # the user rung whatever the endpoint allows, because a request ending on
        # something that demands no answer gets none. The rule and its measurement
        # live in message_roles.rung_for_position; this route kept the early return
        # after the two openai_compat routes lost it, which is the argument for the
        # rule living in one place.
        last = message_dicts[-1] if message_dicts else None
        opener = conversation_opener(message_dicts)
        for d in message_dicts:
            # Fields that are ours, not the conversation's, dropped here for all three
            # serialisers -- after the opener above, which tells a wake from a note
            # by injected_by. One list, one place: each serialiser used to pop its
            # own, and the tool-calling path once sent injected_by to the API.
            for key in PRIVATE_MESSAGE_FIELDS:
                d.pop(key, None)
            if d.get("role") != DEVELOPER:
                continue
            content = d.get("content")
            target = rung_for_position(rung, last=d is last, opens=d is opener)
            if target != USER:
                d["role"] = target
                continue
            d["role"] = USER
            if isinstance(content, list):
                d["content"] = [{"type": "text", "text": NOTE_OPEN}, *content,
                                {"type": "text", "text": NOTE_CLOSE}]
            else:
                d["content"] = as_note(content if isinstance(content, str) else "")
        return message_dicts

    def _create_multimodal_injection(self, tool_msg: ChatMessage, supports_audio: Optional[bool] = None) -> Optional[dict]:
        """Create injected user message for multimodal tool content.

        Delegates to the central utility function in multimodal_tool_content.py.
        ``supports_audio`` overrides the capability: the Realtime path sends no
        audio input, so the model gets the "not supported" note instead.
        """
        from agent_system.utils.multimodal_tool_content import create_multimodal_injection, check_vision_support
        
        # Check if model supports audio input
        if supports_audio is None:
            supports_audio = bool(self.capabilities and getattr(self.capabilities, 'audio_input', False))
        
        return create_multimodal_injection(
            tool_msg=tool_msg,
            supports_vision=check_vision_support(self.capabilities),
            model_name=self.model,
            supports_audio=supports_audio
        )

    def _retry_wait(self, error: Exception, attempt: int) -> Optional[tuple[str, float]]:
        """``(label, seconds)`` when *error* is worth another attempt, else None.

        The one retry schedule of both paths: a rate limit, a 5xx, a connection
        that failed or dropped. The SDK's own retries are off (see __init__).
        """
        from openai import APIConnectionError

        status = _status_of(error)
        backoff = max(self._retry_min_backoff,
                      min(self._retry_backoff_cap, self._retry_base_backoff * (2 ** (attempt - 1))))
        if status == 429:
            hint = _rate_limit_hint(error)
            if _is_quota(str(error)) or (hint and hint > self._retry_backoff_cap):
                # No wait helps a spent quota, and one past the cap is not slept
                # here: the server blocks the model for it and falls back.
                return None
            return "Rate limit (429)", (max(hint, backoff) if hint else backoff + random.random() * 0.5)
        if status is not None and status >= 500:
            return f"Server error ({status})", backoff
        if status is None and isinstance(error, (APIConnectionError, httpx.TransportError)):
            return f"Network error: {error}", backoff
        return None

    def _final_error(self, error: Exception, attempt: int) -> Optional[Exception]:
        """What a failure no retry covers reaches the agent server as.

        The types its fallback chain reads: a rate limit blocks the model, a
        5xx and a lost connection switch profile, and any other status is the
        ``httpx.HTTPStatusError`` that switches profile (and blocks a dead key
        or model). None: not a provider failure, the caller's error dict.
        """
        from openai import APIConnectionError

        status = _status_of(error)
        text = str(error)
        if status == 429:
            retry = self._retry_wait(error, attempt)
            wait = retry[1] if retry else _rate_limit_hint(error)
            if _is_quota(text):
                return LLMQuotaExhaustedError(f"Quota exhausted: {text}", provider="openai",
                                              model=self.model, retry_after=wait)
            return LLMRateLimitError(f"Rate limit exceeded: {text}", provider="openai",
                                     model=self.model, retry_after=wait)
        if status is not None and status >= 500:
            return LLMServerError(f"HTTP {status}: {text}", provider="openai", model=self.model, status_code=status)
        if status is not None:
            if isinstance(error, httpx.HTTPStatusError):
                return error
            response = error.response  # type: ignore[attr-defined]  # an APIStatusError
            return httpx.HTTPStatusError(f"HTTP {status}: {text}", request=response.request, response=response)
        if isinstance(error, (APIConnectionError, httpx.TransportError)):
            return LLMConnectionError(f"Network/protocol error: {text}", provider="openai", model=self.model)
        return None

    async def chat(self, messages: list[ChatMessage], cancellation_token=None, *,
                   response_format: Optional[ResponseFormat] = None) -> str:
        # Before anything is sent: a format this route cannot carry is the caller's
        # mistake, not the provider's.
        self._require_response_format(response_format)
        if self.get_api_type() == 'realtime':
            # Not served on /v1/chat/completions.
            result = await self._chat_tools_realtime(messages, [], cancellation_token)
        else:
            # The tool path without tools: one retry loop, one pair of hook reports.
            # chat() had its own copy of the loop and reported to no hook at all.
            result = await self._chat_tools_chat_completions(messages, [], cancellation_token,
                                                             response_format=response_format)
        error = result["assistant"].get("error")
        if error:
            return json.dumps({"_llm_error": {"error": True, "message": error.get("message")}}, ensure_ascii=False)
        return result["assistant"].get("content") or ""

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None,
                         *, response_format: Optional[ResponseFormat] = None) -> dict:
        """Dispatch to appropriate API based on model capabilities.

        Routes to either Chat Completions API or Realtime API based on
        the model's default_api_type capability.
        """
        self._require_response_format(response_format)
        api_type = self.get_api_type()

        if api_type == 'realtime':
            return await self._chat_tools_realtime(messages, tools, cancellation_token)
        else:
            return await self._chat_tools_chat_completions(messages, tools, cancellation_token, status_scope,
                                                           response_format=response_format)

    async def _chat_tools_chat_completions(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None,
                                           response_format: Optional[ResponseFormat] = None) -> dict:
        """Original Chat Completions API implementation."""
        logger = logging.getLogger(__name__)
        
        # Status reporting helper
        async def report_status(message: str) -> None:
            if status_scope is None:
                return
            try:
                await status_scope.progress(message)
            except Exception as e:
                logger.debug(f"Failed to report LLM status: {e}")
        
        # NOTE: model_dump() is CPU-intensive for large messages, run in thread pool
        def _serialize_messages() -> list[dict]:
            result = []
            for m in messages:
                # Use model_dump() to properly serialize nested Pydantic models
                d = m.model_dump(exclude_none=True, mode='json')
                # Remove multimodal_content from serialized dict - it's processed separately
                d.pop('multimodal_content', None)
                # Kept for us, not for the API — see chat() above.
                d.pop('reasoning_content', None)

                # Filter out audio content from user messages - OpenAI Chat Completions
                # Normalize content for OpenAI API
                if isinstance(d.get('content'), list):
                    d['content'] = openai_utils.normalize_content_list(d['content'])
                    if not d['content']:
                        d['content'] = ""
                
                result.append(d)
                
                # Inject multimodal content as synthetic user message after tool response
                # OpenAI doesn't support native multimodal tool responses, so we inject
                # the content as a user message with a clear prefix
                if m.role == "tool" and m.multimodal_content:
                    injection = self._create_multimodal_injection(m)
                    if injection:
                        result.append(injection)
            return self._apply_developer_rung(result)
        
        msgs = await asyncio.to_thread(_serialize_messages)

        normalized_tools: list[dict] = []
        for idx, t in enumerate(tools):
            if not isinstance(t, dict):
                logger.warning("Skipping non-dict tool schema at index %d: %r", idx, t)
                continue
            tool_obj = dict(t)
            if "type" not in tool_obj:
                tool_obj["type"] = "function"
            if tool_obj.get("type") == "function" and "function" not in tool_obj:
                fn_fields = {k: tool_obj.get(k) for k in ("name", "description", "parameters") if k in tool_obj}
                if fn_fields:
                    for k in list(fn_fields.keys()):
                        tool_obj.pop(k, None)
                    tool_obj["function"] = fn_fields
            fn = tool_obj.get("function") if tool_obj.get("type") == "function" else None
            if tool_obj.get("type") == "function" and (not isinstance(fn, dict) or not fn.get("name")):
                logger.warning("Tool schema at index %d missing function.name; skipping: %r", idx, tool_obj)
                continue
            normalized_tools.append(tool_obj)
        if len(normalized_tools) != len(tools):
            logger.debug("Normalized tool schemas: input=%d, output=%d", len(tools), len(normalized_tools))
        tools = normalized_tools
        opts = {"model": self.model, "messages": msgs}
        # Only include tools if we have at least one tool (some providers reject empty arrays)
        if tools:
            opts["tools"] = tools
            opts["tool_choice"] = "auto"
        opts.update(self._default_extra)
        if response_format is not None:
            opts["response_format"] = chat_completions_response_format(response_format)
        if self.max_tokens:
            opts["max_tokens"] = self.max_tokens

        max_attempts = self._retry_max_attempts
        _request_start = _time.time()
        await self._notify_pre_request({
            "provider": "openai", "model": self.model,
            "url": self._base_url, "is_streaming": False,
            "timestamp_ms": _request_start * 1000,
        })

        # Every way this request ends reaches post_llm_response exactly once:
        # an answer, a refusal after the retries, a cancel, an unexpected error.
        ended = False

        async def report_end(**info: Any) -> None:
            nonlocal ended
            ended = True
            await self._notify_post_response({
                "provider": "openai", "model": self.model, "url": self._base_url, "is_streaming": False,
                "duration_ms": (_time.time() - _request_start) * 1000,
                "timestamp_ms": _time.time() * 1000, **info,
            })

        try:
            try:
                resp = None
                for attempt in range(1, max_attempts + 1):
                    if cancellation_token and cancellation_token.is_cancelled:
                        raise asyncio.CancelledError("Request cancelled by user")
                    try:
                        client_any = cast(Any, self._client)
                        if cancellation_token:
                            llm_task = asyncio.create_task(client_any.chat.completions.create(**opts))
                            resp = await cancellation.await_call(llm_task, cancellation_token)
                        else:
                            resp = await client_any.chat.completions.create(**opts)
                        break
                    except Exception as e:
                        retry = self._retry_wait(e, attempt)
                        if retry is not None and attempt < max_attempts:
                            label, wait = retry
                            await report_status(f"{label}, retry {attempt}/{max_attempts} in {wait:.0f}s: {self.model}")
                            logger.warning("OpenAI %s, retrying in %.1f sec (attempt %d/%d)", label, wait, attempt, max_attempts)
                            await self._notify_retry("openai", self.model, self._base_url, False, label, attempt - 1, max_attempts)
                            await self._cancellable_sleep(wait, cancellation_token)
                            continue
                        final = self._final_error(e, attempt)
                        if final is None or final is e:
                            raise
                        raise final from e

                choice = resp.choices[0] if resp.choices else None
                if not choice:
                    await report_end()
                    return {"assistant": {"role": "assistant", "content": ""}}
                message = choice.message
                out = {"role": "assistant", "content": getattr(message, "content", None)}
                tool_calls = getattr(message, "tool_calls", None) or []
                if tool_calls:
                    out_calls = []
                    for tc in tool_calls:
                        function = getattr(tc, "function", None)
                        name = getattr(function, "name", None) if function is not None else getattr(tc, "name", None)
                        arguments = getattr(function, "arguments", None) if function is not None else getattr(tc, "arguments", None)
                        tc_id = getattr(tc, "id", None) or f"call_{short_id()}"
                        out_calls.append({
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        })
                    out["tool_calls"] = out_calls

                # The thinking, under whichever name the provider used. Reading
                # neither meant the non-streaming path never kept any reasoning at
                # all. Only real strings count — see the streaming path.
                thinking = next(
                    (value for value in (getattr(message, "reasoning_content", None),
                                         getattr(message, "reasoning", None))
                     if isinstance(value, str) and value.strip()), None)
                if thinking:
                    out["reasoning_content"] = thinking

                result: dict[str, Any] = {"assistant": out}
                usage = _usage_dict(getattr(resp, "usage", None))
                if usage:
                    result["usage"] = usage
                finish_reason = getattr(choice, "finish_reason", None)
                if isinstance(finish_reason, str):
                    # The agent's truncation guard reads it ("length").
                    result["finish_reason"] = finish_reason
                await report_end(usage=usage, finish_reason=finish_reason)
                return result
            except (LLMRateLimitError, LLMQuotaExhaustedError, LLMServerError, LLMConnectionError,
                    httpx.HTTPStatusError):
                # Typed for the agent server's fallback: rate-limit block,
                # 5xx and transport fallback, and for a 4xx the profile
                # fallback plus the dead-key block. As an error dict they
                # went through the generic upstream-error branch instead.
                raise
            except Exception as e:
                logger.exception("OpenAI chat with tools failed: %s", e)
                await report_end(error=str(e))
                return {"assistant": {"role": "assistant", "content": "",
                                      "error": {"error": True, "type": "openai_api_error", "message": str(e)}}}
        except BaseException as error:
            # Whatever ended the request without a report -- a cancel, a
            # refusal after the retries -- is reported here, once, and
            # travels on unchanged.
            if not ended:
                await report_end(error=_ending_error(error))
            raise

    async def _chat_tools_realtime(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None) -> dict:
        """Realtime API, non-streaming: the streaming path, collected."""
        final: dict = {}
        async for event in self._chat_tools_streaming_realtime(messages, tools, cancellation_token, is_streaming=False):
            if event.get("type") == "final":
                final = event
        result: dict[str, Any] = {"assistant": final.get("assistant") or {
            "role": "assistant", "content": "",
            "error": {"error": True, "type": "realtime_api_error",
                      "message": "the Realtime stream ended without a result"}}}
        for key in ("usage", "finish_reason"):
            if final.get(key):
                result[key] = final[key]
        return result

    async def chat_tools_streaming(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None,
                                   *, response_format: Optional[ResponseFormat] = None):
        """Dispatch streaming to appropriate API based on model capabilities."""
        self._require_response_format(response_format)
        api_type = self.get_api_type()

        if api_type == 'realtime':
            inner = self._chat_tools_streaming_realtime(messages, tools, cancellation_token)
        else:
            inner = self._chat_tools_streaming_chat_completions(messages, tools, cancellation_token, status_scope,
                                                                response_format=response_format)
        # aclosing: a caller that stops reading closes this generator, and the
        # request underneath must close with it -- not whenever the garbage
        # collector gets to it -- so its end is reported while it matters.
        async with contextlib.aclosing(inner) as stream:
            async for event in stream:
                yield event

    async def _chat_tools_streaming_chat_completions(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None,
                                                     response_format: Optional[ResponseFormat] = None):
        """Original Chat Completions API streaming implementation."""
        logger = logging.getLogger(__name__)
        
        # Status reporting helper
        async def report_status(message: str) -> None:
            if status_scope is None:
                return
            try:
                await status_scope.progress(message)
            except Exception as e:
                logger.debug(f"Failed to report LLM status: {e}")
        
        # NOTE: model_dump() is CPU-intensive for large messages, run in thread pool
        def _serialize_messages() -> list[dict]:
            result = []
            for m in messages:
                d = m.model_dump(exclude_none=True, mode='json')
                # processed separately; the private fields go in _apply_developer_rung
                d.pop('multimodal_content', None)
                # Kept for us, not for the API -- see the non-streaming path.
                d.pop('reasoning_content', None)

                # Normalize content for OpenAI API
                if isinstance(d.get('content'), list):
                    d['content'] = openai_utils.normalize_content_list(d['content'])
                    if not d['content']:
                        d['content'] = ""
                
                result.append(d)
                
                # Inject multimodal content as synthetic user message after tool response
                if m.role == "tool" and m.multimodal_content:
                    injection = self._create_multimodal_injection(m)
                    if injection:
                        result.append(injection)
            return self._apply_developer_rung(result)
        
        msgs = await asyncio.to_thread(_serialize_messages)

        # Normalize tools (same as non-streaming)
        normalized_tools: list[dict] = []
        for idx, t in enumerate(tools):
            if not isinstance(t, dict):
                logger.warning("Skipping non-dict tool schema at index %d: %r", idx, t)
                continue
            tool_obj = dict(t)
            if "type" not in tool_obj:
                tool_obj["type"] = "function"
            if tool_obj.get("type") == "function" and "function" not in tool_obj:
                fn_fields = {k: tool_obj.get(k) for k in ("name", "description", "parameters") if k in tool_obj}
                if fn_fields:
                    for k in list(fn_fields.keys()):
                        tool_obj.pop(k, None)
                    tool_obj["function"] = fn_fields
            fn = tool_obj.get("function") if tool_obj.get("type") == "function" else None
            if tool_obj.get("type") == "function" and (not isinstance(fn, dict) or not fn.get("name")):
                logger.warning("Tool schema at index %d missing function.name; skipping: %r", idx, tool_obj)
                continue
            normalized_tools.append(tool_obj)

        tools = normalized_tools
        opts = {"model": self.model, "messages": msgs, "stream": True, "stream_options": {"include_usage": True}}
        if tools:
            opts["tools"] = tools
            opts["tool_choice"] = "auto"
        opts.update(self._default_extra)
        if response_format is not None:
            opts["response_format"] = chat_completions_response_format(response_format)
        if self.max_tokens:
            opts["max_tokens"] = self.max_tokens
        chunk_timeout = self._timeout if isinstance(self._timeout, (int, float)) else (self._timeout.read if hasattr(self._timeout, 'read') else 60.0)

        max_attempts = self._retry_max_attempts
        _request_start = _time.time()
        await self._notify_pre_request({
            "provider": "openai", "model": self.model,
            "url": self._base_url, "is_streaming": True,
            "timestamp_ms": _request_start * 1000,
        })

        # Every way this request ends reaches post_llm_response exactly once --
        # an answer, a refusal after the retries, a cancel, a caller that stops
        # reading. What streamed so far is billed; its usage is known only when
        # the usage chunk came before the end.
        ended = False
        accumulated_usage = None
        # Whether the caller has seen a delta of an attempt that did not finish.
        # A retry starts from scratch, so the caller is told to drop what it has
        # (stream_restart) -- otherwise it shows the text twice.
        yielded_delta = False

        async def report_end(**info: Any) -> None:
            nonlocal ended
            ended = True
            await self._notify_post_response({
                "provider": "openai", "model": self.model, "url": self._base_url, "is_streaming": True,
                "duration_ms": (_time.time() - _request_start) * 1000,
                "timestamp_ms": _time.time() * 1000, **info,
            })

        try:
            for attempt in range(1, max_attempts + 1):
                # CancelledError is what the agent server reports as a cancel; a
                # plain Exception would reach it as an upstream error (fallback).
                if cancellation_token and cancellation_token.is_cancelled:
                    raise asyncio.CancelledError("Request cancelled by user")
                if yielded_delta:
                    yielded_delta = False
                    yield {"type": "stream_restart"}

                accumulated_content = []
                accumulated_reasoning = []  # the model's thinking, if it sends any
                accumulated_tool_calls = {}
                accumulated_usage = None  # usage information from final chunk
                finish_reason = None
                stream = None
                try:
                    client_any = cast(Any, self._client)
                    # OpenAI SDK's create() is async and returns AsyncStream when awaited
                    stream = await client_any.chat.completions.create(**opts)
                    stream_iter = stream.__aiter__()

                    while True:
                        if cancellation_token and cancellation_token.is_cancelled:
                            raise asyncio.CancelledError("Request cancelled by user")

                        try:
                            chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=chunk_timeout)
                        except StopAsyncIteration:
                            break  # Stream completed normally
                        except asyncio.TimeoutError:
                            logger.warning(f"Stream chunk timeout after {chunk_timeout}s (attempt {attempt}/{max_attempts})")
                            raise httpx.RemoteProtocolError(f"Stream stalled - no data for {chunk_timeout}s")

                        # Usage arrives in the final chunk (stream_options include_usage).
                        accumulated_usage = _usage_dict(getattr(chunk, 'usage', None)) or accumulated_usage

                        choices = chunk.choices if hasattr(chunk, 'choices') else []
                        if not choices:
                            continue
                        reason = getattr(choices[0], 'finish_reason', None)
                        if isinstance(reason, str):
                            finish_reason = reason

                        delta = choices[0].delta if hasattr(choices[0], 'delta') else None
                        if not delta:
                            continue

                        # Handle the thinking delta. Two names carry one payload —
                        # DeepSeek calls it reasoning_content, OpenRouter calls it
                        # reasoning — and only one of them ever arrives. The text
                        # used to be streamed live and then dropped: nothing kept
                        # it, so no session, no debugger row and no later turn saw
                        # what the model had thought.
                        #
                        # Only real strings count: on a mock every attribute exists
                        # and is truthy, which would make this read its own noise.
                        _reasoning_delta = next(
                            (value for value in (getattr(delta, 'reasoning_content', None),
                                                 getattr(delta, 'reasoning', None))
                             if isinstance(value, str) and value), None)
                        if _reasoning_delta:
                            accumulated_reasoning.append(_reasoning_delta)
                            yielded_delta = True
                            yield {
                                "type": "thinking_delta",
                                "delta": _reasoning_delta
                            }

                        # Handle content delta
                        if hasattr(delta, 'content') and delta.content:
                            accumulated_content.append(delta.content)
                            yielded_delta = True
                            yield {
                                "type": "content_delta",
                                "delta": delta.content,
                                "accumulated": "".join(accumulated_content)
                            }

                        # Handle tool call deltas
                        if hasattr(delta, 'tool_calls') and delta.tool_calls:
                            for tc_delta in delta.tool_calls:
                                index = tc_delta.index if hasattr(tc_delta, 'index') else 0

                                if index not in accumulated_tool_calls:
                                    accumulated_tool_calls[index] = {
                                        "id": getattr(tc_delta, "id", None) or f"call_{short_id()}",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""}
                                    }

                                # Accumulate function name
                                if hasattr(tc_delta, 'function') and hasattr(tc_delta.function, 'name') and tc_delta.function.name:
                                    accumulated_tool_calls[index]["function"]["name"] += tc_delta.function.name

                                # Accumulate arguments
                                if hasattr(tc_delta, 'function') and hasattr(tc_delta.function, 'arguments') and tc_delta.function.arguments:
                                    accumulated_tool_calls[index]["function"]["arguments"] += tc_delta.function.arguments

                                # Update ID if provided
                                if hasattr(tc_delta, 'id') and tc_delta.id:
                                    accumulated_tool_calls[index]["id"] = tc_delta.id

                                yielded_delta = True
                                yield {
                                    "type": "tool_call_delta",
                                    "index": index,
                                    "delta": {
                                        "id": getattr(tc_delta, "id", None),
                                        "function": {
                                            "name": getattr(getattr(tc_delta, "function", None), "name", None),
                                            "arguments": getattr(getattr(tc_delta, "function", None), "arguments", None)
                                        }
                                    },
                                    "accumulated": accumulated_tool_calls[index]
                                }

                    # Build final message
                    assistant = {"role": "assistant", "content": "".join(accumulated_content) if accumulated_content else None}

                    # This client keeps no reasoning_details, so the message is the
                    # only home the thinking has here — no second copy to avoid.
                    if accumulated_reasoning:
                        assistant["reasoning_content"] = "".join(accumulated_reasoning)

                    if accumulated_tool_calls:
                        tool_calls_list = [accumulated_tool_calls[i] for i in sorted(accumulated_tool_calls.keys())]
                        assistant["tool_calls"] = tool_calls_list

                    final_result: dict[str, Any] = {"assistant": assistant}
                    if accumulated_usage:
                        final_result["usage"] = accumulated_usage
                    if finish_reason:
                        # The agent's truncation guard reads it ("length").
                        final_result["finish_reason"] = finish_reason

                    await report_end(usage=accumulated_usage, finish_reason=finish_reason)
                    yield {"type": "final", **final_result}
                    return  # Success - exit retry loop

                except Exception as e:
                    retry = self._retry_wait(e, attempt)
                    if retry is not None and attempt < max_attempts:
                        label, wait = retry
                        await report_status(f"{label}, retry {attempt}/{max_attempts} in {wait:.0f}s: {self.model}")
                        logger.warning("OpenAI stream: %s, retrying in %.1f sec (attempt %d/%d)", label, wait, attempt, max_attempts)
                        await self._notify_retry("openai", self.model, self._base_url, True, label, attempt - 1, max_attempts)
                        await self._cancellable_sleep(wait, cancellation_token)
                        continue
                    final = self._final_error(e, attempt)
                    if final is e:
                        raise
                    if final is not None:
                        await report_status(f"Stream failed after {attempt} attempts: {self.model}")
                        raise final from e
                    logger.exception("OpenAI streaming failed: %s", e)
                    await report_end(error=str(e))
                    # The shape the agent server reads (message/type).
                    yield {"type": "final", "assistant": {"role": "assistant", "content": "",
                                                          "error": {"error": True, "type": "openai_api_error", "message": str(e)}}}
                    return
                finally:
                    # The SDK stream holds the HTTP response: close it now, not
                    # whenever the garbage collector gets to it.
                    if stream is not None:
                        with contextlib.suppress(Exception):
                            await stream.close()
        except BaseException as error:
            # Whatever ended the request without a report -- a cancel, a cancel
            # during a retry wait, a caller abandoning the stream (GeneratorExit;
            # awaiting is allowed while it closes), a refusal after the retries --
            # is reported here, once, and travels on unchanged.
            if not ended:
                await report_end(error=_ending_error(error), usage=accumulated_usage)
            raise

    async def _chat_tools_streaming_realtime(self, messages: list[ChatMessage], tools: list[dict],
                                             cancellation_token=None, is_streaming: bool = True):
        """Realtime API (GA): one ``response.create`` over one WebSocket.

        The whole history goes as ``input`` with ``conversation: "none"``, the
        output is text; deltas are streamed, the result comes from
        ``response.done`` (see realtime_adapter).
        """
        logger = logging.getLogger(__name__)
        from . import realtime_adapter as adapter
        from .realtime_session import RealtimeSession, realtime_url

        def failed(message: str, kind: str = "realtime_api_error") -> dict:
            # The shape the agent server reads (message/type), as the httpx client sends it. A type the server
            # sent is prefixed ``upstream_error_``, as the httpx and Responses clients do: the agent yields it as
            # its run's error_type, and a type of the framework's own -- a refusal before the run
            # (REFUSED_BEFORE_THE_RUN) -- would make a run that did run count as refused.
            return {"type": "final", "assistant": {"role": "assistant", "content": "",
                                                   "error": {"error": True, "type": kind, "message": message}}}

        def check_cancelled() -> None:
            # CancelledError is what the agent server reports as a cancel.
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")

        check_cancelled()
        # Tool attachments are read from disk and encoded: not on the event loop.
        instructions, items = await asyncio.to_thread(
            adapter.to_request_input, messages,
            attachment=lambda msg: self._create_multimodal_injection(msg, supports_audio=False))
        response: dict[str, Any] = {"conversation": "none", "input": items}
        if instructions:
            response["instructions"] = instructions
        realtime_tools = adapter.to_realtime_tools(tools or [])
        if realtime_tools:
            response["tools"] = realtime_tools
            response["tool_choice"] = "auto"
        if self.max_tokens:
            # The ceiling is the model's (gpt-realtime(-mini): 4096, 2.1: 32000).
            response["max_output_tokens"] = self.max_tokens
        if self._default_extra and not getattr(self, "_realtime_extra_reported", False):
            # temperature and modalities are not part of the GA Realtime API.
            self._realtime_extra_reported = True
            logger.warning("Realtime API: %s not sent (model=%s)", sorted(self._default_extra), self.model)
        event_timeout = (self._timeout if isinstance(self._timeout, (int, float))
                         else getattr(self._timeout, "read", None) or 60.0)

        url = realtime_url(self._base_url)
        ssl_context = httpx_verify(self._verify) if self._verify is not None and url.startswith("wss:") else None
        hook = {"provider": "openai", "model": self.model, "url": self._base_url, "is_streaming": is_streaming}
        request_start = _time.time()
        await self._notify_pre_request({**hook, "timestamp_ms": request_start * 1000})

        ended = False

        async def notify_done(**info) -> None:
            nonlocal ended
            ended = True
            await self._notify_post_response({**hook, "duration_ms": (_time.time() - request_start) * 1000,
                                              "timestamp_ms": _time.time() * 1000, **info})

        async def next_event(session: RealtimeSession) -> dict:
            # A silent model must not hold a cancel until the event timeout.
            if not cancellation_token:
                return await session.next_event(event_timeout)
            read = asyncio.ensure_future(session.next_event(event_timeout))
            try:
                while not read.done():
                    check_cancelled()
                    await asyncio.wait({read}, timeout=0.1)
            finally:
                read.cancel()  # no-op once it is done
            return read.result()

        content: list[str] = []
        calls: dict[str, dict] = {}  # call_id -> tool call, in output order

        def tool_delta(call: dict, delta: dict) -> dict:
            return {"type": "tool_call_delta", "index": list(calls).index(call["id"]),
                    "delta": delta, "accumulated": call}

        try:
            async with RealtimeSession(self.model, self.api_key, url, ssl_context) as session:
                check_cancelled()
                # Text output is set on the session: in response.create it makes
                # gpt-realtime-2.1 report input_tokens 0 (measured 2026-09-17).
                await session.send_event({"type": "session.update",
                                          "session": {"type": "realtime", "output_modalities": ["text"]}})
                await session.send_event({"type": "response.create", "response": response})
                while True:
                    event = await next_event(session)
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        content.append(event.get("delta", ""))
                        yield {"type": "content_delta", "delta": event.get("delta", ""),
                               "accumulated": "".join(content)}
                    elif kind == "response.output_item.added" and (event.get("item") or {}).get("type") == "function_call":
                        # The name comes only here; the argument deltas carry the call_id.
                        item = event["item"]
                        call = {"id": item.get("call_id"), "type": "function",
                                "function": {"name": item.get("name"), "arguments": ""}}
                        calls[call["id"]] = call
                        yield tool_delta(call, {"id": call["id"], "function": {"name": item.get("name"), "arguments": ""}})
                    elif kind == "response.function_call_arguments.delta":
                        call = calls.get(event.get("call_id"))
                        if call is not None:
                            call["function"]["arguments"] += event.get("delta", "")
                            yield tool_delta(call, {"id": None, "function": {"name": None, "arguments": event.get("delta", "")}})
                    elif kind == "error":
                        error = event.get("error") or {}
                        logger.error("Realtime API error: %s", error)
                        await notify_done(error=error.get("message", "Unknown error"))
                        yield failed(error.get("message", "Unknown error"),
                                     f"upstream_error_{error.get('type', 'unknown')}")
                        return
                    elif kind == "response.done":
                        done = event.get("response") or {}
                        usage = adapter.to_openai_usage(done.get("usage"))
                        finish_reason, error = adapter.outcome(done)
                        if error:
                            await notify_done(usage=usage, finish_reason=done.get("status"), error=error["message"])
                            final = failed(error["message"], f"upstream_error_{error['type']}")
                        else:
                            assistant = adapter.to_assistant_message(done)
                            await notify_done(usage=usage, finish_reason=finish_reason or (
                                "tool_calls" if assistant.get("tool_calls") else "stop"))
                            final = {"type": "final", "assistant": assistant}
                        if finish_reason:
                            final["finish_reason"] = finish_reason
                        if usage:
                            final["usage"] = usage
                        yield final
                        return
        except Exception as e:
            refused = _handshake_refusal(e, url)
            if refused is not None:
                # A refused handshake (401/403/404: dead key or model) is a status
                # error like on the HTTP paths, so the server blocks and falls back.
                final = self._final_error(refused, 1) or refused
                await notify_done(error=str(final))
                raise final from e
            logger.exception("Realtime API call failed: %s", e)
            await notify_done(error=str(e))
            yield failed(str(e))
        except BaseException as error:
            # A cancel, or a caller abandoning the stream (GeneratorExit): the
            # socket was open, what the model generated is billed.
            if not ended:
                await notify_done(error=_ending_error(error))
            raise

    def supports_response_format(self, response_format: Any) -> bool:
        """As every client -- except on the realtime session, which has no such field."""
        return self.get_api_type() != 'realtime' and super().supports_response_format(response_format)

    def supports_streaming(self) -> bool:
        """Check if this client supports streaming based on model capabilities."""
        # Check if capabilities explicitly disable streaming
        if self.capabilities and hasattr(self.capabilities, 'streaming'):
            return self.capabilities.streaming
        return True  # Default to True if capabilities not set

    def get_api_type(self) -> str:
        """Get the API type this client will use based on capabilities.

        Returns:
            API type string: 'chat_completions', 'assistants', or 'realtime'

        Raises:
            NotImplementedError: If 'assistants' API type is configured
        """
        if not self.capabilities:
            return 'chat_completions'

        # Access Pydantic model attribute
        if not hasattr(self.capabilities, 'default_api_type') or not self.capabilities.default_api_type:
            return 'chat_completions'

        api_type = self.capabilities.default_api_type

        # Extract value from enum if it's an enum
        if hasattr(api_type, 'value'):
            api_type = api_type.value
        else:
            api_type = str(api_type)

        # Validate that Assistants API is not used
        if api_type == 'assistants':
            raise NotImplementedError(
                "Assistants API is not implemented. "
                "The AgentSystem already provides its own session management and state handling. "
                "Please use 'chat_completions' (default) or 'realtime' API instead. "
                f"Current model: {self.model}"
            )

        return api_type

    def supports_api_type(self, api_type: str) -> bool:
        """Check if this client supports a specific API type.

        Args:
            api_type: API type to check ('chat_completions', 'assistants', 'realtime')

        Returns:
            True if the API type is supported
        """
        if not self.capabilities:
            return api_type == 'chat_completions'  # Default only supports chat

        # Check if supported_api_types list exists in Pydantic model
        if not hasattr(self.capabilities, 'supported_api_types') or not self.capabilities.supported_api_types:
            return api_type == 'chat_completions'

        # supported_api_types is List[str] in Pydantic model
        return api_type in self.capabilities.supported_api_types
