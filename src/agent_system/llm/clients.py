from __future__ import annotations

from typing import Optional, Any
import json
import re
import uuid

from pydantic import BaseModel
import logging


class ChatMessage(BaseModel):
    role: str
    content: Optional[str] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list] = None


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

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None, default_extra: Optional[dict] = None, timeout: Optional[float] = None) -> None:
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
        self._client = AsyncOpenAI(**kwargs)
        self.model = model
        self._default_extra = default_extra or {}
        self._timeout = timeout

    async def chat(self, messages: list[ChatMessage]) -> str:
        logger = logging.getLogger(__name__)
        try:
            opts = {"model": self.model, "messages": [m.model_dump() for m in messages]}
            opts.update(self._default_extra)
            resp = await self._client.chat.completions.create(
                **opts,
            )
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
            logger.exception("OpenAI chat failed: %s", e)
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
        try:
            opts = {"model": self.model, "messages": msgs, "tools": tools, "tool_choice": "auto"}
            opts.update(self._default_extra)
            resp = await self._client.chat.completions.create(**opts)
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
                    tc_id = getattr(tc, "id", None) or f"call_{uuid.uuid4().hex[:12]}"
                    out_calls.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    })
                out["tool_calls"] = out_calls
            return {"assistant": out}
        except Exception as e:
            logger.exception("OpenAI chat with tools failed: %s", e)
            return {"assistant": {"role": "assistant", "content": ""}}


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
                # Generate UUID if no ID provided by LLM
                tc_id = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                out_calls.append({
                    "id": tc_id,
                    "function": {
                        "name": func.get("name"),
                        "arguments": func.get("arguments"),
                    },
                })
            out["tool_calls"] = out_calls
        return {"assistant": out}


def make_llm(provider: str, model: str, openai_api_key: Optional[str], ollama_url: Optional[str] = None, context_window: Optional[int] = None, ollama_mode: Optional[str] = None, request_timeout: Optional[int] = None) -> LLMClient:
    """Factory creating an async LLM client.

    - provider=openai: use AsyncOpenAI against OpenAI API.
    - provider=ollama: use AsyncOpenAI against Ollama's OpenAI-compatible endpoint at base_url .../v1.
    """
    if provider == "openai":
        # Prefer an explicit api key passed in, otherwise honor the
        # OPENAI_API_KEY environment variable. Avoid implicitly loading
        # repository config files (e.g. config/agent.yaml) here because
        # that couples runtime initialization to repository state and
        # can break unit tests which expect no LLM to be available.
        import os
        if not openai_api_key:
            openai_api_key = os.getenv("OPENAI_API_KEY")

        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when provider=openai")

        return OpenAIAsyncClient(model=model, api_key=openai_api_key, timeout=float(request_timeout) if request_timeout else None)
    if provider == "ollama":
        mode = (ollama_mode or "openai_compat").lower()
        if mode == "native":
            base_native = (ollama_url.rstrip("/")) if ollama_url else "http://127.0.0.1:11434"
            options: dict[str, Any] = {}
            if context_window:
                options["num_ctx"] = context_window
            return OllamaNativeAsyncClient(model=model, base_url=base_native, options=options or None, timeout=float(request_timeout) if request_timeout else None)
        # else: OpenAI-compatible path
        base = (ollama_url.rstrip("/") + "/v1") if ollama_url else "http://127.0.0.1:11434/v1"
        api_key = "ollama"  # required by SDK, ignored by Ollama
        return OpenAIAsyncClient(model=model, api_key=api_key, base_url=base, timeout=float(request_timeout) if request_timeout else None)
    raise ValueError(f"Unknown LLM provider: {provider}")
