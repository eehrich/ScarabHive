from __future__ import annotations

from typing import Optional, Any
import asyncio
from ..utils.id import short_id

from .models import ChatMessage, LLMClient


class OllamaNativeAsyncClient(LLMClient):
    """Async client for native Ollama REST API (/api/chat).

    Supports per-request options including num_ctx.
    """

    def __init__(self, model: str, base_url: Optional[str] = None, options: Optional[dict[str, Any]] = None, timeout: Optional[float] = None, verify: Optional[bool] = None, context_window: Optional[int] = None) -> None:
        import httpx  # lazy import
        self._httpx = httpx
        self._base = (base_url.rstrip("/")) if base_url else "http://127.0.0.1:11434"
        self.model = model
        self.provider = "ollama"
        self.context_window = context_window
        self._options = options or {}
        self._timeout = timeout or 60.0

        # Store verify parameter and create SSLContext if needed
        self._verify = verify if verify is not None else True
        self._verify_arg = self._verify
        if self._verify is False:
            try:
                import ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                self._verify_arg = ctx
            except Exception:
                # Fallback to False if SSLContext creation fails
                self._verify_arg = False

    async def _execute_with_cancellation(self, http_task: asyncio.Task, cancellation_token):
        """Execute HTTP task with efficient event-based cancellation monitoring.

        Instead of polling with timeouts (which throws exceptions every 0.5s),
        uses asyncio.wait() to efficiently wait for either completion or cancellation.

        Returns:
            The result of http_task when completed

        Raises:
            Exception: When cancelled by user
        """
        cancel_event = asyncio.Event()

        async def check_cancellation():
            """Background task that monitors cancellation without polling exceptions"""
            while not http_task.done():
                if cancellation_token.is_cancelled:
                    cancel_event.set()
                    break
                await asyncio.sleep(0.1)  # Check every 100ms, doesn't block main task

        cancel_task = asyncio.create_task(check_cancellation())

        # Wait for either HTTP completion or cancellation (efficient, no exceptions!)
        done, pending = await asyncio.wait(
            {http_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED
        )

        if cancel_event.is_set():
            # Cancellation requested - clean up HTTP task
            http_task.cancel()
            try:
                await http_task
            except asyncio.CancelledError:
                pass
            finally:
                cancel_task.cancel()
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass
            raise Exception("Request cancelled by user during Ollama call")

        # HTTP completed - clean up cancel task
        cancel_task.cancel()
        try:
            await cancel_task
        except asyncio.CancelledError:
            pass

        return await http_task

    def _map_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        import json
        out: list[dict[str, Any]] = []
        for m in messages:
            # Use model_dump() to properly serialize nested Pydantic models
            d = m.model_dump(exclude_none=True)

            # Ollama expects tool_calls.function.arguments to be an object, not a string
            # Convert string arguments to dict if needed
            if "tool_calls" in d and d["tool_calls"]:
                for tc in d["tool_calls"]:
                    if "function" in tc and "arguments" in tc["function"]:
                        args = tc["function"]["arguments"]
                        if isinstance(args, str):
                            try:
                                # Parse JSON string to dict
                                tc["function"]["arguments"] = json.loads(args)
                            except (json.JSONDecodeError, TypeError):
                                # If parsing fails, leave as-is or use empty dict
                                tc["function"]["arguments"] = {}

            out.append(d)
        return out

    async def chat(self, messages: list[ChatMessage], cancellation_token=None) -> str:
        url = f"{self._base}/api/chat"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._map_messages(messages),
            "stream": False,
        }
        if self._options:
            body["options"] = self._options

        if cancellation_token and cancellation_token.is_cancelled:
            raise Exception("Request cancelled by user")

        async with self._httpx.AsyncClient(timeout=self._timeout, verify=self._verify_arg) as client:
            if cancellation_token:
                http_task = asyncio.create_task(client.post(url, json=body))
                resp = await self._execute_with_cancellation(http_task, cancellation_token)
            else:
                resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
        msg = (data or {}).get("message") or {}
        return msg.get("content") or ""

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None) -> dict:
        url = f"{self._base}/api/chat"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._map_messages(messages),
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        if self._options:
            body["options"] = self._options

        if cancellation_token and cancellation_token.is_cancelled:
            raise Exception("Request cancelled by user")

        async with self._httpx.AsyncClient(timeout=self._timeout, verify=self._verify_arg) as client:
            if cancellation_token:
                http_task = asyncio.create_task(client.post(url, json=body))
                resp = await self._execute_with_cancellation(http_task, cancellation_token)
            else:
                resp = await client.post(url, json=body)

            resp.raise_for_status()
            data = resp.json()
        message = (data or {}).get("message") or {}
        out: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        tcs = message.get("tool_calls") or []
        if tcs:
            out_calls = []
            for tc in tcs:
                func = tc.get("function", {})
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
