from __future__ import annotations

from typing import Optional, Any, List, Dict, cast
import json
from ..utils.id import short_id
import asyncio
import random

import httpx

from pydantic import BaseModel
import logging


class ChatMessage(BaseModel):
    role: str
    content: Optional[str] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    function_call: Optional[Dict[str, Any]] = None


class LLMClient:
    async def chat(self, messages: list[ChatMessage]) -> str:
        raise NotImplementedError
    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict]) -> dict:
        raise NotImplementedError


class OpenAIAsyncClient(LLMClient):
    """Async client using OpenAI SDK.

    Can talk to:
      - OpenAI (default base)
      - OpenAI-compatible servers (e.g., Ollama) via base_url="http://host:port/v1"
    """

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None, default_extra: Optional[dict] = None, timeout: Optional[float] = None, *, max_attempts: int = 5, base_backoff: float = 2.0, min_backoff: float = 2.0, backoff_cap: float = 300.0, verify: Optional[bool] = None) -> None:
        try:
            from openai import AsyncOpenAI  # type: ignore
        except Exception as e:
            raise RuntimeError("openai package required for OpenAIAsyncClient") from e
        self._AsyncOpenAI = AsyncOpenAI
        kwargs: dict = {"api_key": api_key}
        # Allow SDK base_url/timeout as before
        if base_url:
            kwargs["base_url"] = base_url
        if timeout is not None:
            kwargs["timeout"] = timeout

        # If a custom 'verify' flag is provided, create an httpx.AsyncClient with that verify
        # and try to pass it into the OpenAI SDK if it accepts a custom httpx client.
        httpx_client = None
        if verify is not None:
            try:
                import httpx as _httpx
                httpx_client = _httpx.AsyncClient(verify=verify, timeout=timeout)
            except Exception as e:
                # Log failure to create custom httpx client
                logger = logging.getLogger(__name__)
                logger.warning("Failed to create custom httpx client, will use SDK default: %s", e)
                httpx_client = None

        # Determine whether AsyncOpenAI accepts a custom httpx client parameter
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
                    # SDK doesn't accept a custom client - fall back to default construction
                    self._client = AsyncOpenAI(**kwargs)
            except Exception:
                # If anything goes wrong inspecting/constructing, fall back to default SDK client
                self._client = AsyncOpenAI(**kwargs)
        else:
            self._client = AsyncOpenAI(**kwargs)
        # Retry/backoff configuration
        self._retry_max_attempts = int(max_attempts)
        self._retry_base_backoff = float(base_backoff)
        self._retry_min_backoff = float(min_backoff)
        self._retry_backoff_cap = float(backoff_cap)
        self.model = model
        self._default_extra = default_extra or {}
        self._timeout = timeout

    async def chat(self, messages: list[ChatMessage]) -> str:
        logger = logging.getLogger(__name__)
        try:
            opts = {"model": self.model, "messages": [m.model_dump() for m in messages]}
            opts.update(self._default_extra)
            # Retry loop for transient 429 responses
            max_attempts = self._retry_max_attempts
            base_backoff = self._retry_base_backoff
            resp = None
            def _parse_reset(val: Optional[str]) -> Optional[float]:
                if not val:
                    return None
                v = str(val).strip()
                try:
                    # ms, s, or plain number (seconds)
                    if v.endswith("ms"):
                        return float(v[:-2]) / 1000.0
                    if v.endswith("s"):
                        return float(v[:-1])
                    return float(v)
                except Exception:
                    return None

            for attempt in range(1, max_attempts + 1):
                try:
                    # SDK stubs vary by version; cast the client to Any to avoid
                    # mypy overload/typing noise for dynamic SDK calls.
                    client_any = cast(Any, self._client)
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
                    # If rate limited, honor Retry-After when present and backoff
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
                            # If Retry-After absent, check x-ratelimit-reset-* headers
                            if retry_after is None:
                                reset_req = _parse_reset(hdr.get("x-ratelimit-reset-requests"))
                                reset_tok = _parse_reset(hdr.get("x-ratelimit-reset-tokens"))
                                # prefer the larger reset suggestion
                                reset_suggest = None
                                if reset_req is not None and reset_req > 0:
                                    reset_suggest = reset_req
                                if reset_tok is not None and (reset_suggest is None or reset_tok > reset_suggest):
                                    reset_suggest = reset_tok
                                if reset_suggest is not None:
                                    # Use server suggestion but don't go below our computed backoff
                                    computed = min(60.0, base_backoff * (2 ** (attempt - 1)))
                                    retry_after = max(reset_suggest, computed)
                        except Exception:
                            retry_after = None
                        # Ensure wait respects configured minimum and cap
                        if retry_after is not None:
                            wait = max(retry_after, self._retry_min_backoff, min(self._retry_backoff_cap, base_backoff * (2 ** (attempt - 1))))
                        else:
                            wait = max(self._retry_min_backoff, min(self._retry_backoff_cap, base_backoff * (2 ** (attempt - 1)))) + random.random() * 0.5
                        logger.warning("OpenAI rate limited (429). retrying in %.1f sec (attempt %d/%d)", wait, attempt, max_attempts)
                        await asyncio.sleep(wait)
                        continue
                    # Not a rate-limit we can retry, re-raise to be handled below
                    raise
            # Log lightly to avoid huge dumps
            try:
                logger.debug("OpenAI resp id=%s choices=%d", getattr(resp, "id", None), len(getattr(resp, "choices", []) or []))
            except Exception:
                pass
            choice = resp.choices[0] if resp.choices else None
            if not choice:
                return ""
            message = choice.message
            # Prefer content text
            content = getattr(message, "content", None)
            if content:
                return content
            # If tool_calls are present, synthesize our tool JSON
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
                            # best-effort: attempt relaxed quotes
                            params = json.loads(arguments.replace("'", '"')) if arguments else {}
                    elif isinstance(arguments, dict):
                        params = arguments
                    if name:
                        return json.dumps({"type": "tool", "tool": name, "params": params}, ensure_ascii=False)
                except Exception:
                    pass
            # Last fallback: dump the structure to string
            try:
                return getattr(message, "content", None) or ""
            except Exception:
                return ""
        except Exception as e:
            # Provide richer error details so caller can differentiate real failure vs empty model output
            try:
                status = None
                resp_obj = getattr(e, "response", None)
                if resp_obj is not None:
                    try:
                        status = int(getattr(resp_obj, "status_code", None) or 0)
                    except Exception:
                        status = None
                err_payload: dict[str, Any] = {"error": True, "message": str(e), "status": status}
                # Attempt to extract JSON body if present
                if resp_obj is not None:
                    try:
                        data = getattr(resp_obj, "json", lambda: None)()
                        if data:
                            err_payload["response_json"] = data
                    except Exception:
                        pass
                logger.exception("OpenAI chat failed: %s", e)
                # Encode structured error as JSON string to distinguish from normal empty content
                return json.dumps({"_llm_error": err_payload}, ensure_ascii=False)
            except Exception:
                logger.exception("OpenAI chat failed (secondary error building payload): %s", e)
                return ""

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict]) -> dict:
        """Call model with native tool calling enabled.

        Returns a dict with keys:
          - assistant: the assistant message dict including optional tool_calls
        """
        logger = logging.getLogger(__name__)
        # Convert ChatMessage -> dicts compatible with OpenAI SDK
        msgs: list[dict] = []
        for m in messages:
            d = {"role": m.role}
            if m.content is not None:
                d["content"] = m.content
            if m.role == "tool":
                # For OpenAI tool message: role must be 'tool' and include tool_call_id
                if m.tool_call_id:
                    # ensure string type
                    d["tool_call_id"] = str(m.tool_call_id)
            elif m.role == "assistant" and m.tool_calls:
                # For OpenAI assistant message with tool calls
                d["tool_calls"] = m.tool_calls
            msgs.append(d)

        # Defensive normalization of tool schemas to prevent 400 errors like
        # "Missing required parameter: 'tools[5].type'" when a plugin forgets it.
        normalized_tools: list[dict] = []
        for idx, t in enumerate(tools):
            if not isinstance(t, dict):
                logger.warning("Skipping non-dict tool schema at index %d: %r", idx, t)
                continue
            tool_obj = dict(t)  # shallow copy
            if "type" not in tool_obj:
                tool_obj["type"] = "function"
            # Some legacy schemas might put name/description directly at top-level; wrap if needed
            if tool_obj.get("type") == "function" and "function" not in tool_obj:
                # Attempt to build function envelope
                fn_fields = {k: tool_obj.get(k) for k in ("name", "description", "parameters") if k in tool_obj}
                if fn_fields:
                    # Remove moved keys
                    for k in list(fn_fields.keys()):
                        tool_obj.pop(k, None)
                    tool_obj["function"] = fn_fields
            # Validate minimal required structure
            fn = tool_obj.get("function") if tool_obj.get("type") == "function" else None
            if tool_obj.get("type") == "function" and (not isinstance(fn, dict) or not fn.get("name")):
                logger.warning("Tool schema at index %d missing function.name; skipping: %r", idx, tool_obj)
                continue
            normalized_tools.append(tool_obj)
        if len(normalized_tools) != len(tools):
            logger.debug("Normalized tool schemas: input=%d, output=%d", len(tools), len(normalized_tools))
        tools = normalized_tools
        try:
            opts = {"model": self.model, "messages": msgs, "tools": tools, "tool_choice": "auto"}
            opts.update(self._default_extra)
            # Retry loop for transient 429 responses
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
                try:
                    client_any = cast(Any, self._client)
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
                        await asyncio.sleep(wait)
                        continue
                    # Check for context length exceeded error (400 with specific message)
                    if status == 400:
                        error_text = str(e)
                        if "context_length_exceeded" in error_text or "Input tokens exceed" in error_text:
                            # This is a context length error - raise a special exception that the agent can catch
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
            # Build a transferable dict representation
            out = {"role": "assistant", "content": getattr(message, "content", None)}
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                out_calls = []
                for tc in tool_calls:
                    function = getattr(tc, "function", None)
                    name = getattr(function, "name", None) if function is not None else getattr(tc, "name", None)
                    arguments = getattr(function, "arguments", None) if function is not None else getattr(tc, "arguments", None)
                    # Generate UUID if no ID provided by LLM
                    tc_id = getattr(tc, "id", None) or f"call_{short_id()}"
                    out_calls.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    })
                out["tool_calls"] = out_calls

            # Include usage data if available
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
            # Preserve detailed error info instead of silently returning empty content so agent loop can treat as fatal
            status = None
            resp_obj = getattr(e, "response", None)
            if resp_obj is not None:
                try:
                    status = int(getattr(resp_obj, "status_code", None) or 0)
                except Exception:
                    status = None
            err_payload: dict[str, Any] = {"error": True, "message": str(e), "status": status}
            if resp_obj is not None:
                # Extract any JSON response body
                try:
                    data = getattr(resp_obj, "json", lambda: None)()
                    if data:
                        err_payload["response_json"] = data
                except Exception:
                    pass
            logger.exception("OpenAI chat with tools failed: %s", e)
            return {"assistant": {"role": "assistant", "content": "", "error": err_payload}}


