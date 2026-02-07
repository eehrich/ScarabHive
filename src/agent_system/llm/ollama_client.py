from __future__ import annotations

from typing import Optional, Any
import asyncio
import time as _time
from ..utils.id import short_id

from .models import ChatMessage, LLMClient
from ..config.models import ModelCapabilitiesConfig
from . import ollama_utils


class OllamaNativeAsyncClient(LLMClient):
    """Async client for native Ollama REST API (/api/chat).

    Supports per-request options including num_ctx.
    """

    def __init__(self, model: str, base_url: Optional[str] = None, options: Optional[dict[str, Any]] = None, timeout: Optional[float] = None, verify: Optional[bool] = None, context_window: Optional[int] = None, capabilities: Optional[ModelCapabilitiesConfig] = None) -> None:
        import httpx  # lazy import
        self._httpx = httpx
        self._base = (base_url.rstrip("/")) if base_url else "http://127.0.0.1:11434"
        self.model = model
        self.provider = "ollama"
        self.context_window = context_window
        self._options = options or {}
        self._timeout = timeout or 60.0
        self.capabilities = capabilities  # Pydantic model or None

        # Validate API type - Ollama only supports chat_completions (native API)
        if self.capabilities and hasattr(self.capabilities, 'default_api_type'):
            api_type = self.capabilities.default_api_type
            # Extract value from enum if it's an enum
            if hasattr(api_type, 'value'):
                api_type = api_type.value
            else:
                api_type = str(api_type) if api_type else 'chat_completions'

            if api_type not in ('chat_completions', None):
                raise NotImplementedError(
                    f"Ollama client only supports 'chat_completions' API (native Ollama API). "
                    f"Requested API type: '{api_type}'. "
                    f"Ollama does not support OpenAI's Realtime or Assistants APIs. "
                    f"Current model: {self.model}"
                )

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
            # Use model_dump() with mode='json' to properly serialize nested Pydantic models and datetime objects
            d = m.model_dump(exclude_none=True, mode='json')

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

            # Normalize for Ollama format (extract images to separate field)
            d = ollama_utils.normalize_message(d)
            out.append(d)
        return out

    async def _map_messages_async(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """Async wrapper for message mapping to avoid blocking event loop."""
        return await asyncio.to_thread(self._map_messages, messages)

    async def chat(self, messages: list[ChatMessage], cancellation_token=None) -> str:
        url = f"{self._base}/api/chat"
        mapped_messages = await self._map_messages_async(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": mapped_messages,
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

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None) -> dict:
        url = f"{self._base}/api/chat"
        mapped_messages = await self._map_messages_async(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": mapped_messages,
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

        # Build result with usage information
        result = {"assistant": out}

        # Extract usage metadata if available (Ollama format)
        # Ollama provides: eval_count (completion tokens), prompt_eval_count (prompt tokens)
        if "eval_count" in data or "prompt_eval_count" in data:
            usage = {}
            if "prompt_eval_count" in data:
                usage["prompt_tokens"] = data["prompt_eval_count"]
            if "eval_count" in data:
                usage["completion_tokens"] = data["eval_count"]
            if "prompt_eval_count" in data and "eval_count" in data:
                usage["total_tokens"] = data["prompt_eval_count"] + data["eval_count"]
            result["usage"] = usage

        return result

    async def chat_tools_streaming(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None):
        """Stream LLM responses from Ollama using native streaming API.

        Ollama's /api/chat endpoint supports streaming with `stream: true`.
        Each line is a JSON object with message deltas.
        """
        import logging
        logger = logging.getLogger(__name__)
        
        # Status reporting helper
        async def report_status(message: str) -> None:
            if status_scope is None:
                return
            try:
                await status_scope.progress(message)
            except Exception as e:
                logger.debug(f"Failed to report LLM status: {e}")
        
        url = f"{self._base}/api/chat"
        mapped_messages = await self._map_messages_async(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": mapped_messages,
            "stream": True,  # Enable streaming
        }
        if tools:
            body["tools"] = tools
        if self._options:
            body["options"] = self._options

        if cancellation_token and cancellation_token.is_cancelled:
            raise Exception("Request cancelled by user")

        # Retry logic for stream interruptions
        max_retries = 3
        retry_backoff = 1.0

        _request_start = _time.time()
        await self._notify_pre_request({
            "provider": "ollama", "model": self.model,
            "url": url, "is_streaming": True,
            "timestamp_ms": _request_start * 1000,
        })

        for attempt in range(max_retries + 1):
            if cancellation_token and cancellation_token.is_cancelled:
                raise Exception("Request cancelled by user")

            # Initialize/reset accumulated state for each attempt
            accumulated_content = []
            accumulated_tool_calls = {}
            accumulated_usage = None  # usage information from final chunk (done=true)

            try:
                async with self._httpx.AsyncClient(timeout=self._timeout, verify=self._verify_arg) as client:
                    async with client.stream("POST", url, json=body) as response:
                        # Handle server errors (5xx) - retry with exponential backoff
                        if response.status_code >= 500 and attempt < max_retries:
                            backoff_time = retry_backoff * (2 ** attempt)
                            await report_status(f"Server error ({response.status_code}), retry {attempt + 1}/{max_retries} in {backoff_time:.0f}s: {self.model}")
                            logger.warning(f"Ollama server error {response.status_code}, retrying in {backoff_time}s")
                            await self._notify_retry("ollama", self.model, url, True, f"Server error ({response.status_code})", attempt, max_retries + 1)
                            await self._cancellable_sleep(backoff_time, cancellation_token)
                            continue
                        
                        response.raise_for_status()

                        # Use timeout from config for chunk-level timeout
                        chunk_timeout = self._timeout
                        line_iter = response.aiter_lines().__aiter__()
                        
                        while True:
                            if cancellation_token and cancellation_token.is_cancelled:
                                raise Exception("Request cancelled by user")
                            
                            try:
                                line = await asyncio.wait_for(line_iter.__anext__(), timeout=chunk_timeout)
                            except StopAsyncIteration:
                                break  # Stream completed
                            except asyncio.TimeoutError:
                                await report_status(f"Stream timeout after {chunk_timeout}s: {self.model}")
                                logger.warning(f"Ollama stream chunk timeout after {chunk_timeout}s")
                                raise Exception(f"Stream stalled - no data for {chunk_timeout}s")

                            if not line.strip():
                                continue

                            try:
                                chunk_data = response.json() if hasattr(line, 'json') else self._httpx.json.loads(line)
                            except Exception:
                                import json
                                try:
                                    chunk_data = json.loads(line)
                                except Exception:
                                    continue

                            # Check if stream is done - final chunk may contain usage info
                            if chunk_data.get("done"):
                                # Extract usage metadata if available (prompt_eval_count, eval_count, etc.)
                                # Ollama provides: eval_count (completion tokens), prompt_eval_count (prompt tokens)
                                if "eval_count" in chunk_data or "prompt_eval_count" in chunk_data:
                                    accumulated_usage = {}
                                    if "prompt_eval_count" in chunk_data:
                                        accumulated_usage["prompt_tokens"] = chunk_data["prompt_eval_count"]
                                    if "eval_count" in chunk_data:
                                        accumulated_usage["completion_tokens"] = chunk_data["eval_count"]
                                    if "prompt_eval_count" in chunk_data and "eval_count" in chunk_data:
                                        accumulated_usage["total_tokens"] = chunk_data["prompt_eval_count"] + chunk_data["eval_count"]
                                break

                            message = chunk_data.get("message", {})

                            # Handle content delta
                            content = message.get("content")
                            if content:
                                accumulated_content.append(content)
                                yield {
                                    "type": "content_delta",
                                    "delta": content,
                                    "accumulated": "".join(accumulated_content)
                                }

                            # Handle tool call deltas
                            tool_calls = message.get("tool_calls")
                            if tool_calls:
                                for tc in tool_calls:
                                    # Ollama sends complete tool calls, not deltas
                                    # Extract index if available, otherwise use name as key
                                    func = tc.get("function", {})
                                    tc_id = tc.get("id") or f"call_{short_id()}"
                                    name = func.get("name", "")
                                    index = len(accumulated_tool_calls)  # Assign next index

                                    if index not in accumulated_tool_calls:
                                        accumulated_tool_calls[index] = {
                                            "id": tc_id,
                                            "type": "function",
                                            "function": {"name": name, "arguments": func.get("arguments", {})}
                                    }

                                yield {
                                    "type": "tool_call_delta",
                                    "index": index,
                                    "delta": tc,
                                    "accumulated": accumulated_tool_calls[index]
                                }

                # Build final assistant message (after async with block)
                assistant = {
                    "role": "assistant",
                    "content": "".join(accumulated_content) if accumulated_content else None
                }

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
                    "provider": "ollama", "model": self.model,
                    "url": url, "is_streaming": True,
                    "duration_ms": _duration_ms,
                    "usage": accumulated_usage,
                    "timestamp_ms": _time.time() * 1000,
                })

                yield {"type": "final", **final_result}
                return  # Success - exit retry loop

            except (self._httpx.RemoteProtocolError, self._httpx.NetworkError, self._httpx.ConnectError) as e:
                if attempt < max_retries:
                    backoff_time = retry_backoff * (2 ** attempt)
                    await report_status(f"Stream interrupted, retry {attempt + 1}/{max_retries} in {backoff_time:.0f}s: {self.model}")
                    logger.warning(f"Ollama stream interrupted (attempt {attempt + 1}/{max_retries + 1}), retrying in {backoff_time}s: {e}")
                    await self._notify_retry("ollama", self.model, url, True, f"Stream interrupted: {e}", attempt, max_retries + 1)
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                    continue
                else:
                    await report_status(f"Stream failed after {max_retries + 1} attempts: {self.model}")
                    logger.error(f"Ollama streaming failed after {max_retries + 1} attempts: {e}")
                    yield {"type": "final", "assistant": {"role": "assistant", "content": "", "error": {"error": f"Stream failed after {max_retries + 1} attempts: {e}"}}}
                    return

            except Exception as e:
                await report_status(f"Request failed: {self.model}")
                logger.exception("Ollama streaming failed: %s", e)
                yield {"type": "final", "assistant": {"role": "assistant", "content": "", "error": {"error": str(e)}}}
                return

    def supports_streaming(self) -> bool:
        """Check if this client supports streaming based on model capabilities."""
        # Check if capabilities explicitly disable streaming
        if self.capabilities and hasattr(self.capabilities, 'streaming'):
            return self.capabilities.streaming
        return True  # Default to True if capabilities not set
