from __future__ import annotations

from typing import Optional, Any, cast
import json
import asyncio
import random
import logging
import time as _time
import httpx

from agent_system.llm.tls import httpx_verify
from agent_system.utils.id import short_id
from agent_system.utils.json_utils import repair_json
from agent_system.llm.message_roles import (
    DEVELOPER, NOTE_CLOSE, NOTE_OPEN, USER, as_note, conversation_opener, resolve_rung,
    rung_for_position,
)
from agent_system.llm.models import (
    PRIVATE_MESSAGE_FIELDS, ChatMessage, LLMClient, LLMRateLimitError, LLMQuotaExhaustedError,
)
from agent_system.config.models import ModelCapabilitiesConfig
from agent_system.llm.structured_output import JSON_OBJECT, JSON_SCHEMA, ResponseFormat
from plugins.llm_common import cancellation, openai_utils
from plugins.llm_common.structured_output import chat_completions_response_format


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
        kwargs: dict = {"api_key": api_key}
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

    async def chat(self, messages: list[ChatMessage], cancellation_token=None, *,
                   response_format: Optional[ResponseFormat] = None) -> str:
        logger = logging.getLogger(__name__)
        # Before the try below, which turns every failure into an error text: a format this
        # route cannot carry is the caller's mistake, not the provider's.
        self._require_response_format(response_format)
        if self.get_api_type() == 'realtime':
            # Not served on /v1/chat/completions.
            result = await self._chat_tools_realtime(messages, [], cancellation_token)
            error = result["assistant"].get("error")
            if error:
                return json.dumps({"_llm_error": {"error": True, "message": error.get("message")}}, ensure_ascii=False)
            return result["assistant"].get("content") or ""
        try:
            # NOTE: model_dump() is CPU-intensive for large messages, run in thread pool
            def _serialize() -> list:
                result = []
                for m in messages:
                    d = m.model_dump(exclude_none=True, mode='json')
                    # Kept for us, not for the API: reasoning_content is a
                    # DeepSeek extension and unknown here. Every sibling client
                    # drops it before the request; this one now produces it, so
                    # it has to drop it too.
                    d.pop('reasoning_content', None)
                    # Normalize content for OpenAI API
                    if isinstance(d.get('content'), list):
                        d['content'] = openai_utils.normalize_content_list(d['content'])
                        if not d['content']:
                            d['content'] = ""
                    result.append(d)
                return self._apply_developer_rung(result)
            serialized = await asyncio.to_thread(_serialize)
            
            opts = {"model": self.model, "messages": serialized}
            opts.update(self._default_extra)
            if response_format is not None:
                opts["response_format"] = chat_completions_response_format(response_format)
            
            # Add max_tokens if configured
            if self.max_tokens:
                opts["max_tokens"] = self.max_tokens
                
            max_attempts = self._retry_max_attempts
            base_backoff = self._retry_base_backoff
            resp = None

            def _parse_reset(val: Optional[str]) -> Optional[float]:
                if not val:
                    return None
                v = str(val).strip()
                try:
                    if v.endswith("ms"):
                        return float(v[:-2]) / 1000.0
                    if v.endswith("s"):
                        return float(v[:-1])
                    return float(v)
                except Exception:
                    return None

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
                    status = None
                    resp_obj = getattr(e, "response", None)
                    if isinstance(e, httpx.HTTPStatusError) or (resp_obj is not None):
                        try:
                            status = int(getattr(resp_obj, "status_code", None) or 0)
                        except Exception:
                            status = None
                    if status == 429:
                        retry_after = None
                        try:
                            hdr = getattr(resp_obj, "headers", {}) or {}
                            ra = hdr.get("retry-after")
                            if ra is not None:
                                try:
                                    retry_after = float(ra)
                                except Exception:
                                    retry_after = None
                            if retry_after is None:
                                reset_req = _parse_reset(hdr.get("x-ratelimit-reset-requests"))
                                reset_tok = _parse_reset(hdr.get("x-ratelimit-reset-tokens"))
                                reset_suggest = None
                                if reset_req is not None and reset_req > 0:
                                    reset_suggest = reset_req
                                if reset_tok is not None and (reset_suggest is None or reset_tok > reset_suggest):
                                    reset_suggest = reset_tok
                                if reset_suggest is not None:
                                    computed = min(60.0, base_backoff * (2 ** (attempt - 1)))
                                    retry_after = max(reset_suggest, computed)
                        except Exception:
                            retry_after = None
                        if retry_after is not None:
                            wait = max(retry_after, self._retry_min_backoff, min(self._retry_backoff_cap, base_backoff * (2 ** (attempt - 1))))
                        else:
                            wait = max(self._retry_min_backoff, min(self._retry_backoff_cap, base_backoff * (2 ** (attempt - 1)))) + random.random() * 0.5
                        if attempt < max_attempts:
                            logger.warning("OpenAI rate limited (429). retrying in %.1f sec (attempt %d/%d)", wait, attempt, max_attempts)
                            if cancellation_token and cancellation_token.is_cancelled:
                                raise asyncio.CancelledError("Request cancelled by user during rate limit backoff")
                            await self._notify_retry("openai", self.model, self._base_url, False, "Rate limit (429)", attempt - 1, max_attempts)
                            await self._cancellable_sleep(wait, cancellation_token)
                            continue
                        # Retries exhausted - raise a typed error so callers can
                        # switch to a fallback profile (mirrors
                        # _chat_tools_chat_completions; the old code slept a
                        # full backoff AFTER the last attempt and then returned
                        # an error string as if it were an answer).
                        error_text = str(e)
                        if "quota" in error_text.lower() or "exhausted" in error_text.lower():
                            raise LLMQuotaExhaustedError(
                                f"Quota exhausted: {error_text}",
                                provider="openai", model=self.model, retry_after=wait
                            )
                        raise LLMRateLimitError(
                            f"Rate limit exceeded: {error_text}",
                            provider="openai", model=self.model, retry_after=wait
                        )
                    # Handle server errors (5xx) - retry with exponential backoff
                    if status is not None and status >= 500 and attempt < max_attempts:
                        wait = max(self._retry_min_backoff, min(self._retry_backoff_cap, base_backoff * (2 ** (attempt - 1))))
                        logger.warning("OpenAI server error (%d). retrying in %.1f sec (attempt %d/%d)", status, wait, attempt, max_attempts)
                        if cancellation_token and cancellation_token.is_cancelled:
                            raise asyncio.CancelledError("Request cancelled by user during server error backoff")
                        await self._notify_retry("openai", self.model, self._base_url, False, f"Server error ({status})", attempt - 1, max_attempts)
                        await self._cancellable_sleep(wait, cancellation_token)
                        continue
                    if status == 400:
                        error_text = str(e)
                        if "context_length_exceeded" in error_text or "Input tokens exceed" in error_text:
                            from agent_system.context.exceptions import ContextLengthExceededError
                            raise ContextLengthExceededError(
                                message=f"OpenAI context length exceeded: {error_text}",
                                original_exception=e
                            )
                    raise
            # Check if resp is None after retry loop (e.g., all attempts failed with rate limiting)
            if resp is None:
                return json.dumps({"_llm_error": {"error": True, "message": f"OpenAI API request failed after {max_attempts} attempts (rate limiting or other errors)"}}, ensure_ascii=False)
            
            try:
                logger.debug("OpenAI resp id=%s choices=%d", getattr(resp, "id", None), len(getattr(resp, "choices", []) or []))
            except Exception as e:
                logger.debug(f"Failed to log OpenAI response info: {e}")
            choice = resp.choices[0] if resp.choices else None
            if not choice:
                return ""
            message = choice.message
            content = getattr(message, "content", None)
            if content:
                return content
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                try:
                    first = tool_calls[0]
                    function = getattr(first, "function", None)
                    name = getattr(function, "name", None) if function is not None else getattr(first, "name", None)
                    arguments = getattr(function, "arguments", None) if function is not None else getattr(first, "arguments", None)
                    params = {}
                    if isinstance(arguments, str):
                        try:
                            params = json.loads(arguments)
                        except Exception:
                            repaired = repair_json(arguments)
                            if repaired is not None and isinstance(repaired, dict):
                                logger.info("Repaired malformed tool args from OpenAI for %s", name)
                                params = repaired
                            else:
                                logger.warning("Failed to parse/repair tool call arguments for %s", name)
                    elif isinstance(arguments, dict):
                        params = arguments
                    if name:
                        return json.dumps({"type": "tool", "tool": name, "params": params}, ensure_ascii=False)
                except Exception as e:
                    logger.debug(f"Failed to extract tool call from OpenAI response: {e}")
            try:
                return getattr(message, "content", None) or ""
            except Exception as e:
                logger.debug(f"Failed to extract content from message: {e}")
                return ""
        except (LLMRateLimitError, LLMQuotaExhaustedError):
            # Raised intentionally after retry exhaustion -- callers use these
            # to switch to a fallback profile (same passthrough as chat_tools).
            raise
        except Exception as e:
            try:
                status = None
                resp_obj = getattr(e, "response", None)
                if resp_obj is not None:
                    try:
                        status = int(getattr(resp_obj, "status_code", None) or 0)
                    except Exception as e:
                        logger.debug(f"Failed to extract status code from error response: {e}")
                        status = None
                err_payload: dict[str, Any] = {"error": str(e), "status": status}
                if resp_obj is not None:
                    try:
                        data = getattr(resp_obj, "json", lambda: None)()
                        if data:
                            err_payload["response_json"] = data
                    except Exception as e:
                        logger.debug(f"Failed to extract JSON from OpenAI error response: {e}")
                logger.exception("OpenAI chat failed: %s", e)
                return json.dumps({"_llm_error": err_payload}, ensure_ascii=False)
            except Exception:
                logger.exception("OpenAI chat failed (secondary error building payload): %s", e)
                return ""

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
        try:
            opts = {"model": self.model, "messages": msgs}
            
            # Only include tools if we have at least one tool (some providers reject empty arrays)
            if tools:
                opts["tools"] = tools
                opts["tool_choice"] = "auto"

            opts.update(self._default_extra)
            if response_format is not None:
                opts["response_format"] = chat_completions_response_format(response_format)
            
            # Add max_tokens if configured
            if self.max_tokens:
                opts["max_tokens"] = self.max_tokens
                
            max_attempts = self._retry_max_attempts
            base_backoff = self._retry_base_backoff
            resp = None
            _request_start = _time.time()

            await self._notify_pre_request({
                "provider": "openai", "model": self.model,
                "url": self._base_url, "is_streaming": False,
                "timestamp_ms": _request_start * 1000,
            })

            def _parse_reset(val: Optional[str]) -> Optional[float]:
                if not val:
                    return None
                v = str(val).strip()
                try:
                    if v.endswith("ms"):
                        return float(v[:-2]) / 1000.0
                    if v.endswith("s"):
                        return float(v[:-1])
                    return float(v)
                except Exception:
                    return None

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
                    status = None
                    resp_obj = getattr(e, "response", None)
                    if isinstance(e, httpx.HTTPStatusError) or (resp_obj is not None):
                        try:
                            status = int(getattr(resp_obj, "status_code", None) or 0)
                        except Exception:
                            status = None
                    if status == 429:
                        retry_after = None
                        try:
                            hdr = getattr(resp_obj, "headers", {}) or {}
                            ra = hdr.get("retry-after")
                            if ra is not None:
                                try:
                                    retry_after = float(ra)
                                except Exception:
                                    retry_after = None
                            if retry_after is None:
                                reset_req = _parse_reset(hdr.get("x-ratelimit-reset-requests"))
                                reset_tok = _parse_reset(hdr.get("x-ratelimit-reset-tokens"))
                                reset_suggest = None
                                if reset_req is not None and reset_req > 0:
                                    reset_suggest = reset_req
                                if reset_tok is not None and (reset_suggest is None or reset_tok > reset_suggest):
                                    reset_suggest = reset_tok
                                if reset_suggest is not None:
                                    computed = min(60.0, base_backoff * (2 ** (attempt - 1)))
                                    retry_after = max(reset_suggest, computed)
                        except Exception:
                            retry_after = None
                        if retry_after is not None:
                            wait = max(retry_after, self._retry_min_backoff, min(self._retry_backoff_cap, base_backoff * (2 ** (attempt - 1))))
                        else:
                            wait = max(self._retry_min_backoff, min(self._retry_backoff_cap, base_backoff * (2 ** (attempt - 1)))) + random.random() * 0.5
                        if attempt < max_attempts:
                            await report_status(f"Rate limit, waiting {wait:.0f}s, retry {attempt}/{max_attempts}: {self.model}")
                            logger.warning("OpenAI rate limited (429). retrying in %.1f sec (attempt %d/%d)", wait, attempt, max_attempts)
                            if cancellation_token and cancellation_token.is_cancelled:
                                raise asyncio.CancelledError("Request cancelled by user during rate limit backoff")
                            await self._notify_retry("openai", self.model, self._base_url, False, "Rate limit (429)", attempt - 1, max_attempts)
                            await self._cancellable_sleep(wait, cancellation_token)
                            continue
                        # Retries exhausted - raise for fallback
                        await report_status(f"Rate limit exceeded after {max_attempts} attempts: {self.model}")
                        error_text = str(e)
                        if "quota" in error_text.lower() or "exhausted" in error_text.lower():
                            raise LLMQuotaExhaustedError(
                                f"Quota exhausted: {error_text}",
                                provider="openai", model=self.model, retry_after=wait
                            )
                        raise LLMRateLimitError(
                            f"Rate limit exceeded: {error_text}",
                            provider="openai", model=self.model, retry_after=wait
                        )
                    # Handle server errors (5xx) - retry with exponential backoff
                    if status is not None and status >= 500 and attempt < max_attempts:
                        wait = max(self._retry_min_backoff, min(self._retry_backoff_cap, base_backoff * (2 ** (attempt - 1))))
                        await report_status(f"Server error ({status}), retry {attempt}/{max_attempts} in {wait:.0f}s: {self.model}")
                        logger.warning("OpenAI server error (%d). retrying in %.1f sec (attempt %d/%d)", status, wait, attempt, max_attempts)
                        if cancellation_token and cancellation_token.is_cancelled:
                            raise asyncio.CancelledError("Request cancelled by user during server error backoff")
                        await self._notify_retry("openai", self.model, self._base_url, False, f"Server error ({status})", attempt - 1, max_attempts)
                        await self._cancellable_sleep(wait, cancellation_token)
                        continue
                    if status == 400:
                        error_text = str(e)
                        if "context_length_exceeded" in error_text or "Input tokens exceed" in error_text:
                            from agent_system.context.exceptions import ContextLengthExceededError
                            raise ContextLengthExceededError(
                                message=f"OpenAI context length exceeded: {error_text}",
                                original_exception=e
                            )
                    raise
            
            # Check if resp is None after retry loop (e.g., all attempts failed with rate limiting)
            if resp is None:
                raise Exception(f"OpenAI API request failed after {max_attempts} attempts (rate limiting or other errors)")
            
            choice = resp.choices[0] if resp.choices else None
            if not choice:
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

            result = {"assistant": out}
            logger.debug("OpenAI response has usage attr: %s", hasattr(resp, 'usage'))
            if hasattr(resp, 'usage'):
                logger.debug("OpenAI response usage value: %s", resp.usage)
                if resp.usage:
                    usage_data = {
                        "prompt_tokens": resp.usage.prompt_tokens,
                        "completion_tokens": resp.usage.completion_tokens,
                        "total_tokens": resp.usage.total_tokens
                    }
                    # Extract prompt_tokens_details (cached_tokens) if available
                    if hasattr(resp.usage, 'prompt_tokens_details') and resp.usage.prompt_tokens_details:
                        ptd = resp.usage.prompt_tokens_details
                        usage_data["prompt_tokens_details"] = {
                            "cached_tokens": getattr(ptd, 'cached_tokens', 0) or 0
                        }
                    # Extract completion_tokens_details if available
                    if hasattr(resp.usage, 'completion_tokens_details') and resp.usage.completion_tokens_details:
                        ctd = resp.usage.completion_tokens_details
                        usage_data["completion_tokens_details"] = {
                            "reasoning_tokens": getattr(ctd, 'reasoning_tokens', 0) or 0
                        }
                    result["usage"] = usage_data
                    logger.debug("Added usage to result: %s", result["usage"])
                else:
                    logger.debug("OpenAI response usage is None")
            else:
                logger.debug("OpenAI response has no usage attribute")

            # Notify post-response hook
            _duration_ms = (_time.time() - _request_start) * 1000
            _finish_reason = None
            if choice:
                _finish_reason = getattr(choice, "finish_reason", None)
            await self._notify_post_response({
                "provider": "openai", "model": self.model,
                "url": self._base_url, "is_streaming": False,
                "duration_ms": _duration_ms,
                "usage": result.get("usage"),
                "finish_reason": _finish_reason,
                "timestamp_ms": _time.time() * 1000,
            })

            return result
        except (LLMRateLimitError, LLMQuotaExhaustedError):
            # Typed errors are raised intentionally for the agent server's
            # LLM-fallback mechanism. Downgrading them to an error dict here
            # would route them through the generic upstream-error branch and
            # lose the rate-limit / retry_after / persistent-fallback semantics.
            raise
        except asyncio.CancelledError:
            raise  # User cancellation must propagate, not become an error dict
        except Exception as e:
            status = None
            resp_obj = getattr(e, "response", None)
            if resp_obj is not None:
                try:
                    status = int(getattr(resp_obj, "status_code", None) or 0)
                except Exception:
                    status = None
            err_payload: dict[str, Any] = {"error": True, "message": str(e), "status": status}
            if resp_obj is not None:
                try:
                    data = getattr(resp_obj, "json", lambda: None)()
                    if data:
                        err_payload["response_json"] = data
                except Exception:
                    pass
            logger.exception("OpenAI chat with tools failed: %s", e)
            return {"assistant": {"role": "assistant", "content": "", "error": err_payload}}

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
            async for event in self._chat_tools_streaming_realtime(messages, tools, cancellation_token):
                yield event
        else:
            async for event in self._chat_tools_streaming_chat_completions(messages, tools, cancellation_token, status_scope,
                                                                           response_format=response_format):
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

        # Retry logic for stream interruptions
        max_retries = 3
        retry_backoff = 1.0

        _request_start = _time.time()
        await self._notify_pre_request({
            "provider": "openai", "model": self.model,
            "url": self._base_url, "is_streaming": True,
            "timestamp_ms": _request_start * 1000,
        })

        for attempt in range(max_retries + 1):
            # CancelledError is what the agent server reports as a cancel; a
            # plain Exception would reach it as an upstream error (fallback).
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")

            try:
                opts = {"model": self.model, "messages": msgs, "stream": True, "stream_options": {"include_usage": True}}
                
                if tools:
                    opts["tools"] = tools
                    opts["tool_choice"] = "auto"
                opts.update(self._default_extra)
                if response_format is not None:
                    opts["response_format"] = chat_completions_response_format(response_format)
                
                # Add max_tokens if configured
                if self.max_tokens:
                    opts["max_tokens"] = self.max_tokens

                # Accumulated state
                accumulated_content = []
                accumulated_reasoning = []  # the model's thinking, if it sends any
                accumulated_tool_calls = {}
                accumulated_usage = None  # usage information from final chunk

                client_any = cast(Any, self._client)

                # OpenAI SDK's create() is async and returns AsyncStream when awaited
                stream = await client_any.chat.completions.create(**opts)

                # Process stream chunks with timeout per chunk from config
                chunk_timeout = self._timeout if isinstance(self._timeout, (int, float)) else (self._timeout.read if hasattr(self._timeout, 'read') else 60.0)
                stream_iter = stream.__aiter__()
                
                while True:
                    if cancellation_token and cancellation_token.is_cancelled:
                        raise asyncio.CancelledError("Request cancelled by user")

                    try:
                        chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=chunk_timeout)
                    except StopAsyncIteration:
                        break  # Stream completed normally
                    except asyncio.TimeoutError:
                        logger.warning(f"Stream chunk timeout after {chunk_timeout}s (attempt {attempt + 1}/{max_retries + 1})")
                        raise httpx.RemoteProtocolError(f"Stream stalled - no data for {chunk_timeout}s")

                    # Extract usage if available (appears in final chunk when stream_options={'include_usage': True})
                    if hasattr(chunk, 'usage') and chunk.usage:
                        accumulated_usage = {
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                            "total_tokens": chunk.usage.total_tokens
                        }
                        # Extract prompt_tokens_details (cached_tokens) if available
                        if hasattr(chunk.usage, 'prompt_tokens_details') and chunk.usage.prompt_tokens_details:
                            ptd = chunk.usage.prompt_tokens_details
                            accumulated_usage["prompt_tokens_details"] = {
                                "cached_tokens": getattr(ptd, 'cached_tokens', 0) or 0
                            }
                        # Extract completion_tokens_details if available
                        if hasattr(chunk.usage, 'completion_tokens_details') and chunk.usage.completion_tokens_details:
                            ctd = chunk.usage.completion_tokens_details
                            accumulated_usage["completion_tokens_details"] = {
                                "reasoning_tokens": getattr(ctd, 'reasoning_tokens', 0) or 0
                            }

                    choices = chunk.choices if hasattr(chunk, 'choices') else []
                    if not choices:
                        continue

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
                        yield {
                            "type": "thinking_delta",
                            "delta": _reasoning_delta
                        }

                    # Handle content delta
                    if hasattr(delta, 'content') and delta.content:
                        accumulated_content.append(delta.content)
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

                # Build final result with usage
                final_result = {"assistant": assistant}
                if accumulated_usage:
                    final_result["usage"] = accumulated_usage

                # Notify post-response hook
                _duration_ms = (_time.time() - _request_start) * 1000
                await self._notify_post_response({
                    "provider": "openai", "model": self.model,
                    "url": self._base_url, "is_streaming": True,
                    "duration_ms": _duration_ms,
                    "usage": accumulated_usage,
                    "timestamp_ms": _time.time() * 1000,
                })

                yield {"type": "final", **final_result}
                return  # Success - exit retry loop

            except (httpx.RemoteProtocolError, httpx.NetworkError, httpx.ConnectError) as e:
                if attempt < max_retries:
                    backoff_time = retry_backoff * (2 ** attempt)
                    await report_status(f"Stream interrupted, retry {attempt + 1}/{max_retries} in {backoff_time:.0f}s: {self.model}")
                    logger.warning(f"OpenAI stream interrupted (attempt {attempt + 1}/{max_retries + 1}), retrying in {backoff_time}s: {e}")
                    await self._notify_retry("openai", self.model, self._base_url, True, f"Stream interrupted: {e}", attempt, max_retries + 1)
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                    # Reset accumulated state for retry
                    accumulated_content = []
                    accumulated_tool_calls = {}
                    accumulated_usage = None
                    continue
                else:
                    await report_status(f"Stream failed after {max_retries + 1} attempts: {self.model}")
                    logger.error(f"OpenAI streaming failed after {max_retries + 1} attempts: {e}")
                    message = f"Stream failed after {max_retries + 1} attempts: {e}"
                    await self._notify_post_response({
                        "provider": "openai", "model": self.model, "url": self._base_url, "is_streaming": True,
                        "duration_ms": (_time.time() - _request_start) * 1000,
                        "error": message, "timestamp_ms": _time.time() * 1000,
                    })
                    # retried: the agent must not ask a whole retry cycle again.
                    yield {"type": "final", "assistant": {"role": "assistant", "content": "", "error": {
                        "message": message, "retried": True}}}
                    return

            except Exception as e:
                logger.exception("OpenAI streaming failed: %s", e)
                await self._notify_post_response({
                    "provider": "openai", "model": self.model, "url": self._base_url, "is_streaming": True,
                    "duration_ms": (_time.time() - _request_start) * 1000,
                    "error": str(e), "timestamp_ms": _time.time() * 1000,
                })
                # The shape the agent server reads (message/type).
                yield {"type": "final", "assistant": {"role": "assistant", "content": "",
                                                      "error": {"error": True, "type": "openai_api_error", "message": str(e)}}}
                return

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

        async def notify_done(**info) -> None:
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
            logger.exception("Realtime API call failed: %s", e)
            await notify_done(error=str(e))
            yield failed(str(e))

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