class OllamaNativeAsyncClient(LLMClient):
    """Async client for native Ollama REST API (/api/chat).

    Supports per-request options including num_ctx.
    """

    def __init__(self, model: str, base_url: Optional[str] = None, options: Optional[dict[str, Any]] = None, timeout: Optional[float] = None) -> None:
        import httpx  # lazy import
        self._httpx = httpx
        self._base = (base_url.rstrip("/")) if base_url else "http://127.0.0.1:11434"
        self.model = model
        self._options = options or {}
        self._timeout = timeout or 60.0

    def _map_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            d: dict[str, Any] = {"role": m.role}
            if m.content is not None:
                d["content"] = m.content
            # Ollama native supports tool messages via tool_name and tool_call_id
            if m.role == "tool":
                if m.name:
                    d["tool_name"] = m.name
                if m.tool_call_id:
                    d["tool_call_id"] = m.tool_call_id
            # Handle assistant messages with tool calls for Ollama native
            elif m.role == "assistant" and m.tool_calls:
                d["tool_calls"] = m.tool_calls
            out.append(d)
        return out

    async def chat(self, messages: list[ChatMessage]) -> str:
        url = f"{self._base}/api/chat"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._map_messages(messages),
            "stream": False,
        }
        if self._options:
            body["options"] = self._options
        async with self._httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
        msg = (data or {}).get("message") or {}
        return msg.get("content") or ""

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict]) -> dict:
        url = f"{self._base}/api/chat"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._map_messages(messages),
            "tools": tools,
            "stream": False,
        }
        if self._options:
            body["options"] = self._options
        async with self._httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
        message = (data or {}).get("message") or {}
        out: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        tcs = message.get("tool_calls") or []
        if tcs:
            # Shape tool calls similar to OpenAI's tool_calls
            out_calls = []
            for tc in tcs:
                func = tc.get("function", {})
                # Generate short id if no ID provided by LLM
                tc_id = tc.get("id") or f"call_{short_id()}"
                out_calls.append({
                    "id": tc_id,
                    "function": {
                        "name": func.get("name"),
                        "arguments": func.get("arguments"),
                    },
                })
            out["tool_calls"] = out_calls
        return {"assistant": out}


