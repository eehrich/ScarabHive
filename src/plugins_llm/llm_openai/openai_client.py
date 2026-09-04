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
from agent_system.llm.models import ChatMessage, LLMClient, LLMRateLimitError, LLMQuotaExhaustedError
from agent_system.config.models import ModelCapabilitiesConfig
from agent_system.llm.retry_utils import execute_with_cancellation
from plugins_llm.llm_common import openai_utils


class OpenAIAsyncClient(LLMClient):
    """Async client using OpenAI SDK.

    Can talk to:
      - OpenAI (default base)
      - OpenAI-compatible servers (e.g., Ollama) via base_url="http://host:port/v1"
    """

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

    def _create_multimodal_injection(self, tool_msg: ChatMessage) -> Optional[dict]:
        """Create injected user message for multimodal tool content.
        
        Delegates to the central utility function in multimodal_tool_content.py.
        """
        from agent_system.utils.multimodal_tool_content import create_multimodal_injection, check_vision_support
        
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

    async def chat(self, messages: list[ChatMessage], cancellation_token=None) -> str:
        logger = logging.getLogger(__name__)
        try:
            # NOTE: model_dump() is CPU-intensive for large messages, run in thread pool
            def _serialize() -> list:
                result = []
                for m in messages:
                    d = m.model_dump(exclude_none=True, mode='json')
                    d.pop('injected_by', None)  # Internal hook metadata
                    d.pop('rd_orphaned', None)  # Internal reasoning-invalidation marker (utils/reasoning_artifacts.py)
                    # Normalize content for OpenAI API
                    if isinstance(d.get('content'), list):
                        d['content'] = openai_utils.normalize_content_list(d['content'])
                        if not d['content']:
                            d['content'] = ""
                    result.append(d)
                return result
            serialized = await asyncio.to_thread(_serialize)
            
            opts = {"model": self.model, "messages": serialized}
            opts.update(self._default_extra)
            
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
                    raise Exception("Request cancelled by user")
                try:
                    client_any = cast(Any, self._client)
                    if cancellation_token:
                        llm_task = asyncio.create_task(client_any.chat.completions.create(**opts))
                        resp = await execute_with_cancellation(llm_task, cancellation_token)
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
                                raise Exception("Request cancelled by user during rate limit backoff")
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
                            raise Exception("Request cancelled by user during server error backoff")
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

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None) -> dict:
        """Dispatch to appropriate API based on model capabilities.

        Routes to either Chat Completions API or Realtime API based on
        the model's default_api_type capability.
        """
        api_type = self.get_api_type()

        if api_type == 'realtime':
            return await self._chat_tools_realtime(messages, tools, cancellation_token)
        else:
            return await self._chat_tools_chat_completions(messages, tools, cancellation_token, status_scope)

    async def _chat_tools_chat_completions(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None) -> dict:
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
                d.pop('rd_orphaned', None)  # Internal reasoning-invalidation marker (utils/reasoning_artifacts.py)

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
            return result
        
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
                    raise Exception("Request cancelled by user")
                try:
                    client_any = cast(Any, self._client)
                    if cancellation_token:
                        llm_task = asyncio.create_task(client_any.chat.completions.create(**opts))
                        resp = await execute_with_cancellation(llm_task, cancellation_token)
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
                                raise Exception("Request cancelled by user during rate limit backoff")
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
                            raise Exception("Request cancelled by user during server error backoff")
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
            # Cancellation is also raised above as a plain Exception sentinel
            # ("Request cancelled by user"). Propagate it cleanly instead of
            # reporting it as an upstream LLM error (which makes the server
            # retry the cancelled request through every fallback profile).
            if "cancelled by user" in str(e).lower():
                raise
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
        """Realtime API implementation (non-streaming).

        Connects to Realtime API via WebSocket, sends conversation items,
        and collects all response events until completion.
        """
        logger = logging.getLogger(__name__)

        try:
            from .realtime_session import RealtimeSession
            from .realtime_adapter import RealtimeMessageAdapter
        except ImportError as e:
            logger.error("Failed to import Realtime API modules: %s", e)
            return {"assistant": {"role": "assistant", "content": "", "error": {"error": "Realtime API modules not available"}}}

        try:
            # Create and connect session
            async with RealtimeSession(self.model, self.api_key) as session:
                # Check for cancellation
                if cancellation_token and cancellation_token.is_cancelled:
                    raise Exception("Request cancelled by user")

                # Extract system instructions
                instructions = RealtimeMessageAdapter.extract_system_instructions(messages)

                # Configure session
                session_config: dict[str, Any] = {
                    "modalities": ["text"],  # Text-only mode
                }
                if instructions:
                    session_config["instructions"] = instructions
                if tools:
                    realtime_tools = RealtimeMessageAdapter.tools_to_realtime_tools(tools)
                    if realtime_tools:
                        session_config["tools"] = realtime_tools
                        session_config["tool_choice"] = "auto"

                # Apply default_extra config (temperature, etc.)
                session_config.update(self._default_extra)

                await session.configure_session(session_config)

                # Check for cancellation
                if cancellation_token and cancellation_token.is_cancelled:
                    raise Exception("Request cancelled by user")

                # Convert messages to conversation items
                items = RealtimeMessageAdapter.messages_to_conversation_items(messages)

                # Add conversation items
                for item in items:
                    await session.add_conversation_item(item)

                    # Check for cancellation
                    if cancellation_token and cancellation_token.is_cancelled:
                        raise Exception("Request cancelled by user")

                # Request response
                await session.create_response()

                # Collect all response events
                response_events = []
                async for event in session.receive_events():
                    # Check for cancellation
                    if cancellation_token and cancellation_token.is_cancelled:
                        raise Exception("Request cancelled by user")

                    event_type = event.get("type")
                    response_events.append(event)

                    # Check for errors
                    if event_type == "error":
                        error_data = event.get("error", {})
                        logger.error(f"Realtime API error: {error_data}")
                        return {
                            "assistant": {
                                "role": "assistant",
                                "content": "",
                                "error": {
                                    "error": f"{error_data.get('type', 'unknown')}: {error_data.get('message', 'Unknown error')}"
                                }
                            }
                        }

                    # Stop when response is done
                    if event_type == "response.done":
                        break

                # Parse response content from events
                content, tool_calls = RealtimeMessageAdapter.parse_response_content(response_events)

                # Build assistant message
                assistant_msg = RealtimeMessageAdapter.build_assistant_message(content, tool_calls)

                return {"assistant": assistant_msg}

        except Exception as e:
            error_str = str(e).lower()
            if "cancelled" in error_str:
                logger.info(f"Realtime API request cancelled: {e}")
                raise

            logger.exception("Realtime API call failed: %s", e)
            return {
                "assistant": {
                    "role": "assistant",
                    "content": "",
                    "error": {
                        "error": f"realtime_api_error: {str(e)}"
                    }
                }
            }

    async def chat_tools_streaming(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None):
        """Dispatch streaming to appropriate API based on model capabilities."""
        api_type = self.get_api_type()

        if api_type == 'realtime':
            async for event in self._chat_tools_streaming_realtime(messages, tools, cancellation_token):
                yield event
        else:
            async for event in self._chat_tools_streaming_chat_completions(messages, tools, cancellation_token, status_scope):
                yield event

    async def _chat_tools_streaming_chat_completions(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None):
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
                # Remove internal metadata from serialized dict
                d.pop('multimodal_content', None)
                d.pop('injected_by', None)
                d.pop('rd_orphaned', None)  # Internal reasoning-invalidation marker (utils/reasoning_artifacts.py)
                
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
            return result
        
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
            if cancellation_token and cancellation_token.is_cancelled:
                raise Exception("Request cancelled by user")

            try:
                opts = {"model": self.model, "messages": msgs, "stream": True, "stream_options": {"include_usage": True}}
                
                if tools:
                    opts["tools"] = tools
                    opts["tool_choice"] = "auto"
                opts.update(self._default_extra)
                
                # Add max_tokens if configured
                if self.max_tokens:
                    opts["max_tokens"] = self.max_tokens

                # Accumulated state
                accumulated_content = []
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
                        raise Exception("Request cancelled by user")
                    
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

                    # Handle reasoning/thinking delta (Gemini thinking tokens)
                    if hasattr(delta, 'reasoning') and delta.reasoning:
                        yield {
                            "type": "thinking_delta",
                            "delta": delta.reasoning
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
                    yield {"type": "final", "assistant": {"role": "assistant", "content": "", "error": {"message": f"Stream failed after {max_retries + 1} attempts: {e}"}}}
                    return

            except Exception as e:
                logger.exception("OpenAI streaming failed: %s", e)
                yield {"type": "final", "assistant": {"role": "assistant", "content": "", "error": {"error": str(e)}}}
                return

    async def _chat_tools_streaming_realtime(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None):
        """Realtime API streaming implementation.

        Connects to Realtime API and yields events in real-time as they arrive.
        """
        logger = logging.getLogger(__name__)

        try:
            from .realtime_session import RealtimeSession
            from .realtime_adapter import RealtimeMessageAdapter
        except ImportError as e:
            logger.error("Failed to import Realtime API modules: %s", e)
            yield {"type": "final", "assistant": {"role": "assistant", "content": "", "error": {"error": "Realtime API modules not available"}}}
            return

        try:
            # Create and connect session
            async with RealtimeSession(self.model, self.api_key) as session:
                # Check for cancellation
                if cancellation_token and cancellation_token.is_cancelled:
                    raise Exception("Request cancelled by user")

                # Extract system instructions
                instructions = RealtimeMessageAdapter.extract_system_instructions(messages)

                # Configure session
                session_config: dict[str, Any] = {
                    "modalities": ["text"],  # Text-only mode
                }
                if instructions:
                    session_config["instructions"] = instructions

                # Convert and configure tools
                if tools:
                    realtime_tools = RealtimeMessageAdapter.tools_to_realtime_tools(tools)
                    if realtime_tools:
                        session_config["tools"] = realtime_tools
                        session_config["tool_choice"] = "auto"

                # Apply default_extra config
                session_config.update(self._default_extra)

                # Configure session
                await session.configure_session(session_config)
                # Check for cancellation
                if cancellation_token and cancellation_token.is_cancelled:
                    raise Exception("Request cancelled by user")

                # Convert messages to conversation items
                items = RealtimeMessageAdapter.messages_to_conversation_items(messages)

                # Add conversation items
                for item in items:
                    await session.add_conversation_item(item)
                    if cancellation_token and cancellation_token.is_cancelled:
                        raise Exception("Request cancelled by user")

                # Request response
                await session.create_response()

                # Stream events in real-time
                accumulated_content = []
                accumulated_tool_calls = {}

                async for event in session.receive_events():
                    # Check for cancellation
                    if cancellation_token and cancellation_token.is_cancelled:
                        raise Exception("Request cancelled by user")

                    event_type = event.get("type")

                    # Yield text deltas
                    if event_type == "response.text.delta":
                        delta = event.get("delta", "")
                        accumulated_content.append(delta)
                        yield {
                            "type": "content_delta",
                            "delta": delta,
                            "accumulated": "".join(accumulated_content)
                        }

                    # Handle tool call deltas
                    elif event_type == "response.function_call_arguments.delta":
                        call_id = event.get("call_id")
                        if call_id not in accumulated_tool_calls:
                            accumulated_tool_calls[call_id] = {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": event.get("name"),
                                    "arguments": ""
                                }
                            }
                        accumulated_tool_calls[call_id]["function"]["arguments"] += event.get("delta", "")

                        yield {
                            "type": "tool_call_delta",
                            "index": len(accumulated_tool_calls) - 1,
                            "accumulated": accumulated_tool_calls[call_id]
                        }

                    # Check for errors
                    elif event_type == "error":
                        error_data = event.get("error", {})
                        logger.error(f"Realtime API error: {error_data}")
                        yield {
                            "type": "final",
                            "assistant": {
                                "role": "assistant",
                                "content": "",
                                "error": {
                                    "error": f"{error_data.get('type', 'unknown')}: {error_data.get('message', 'Unknown error')}"
                                }
                            }
                        }
                        return

                    # Response complete
                    elif event_type == "response.done":
                        # Build final assistant message
                        content = "".join(accumulated_content) if accumulated_content else None
                        tool_calls = [accumulated_tool_calls[cid] for cid in sorted(accumulated_tool_calls.keys())] if accumulated_tool_calls else None

                        assistant_msg = RealtimeMessageAdapter.build_assistant_message(content, tool_calls)

                        yield {"type": "final", "assistant": assistant_msg}
                        return

        except Exception as e:
            error_str = str(e).lower()
            if "cancelled" in error_str:
                logger.info(f"Realtime API streaming cancelled: {e}")
                raise

            logger.exception("Realtime API streaming failed: %s", e)
            yield {
                "type": "final",
                "assistant": {
                    "role": "assistant",
                    "content": "",
                    "error": {
                        "error": f"realtime_api_error: {str(e)}"
                    }
                }
            }

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
