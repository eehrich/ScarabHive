from __future__ import annotations

from typing import Optional, Any, cast
import json
import asyncio
import random
import logging
import httpx

from ..utils.id import short_id
from .models import ChatMessage, LLMClient


class OpenAIAsyncClient(LLMClient):
    """Async client using OpenAI SDK.

    Can talk to:
      - OpenAI (default base)
      - OpenAI-compatible servers (e.g., Ollama) via base_url="http://host:port/v1"
    """

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None, default_extra: Optional[dict] = None, timeout: Optional[float] = None, *, max_attempts: int = 5, base_backoff: float = 2.0, min_backoff: float = 2.0, backoff_cap: float = 300.0, verify: Optional[bool] = None, context_window: Optional[int] = None) -> None:
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
                import ssl as _ssl
                # If verify explicitly False, create an SSLContext that disables
                # certificate verification to ensure behavior across backends
                verify_arg = verify
                if verify is False:
                    try:
                        ctx = _ssl.create_default_context()
                        ctx.check_hostname = False
                        ctx.verify_mode = _ssl.CERT_NONE
                        verify_arg = ctx
                    except Exception:
                        verify_arg = False

                httpx_client = _httpx.AsyncClient(verify=verify_arg, timeout=timeout)
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
        self.provider = "openai"
        self.context_window = context_window
        self._default_extra = default_extra or {}
        self._timeout = timeout

    async def _execute_with_cancellation(self, llm_task: asyncio.Task, cancellation_token):
        """Execute LLM task with efficient event-based cancellation monitoring.

        Instead of polling with timeouts (which throws exceptions every 0.5s),
        uses asyncio.wait() to efficiently wait for either completion or cancellation.

        Returns:
            The result of llm_task when completed

        Raises:
            Exception: When cancelled by user
        """
        cancel_event = asyncio.Event()

        async def check_cancellation():
            """Background task that monitors cancellation without polling exceptions"""
            while not llm_task.done():
                if cancellation_token.is_cancelled:
                    cancel_event.set()
                    break
                await asyncio.sleep(0.1)  # Check every 100ms, doesn't block main task

        cancel_task = asyncio.create_task(check_cancellation())

        # Wait for either LLM completion or cancellation (efficient, no exceptions!)
        done, pending = await asyncio.wait(
            {llm_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED
        )

        if cancel_event.is_set():
            # Cancellation requested - clean up LLM task
            llm_task.cancel()
            try:
                await llm_task
            except asyncio.CancelledError:
                pass
            finally:
                cancel_task.cancel()
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass
            raise Exception("Request cancelled by user during LLM call")

        # LLM completed - clean up cancel task
        cancel_task.cancel()
        try:
            await cancel_task
        except asyncio.CancelledError:
            pass

        return await llm_task

    async def chat(self, messages: list[ChatMessage], cancellation_token=None) -> str:
        logger = logging.getLogger(__name__)
        try:
            opts = {"model": self.model, "messages": [m.model_dump() for m in messages]}
            opts.update(self._default_extra)
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
                        resp = await self._execute_with_cancellation(llm_task, cancellation_token)
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
                        logger.warning("OpenAI rate limited (429). retrying in %.1f sec (attempt %d/%d)", wait, attempt, max_attempts)
                        if cancellation_token and cancellation_token.is_cancelled:
                            raise Exception("Request cancelled by user during rate limit backoff")
                        await asyncio.sleep(wait)
                        continue
                    if status == 400:
                        error_text = str(e)
                        if "context_length_exceeded" in error_text or "Input tokens exceed" in error_text:
                            from ..context.exceptions import ContextLengthExceededError
                            raise ContextLengthExceededError(
                                message=f"OpenAI context length exceeded: {error_text}",
                                original_exception=e
                            )
                    raise
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
                        except Exception as e:
                            logger.debug(f"Failed to parse tool call arguments as JSON, trying quote replacement: {e}")
                            params = json.loads(arguments.replace("'", '"')) if arguments else {}
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
                err_payload: dict[str, Any] = {"error": True, "message": str(e), "status": status}
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

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None) -> dict:
        logger = logging.getLogger(__name__)
        msgs: list[dict] = []
        for m in messages:
            # Use model_dump() to properly serialize nested Pydantic models
            d = m.model_dump(exclude_none=True)
            msgs.append(d)

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
                        resp = await self._execute_with_cancellation(llm_task, cancellation_token)
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
                        logger.warning("OpenAI rate limited (429). retrying in %.1f sec (attempt %d/%d)", wait, attempt, max_attempts)
                        if cancellation_token and cancellation_token.is_cancelled:
                            raise Exception("Request cancelled by user during rate limit backoff")
                        await asyncio.sleep(wait)
                        continue
                    if status == 400:
                        error_text = str(e)
                        if "context_length_exceeded" in error_text or "Input tokens exceed" in error_text:
                            from ..context.exceptions import ContextLengthExceededError
                            raise ContextLengthExceededError(
                                message=f"OpenAI context length exceeded: {error_text}",
                                original_exception=e
                            )
                    raise
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
                    result["usage"] = {
                        "prompt_tokens": resp.usage.prompt_tokens,
                        "completion_tokens": resp.usage.completion_tokens,
                        "total_tokens": resp.usage.total_tokens
                    }
                    logger.debug("Added usage to result: %s", result["usage"])
                else:
                    logger.debug("OpenAI response usage is None")
            else:
                logger.debug("OpenAI response has no usage attribute")

            return result
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