def make_llm(provider: str, model: str, openai_api_key: Optional[str], ollama_url: Optional[str] = None, context_window: Optional[int] = None, ollama_mode: Optional[str] = None, request_timeout: Optional[int] = None, ssl_verify: Optional[bool] = None) -> LLMClient:
    """Factory creating an async LLM client.

    - provider=openai: use AsyncOpenAI against OpenAI API.
    - provider=ollama: use Ollama (native or openai-compat) depending on mode.
    """
    import os

    if provider == "openai":
        # Prefer an explicit api key passed in, otherwise honor the
        # OPENAI_API_KEY environment variable.
        if not openai_api_key:
            openai_api_key = os.getenv("OPENAI_API_KEY")
        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when provider=openai")

        return OpenAIAsyncClient(
            model=model,
            api_key=openai_api_key,
            timeout=float(request_timeout) if request_timeout else None,
            verify=ssl_verify,
        )

    if provider == "ollama":
        mode = (ollama_mode or "openai_compat").lower()
        if mode == "native":
            base_native = (ollama_url.rstrip("/")) if ollama_url else "http://127.0.0.1:11434"
            options: dict[str, Any] = {}
            if context_window:
                options["num_ctx"] = context_window
            return OllamaNativeAsyncClient(
                model=model,
                base_url=base_native,
                options=options or None,
                timeout=float(request_timeout) if request_timeout else None,
            )
        # else: OpenAI-compatible path using the OpenAI-compatible SDK client
        base = (ollama_url.rstrip("/") + "/v1") if ollama_url else "http://127.0.0.1:11434/v1"
        api_key = "ollama"  # required by SDK, ignored by Ollama
        return OpenAIAsyncClient(
            model=model,
            api_key=api_key,
            base_url=base,
            timeout=float(request_timeout) if request_timeout else None,
            verify=ssl_verify,
        )

    raise ValueError(f"Unknown LLM provider: {provider}")
