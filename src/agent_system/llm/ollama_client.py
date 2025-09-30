from __future__ import annotations

from typing import Optional, Any
import asyncio
from ..utils.id import short_id

from .models import ChatMessage, LLMClient


class OllamaNativeAsyncClient(LLMClient):
    """Async client for native Ollama REST API (/api/chat).

    Supports per-request options including num_ctx.
    """

    def __init__(self, model: str, base_url: Optional[str] = None, options: Optional[dict[str, Any]] = None, timeout: Optional[float] = None, verify: Optional[bool] = None) -> None:
        import httpx  # lazy import
        self._httpx = httpx
        self._base = (base_url.rstrip("/")) if base_url else "http://127.0.0.1:11434"
        self.model = model
        self._options = options or {}
        self._timeout = timeout or 60.0
        self.verify = verify if verify is not None else True

    def _map_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            d: dict[str, Any] = {"role": m.role}
            if m.content is not None:
                d["content"] = m.content
            if m.role == "tool":
                if m.name:
                    d["tool_name"] = m.name
                if m.tool_call_id:
                    d["tool_call_id"] = m.tool_call_id
            elif m.role == "assistant" and m.tool_calls:
                d["tool_calls"] = m.tool_calls
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

        async with self._httpx.AsyncClient(timeout=self._timeout, verify=self.verify) as client:
            if cancellation_token:
                http_task = asyncio.create_task(client.post(url, json=body))
                while not http_task.done():
                    try:
                        resp = await asyncio.wait_for(asyncio.shield(http_task), timeout=0.5)
                        break
                    except asyncio.TimeoutError:
                        if cancellation_token.is_cancelled:
                            http_task.cancel()
                            try:
                                await http_task
                            except asyncio.CancelledError:
                                pass
                            raise Exception("Request cancelled by user during Ollama call")
                        continue
                resp = await http_task
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
            "tools": tools,
            "stream": False,
        }
        if self._options:
            body["options"] = self._options

        if cancellation_token and cancellation_token.is_cancelled:
            raise Exception("Request cancelled by user")

        async with self._httpx.AsyncClient(timeout=self._timeout, verify=self.verify) as client:
            if cancellation_token:
                http_task = asyncio.create_task(client.post(url, json=body))
                while not http_task.done():
                    try:
                        resp = await asyncio.wait_for(asyncio.shield(http_task), timeout=0.5)
                        break
                    except asyncio.TimeoutError:
                        if cancellation_token.is_cancelled:
                            http_task.cancel()
                            try:
                                await http_task
                            except asyncio.CancelledError:
                                pass
                            raise Exception("Request cancelled by user during Ollama call")
                        continue
                resp = await http_task
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
