from __future__ import annotations

from typing import Optional, Any
import asyncio
import contextlib
import json
import time as _time

import httpx

from agent_system.utils.id import short_id

from agent_system.llm.message_roles import (
    DEVELOPER, NOTE_CLOSE, NOTE_OPEN, SYSTEM, USER, conversation_opener, developer_turn,
    resolve_rung, rung_for_position,
)
from agent_system.llm.models import (
    ChatMessage, LLMClient, LLMConnectionError, LLMRateLimitError, LLMServerError,
)
from agent_system.llm.structured_output import JSON_OBJECT, JSON_SCHEMA, ResponseFormat
from agent_system.llm.tls import httpx_verify
from agent_system.config.models import ModelCapabilitiesConfig
from plugins.llm_common import cancellation
from plugins.llm_common.model_dialects import (
    reasoning_replay_flags, resolve_reasoning_details_mode,
)
from . import ollama_utils


def _ending_error(error: BaseException) -> str:
    """The post_llm_response error text for a request that ended by *error*."""
    if isinstance(error, asyncio.CancelledError):
        return str(error) or "cancelled"
    if isinstance(error, GeneratorExit):
        return "stream abandoned by the caller"
    return str(error) or type(error).__name__


class OllamaNativeAsyncClient(LLMClient):
    """Async client for native Ollama REST API (/api/chat).

    Supports per-request options including num_ctx.
    """

    #: /api/chat ``format``: "json" or a JSON schema; the model entry says whether the model
    #: holds to it (capabilities.structured_output).
    response_format_kinds = (JSON_SCHEMA, JSON_OBJECT)

    #: Streaming only: retries after a 5xx or a dropped connection, and the
    #: first wait (doubling: 1, 2, 4 s).
    max_retries = 3
    retry_backoff = 1.0

    def __init__(self, model: str, base_url: Optional[str] = None, options: Optional[dict[str, Any]] = None, timeout: Optional[float] = None, verify: Optional[bool] = None, context_window: Optional[int] = None, capabilities: Optional[ModelCapabilitiesConfig] = None, think: Optional[bool | str] = None, reasoning_details_mode: Optional[str] = None) -> None:
        import httpx  # lazy import
        self._httpx = httpx
        self._base = (base_url.rstrip("/")) if base_url else "http://127.0.0.1:11434"
        # model_health keys its blocks by base_url: without it a 404 on one
        # Ollama host blocked the same model name on every host.
        self.base_url = self._base
        self.model = model
        self.provider = "ollama"
        self.context_window = context_window
        self._options = options or {}
        self._timeout = timeout or 60.0
        self.capabilities = capabilities  # Pydantic model or None
        # /api/chat `think`: a bool, or "low"/"medium"/"high". None leaves it
        # to the model -- a thinking model then thinks and says so in
        # message.thinking.
        self._think = think
        self._can_think: Optional[bool] = None  # asked once, see _think_for_request
        # Which stored reasoning goes back as `thinking` (keep_all | keep_last | strip).
        self.reasoning_details_mode = resolve_reasoning_details_mode(
            reasoning_details_mode, model=model, default="keep_all")

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

        # The configured flag stays readable as-is; what httpx gets is the
        # process-wide context for that flag (no per-client SSL setup).
        self._verify = verify if verify is not None else True
        self._verify_arg = httpx_verify(self._verify)

    @property
    def _developer_rung(self) -> str:
        return resolve_rung(getattr(self.capabilities, "developer_role", None),
                            ceiling=SYSTEM, default=SYSTEM, route="Ollama /api/chat")

    def _map_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        from agent_system.utils.json_utils import repair_json as _repair_json
        out: list[dict[str, Any]] = []
        opener = conversation_opener(messages)
        may_replay = reasoning_replay_flags(messages, self.reasoning_details_mode)
        for i, m in enumerate(messages):
            # Use model_dump() with mode='json' to properly serialize nested Pydantic models and datetime objects
            d = m.model_dump(exclude_none=True, mode='json')
            d.pop('injected_by', None)  # Internal hook metadata
            d.pop('rd_orphaned', None)  # Internal reasoning-invalidation marker (utils/reasoning_artifacts.py)
            d.pop('reasoning_model', None)  # Producer of reasoning_details, never sent

            # /api/chat documents "either `system`, `user`, `assistant`, or
            # `tool`". An unknown role is not refused here -- it is handed to
            # the model's chat template, which typically renders a role it does
            # not know as nothing at all, so the note would be dropped behind a
            # 200. A system turn is the documented rung; a model entry may set
            # `capabilities.developer_role: user` for a template that only ever
            # renders the FIRST system message.
            #
            # Except the LAST one, which rides the user rung whatever the entry
            # says: a request ending on a system turn asks for nothing, and gets
            # nothing. Nothing this loop adds ever follows a developer message
            # (only a tool result gets an injected image message), so the last
            # input stays the last message on the wire. See
            # message_roles.rung_for_position.
            if d.get("role") == DEVELOPER:
                content = d.get("content")
                role, text = developer_turn(
                    content if isinstance(content, str) else "",
                    rung_for_position(self._developer_rung, last=m is messages[-1],
                                      opens=m is opener))
                d["role"] = role
                if role == USER and isinstance(content, list):
                    # Parts, not a string: the tags go round them, and
                    # normalize_message below still finds the images. As a
                    # string the parts were replaced by an empty note.
                    d["content"] = [{"type": "text", "text": NOTE_OPEN}, *content,
                                    {"type": "text", "text": NOTE_CLOSE}]
                elif role == USER:
                    d["content"] = text

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
                                # Attempt repair — local LLMs are most prone to malformed JSON
                                repaired = _repair_json(args)
                                if repaired is not None and isinstance(repaired, dict):
                                    tc["function"]["arguments"] = repaired
                                else:
                                    tc["function"]["arguments"] = {}

            # Normalize for Ollama format (extract images to separate field)
            d = ollama_utils.normalize_message(d)
            if not may_replay[i]:
                d.pop("thinking", None)
            out.append(d)

            # A tool's image rides a user message behind the result, as on
            # every other route; normalize_message turns its data URLs into
            # `images`. Without this the screenshot never reached the model.
            if m.role == "tool" and m.multimodal_content:
                from agent_system.utils.multimodal_tool_content import (
                    check_vision_support, create_multimodal_injection,
                )
                injection = create_multimodal_injection(
                    tool_msg=m, supports_vision=check_vision_support(self.capabilities),
                    model_name=self.model,
                    supports_audio=bool(getattr(self.capabilities, "audio_input", False)))
                if injection:
                    out.append(ollama_utils.normalize_message(injection))
        return out

    async def _map_messages_async(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """Async wrapper for message mapping to avoid blocking event loop."""
        return await asyncio.to_thread(self._map_messages, messages)

    async def _body(self, messages: list[ChatMessage], tools: Optional[list[dict]], stream: bool,
                    response_format: Optional[ResponseFormat] = None) -> dict[str, Any]:
        self._require_response_format(response_format)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": await self._map_messages_async(messages),
            "stream": stream,
        }
        if tools:
            body["tools"] = tools
        if response_format is not None:
            # /api/chat: "json" is JSON mode, a JSON schema constrains the answer to it.
            body["format"] = "json" if response_format.type == JSON_OBJECT else response_format.schema
        if self._options:
            body["options"] = self._options
        think = await self._think_for_request()
        if think is not None:
            body["think"] = think
        return body

    async def _think_for_request(self) -> Optional[bool | str]:
        """`think` as configured -- unless the model cannot think at all.

        Ollama refuses `think: true` (or a level) with a 400 "does not support
        thinking" for a model without the capability, while `false` passes.
        A level set for a whole chain (`llm_params: {"*": ...}`) reaches every
        entry in it, so the model is asked once instead of failing every call.
        """
        if not self._think:
            return self._think
        if self._can_think is None:
            try:
                # Metadata, not generation: a short cap, so a server that
                # hangs costs this once and not a full request timeout.
                async with self._httpx.AsyncClient(timeout=min(self._timeout, 10.0), verify=self._verify_arg) as client:
                    resp = await client.post(f"{self._base}/api/show", json={"model": self.model})
                    resp.raise_for_status()
                    self._can_think = "thinking" in (resp.json().get("capabilities") or [])
            except Exception:
                # Unknown: send it as configured and let the request say why
                # it fails -- asked once, not before every call.
                self._can_think = True
            if not self._can_think:
                import logging
                logging.getLogger(__name__).warning(
                    "think=%r dropped for %s: the model does not support thinking.",
                    self._think, self.model)
        return self._think if self._can_think else None

    async def _raise_for_status(self, resp: Any) -> None:
        """raise_for_status, carrying the reason Ollama gave.

        httpx's own message names only the status: every missing model, every
        "does not support tools" read as a bare "404 Not Found" / "400 Bad
        Request". Ollama says why in the body's `error` field.
        """
        try:
            resp.raise_for_status()
        except self._httpx.HTTPStatusError as e:
            reason = ""
            try:
                await resp.aread()
                reason = resp.text
                reason = resp.json().get("error") or reason
            except Exception:
                pass  # not JSON: the raw text is the reason
            raise self._httpx.HTTPStatusError(
                f"Ollama {resp.status_code}: {reason}" if reason else str(e),
                request=e.request, response=e.response) from e

    @staticmethod
    def _usage(data: dict[str, Any]) -> Optional[dict[str, int]]:
        # prompt_eval_count = prompt tokens, eval_count = completion tokens
        # (thinking included -- Ollama has no separate count for it).
        if "eval_count" not in data and "prompt_eval_count" not in data:
            return None
        usage: dict[str, int] = {}
        if "prompt_eval_count" in data:
            usage["prompt_tokens"] = data["prompt_eval_count"]
        if "eval_count" in data:
            usage["completion_tokens"] = data["eval_count"]
        if "prompt_eval_count" in data and "eval_count" in data:
            usage["total_tokens"] = data["prompt_eval_count"] + data["eval_count"]
        return usage

    def _final_error(self, error: Exception) -> Exception:
        """What a provider failure reaches the agent server as: the types its fallback reads.

        429 blocks the model and falls back, 5xx and a lost connection or
        timeout fall back, any other status is the ``httpx.HTTPStatusError``
        that falls back (and blocks a dead model on 404). Anything else stays
        as it is.
        """
        text = str(error) or type(error).__name__
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            if status == 429:
                return LLMRateLimitError(text, provider="ollama", model=self.model)
            if status >= 500:
                return LLMServerError(text, provider="ollama", model=self.model, status_code=status)
            return error
        if isinstance(error, httpx.TransportError):
            return LLMConnectionError(f"Network/protocol error: {text}", provider="ollama", model=self.model)
        return error

    def _no_json(self, status: int, text: str) -> LLMConnectionError:
        """Something else answered (a proxy, a captive portal, a web UI): not Ollama, fall back."""
        return LLMConnectionError(f"Ollama answered {status} with a body that is no JSON: {text[:200]}",
                                  provider="ollama", model=self.model)

    async def _post(self, body: dict[str, Any], cancellation_token) -> dict[str, Any]:
        """One blocking /api/chat call, reported to post_llm_response exactly once however it ends."""
        url = f"{self._base}/api/chat"
        start = _time.time()
        await self._notify_pre_request({
            "provider": "ollama", "model": self.model, "url": url,
            "payload": body, "is_streaming": False, "timestamp_ms": start * 1000,
        })
        ended = False

        async def report_end(**info: Any) -> None:
            nonlocal ended
            ended = True  # before the await: a cancel landing in the hook must not report twice
            await self._notify_post_response({
                "provider": "ollama", "model": self.model, "url": url, "is_streaming": False,
                "duration_ms": (_time.time() - start) * 1000,
                "timestamp_ms": _time.time() * 1000, **info,
            })

        try:
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")
            try:
                async with self._httpx.AsyncClient(timeout=self._timeout, verify=self._verify_arg) as client:
                    if cancellation_token:
                        http_task = asyncio.create_task(client.post(url, json=body))
                        resp = await cancellation.await_call(http_task, cancellation_token)
                    else:
                        resp = await client.post(url, json=body)
                    await self._raise_for_status(resp)
                    try:
                        data = resp.json() or {}
                    except ValueError:
                        raise self._no_json(resp.status_code, resp.text) from None
            except httpx.HTTPError as e:
                final = self._final_error(e)
                if final is e:
                    raise
                raise final from e
            await report_end(response_data=data, usage=self._usage(data),
                             finish_reason=data.get("done_reason"))
            return data
        except BaseException as error:
            # A refusal, a cancel (token or task), anything unexpected: reported
            # here, once, and passed on unchanged.
            if not ended:
                await report_end(error=_ending_error(error))
            raise

    async def chat(self, messages: list[ChatMessage], cancellation_token=None, *,
                   response_format: Optional[ResponseFormat] = None) -> str:
        data = await self._post(await self._body(messages, None, stream=False, response_format=response_format),
                                cancellation_token)
        msg = data.get("message") or {}
        return msg.get("content") or ""

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None,
                         *, response_format: Optional[ResponseFormat] = None) -> dict:
        data = await self._post(await self._body(messages, tools, stream=False, response_format=response_format),
                                cancellation_token)
        message = data.get("message") or {}
        out: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        # A thinking model's reasoning arrives beside the answer, never in it.
        if message.get("thinking"):
            out["reasoning_content"] = message["thinking"]
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

        result: dict[str, Any] = {"assistant": out}
        usage = self._usage(data)
        if usage:
            result["usage"] = usage
        # "length": the answer was cut at num_predict -- the loop's truncation guard reads it
        if data.get("done_reason"):
            result["finish_reason"] = data["done_reason"]

        return result

    async def chat_tools_streaming(self, messages: list[ChatMessage], tools: list[dict], cancellation_token=None, status_scope=None,
                                   *, response_format: Optional[ResponseFormat] = None):
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
        body = await self._body(messages, tools, stream=True, response_format=response_format)

        max_retries, retry_backoff = self.max_retries, self.retry_backoff

        _request_start = _time.time()
        await self._notify_pre_request({
            "provider": "ollama", "model": self.model,
            "url": url, "payload": body, "is_streaming": True,
            "timestamp_ms": _request_start * 1000,
        })
        # Every way this request ends reaches post_llm_response exactly once.
        # Ollama sends its usage only on the last line, so an ending before it
        # has none to report.
        ended = False

        async def report_end(**info: Any) -> None:
            nonlocal ended
            ended = True  # before the await: a cancel landing in the hook must not report twice
            await self._notify_post_response({
                "provider": "ollama", "model": self.model, "url": url, "is_streaming": True,
                "duration_ms": (_time.time() - _request_start) * 1000,
                "timestamp_ms": _time.time() * 1000, **info,
            })

        try:
            # aclosing: a caller that stops reading closes this generator, and
            # the HTTP response underneath closes with it -- now, not whenever
            # the garbage collector gets to it.
            async with contextlib.aclosing(self._stream_attempts(
                    url, body, max_retries, retry_backoff, cancellation_token,
                    report_status, report_end)) as attempts:
                async for event in attempts:
                    yield event
        except BaseException as error:
            # A refusal after the retries, a cancel (token, task, or during a
            # retry wait), a caller that stops reading (GeneratorExit), anything
            # unexpected: reported here, once, and passed on unchanged.
            if not ended:
                await report_end(error=_ending_error(error))
            raise

    async def _stream_attempts(self, url, body, max_retries, retry_backoff, cancellation_token,
                               report_status, report_end):
        """The attempts of one streaming request; its callers report its end."""
        import logging
        logger = logging.getLogger(__name__)
        # The caller has seen deltas of an attempt that did not finish: a retry
        # starts from scratch, so it is told to drop them (stream_restart).
        yielded_delta = False
        for attempt in range(max_retries + 1):
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")
            if yielded_delta:
                yielded_delta = False
                yield {"type": "stream_restart"}

            # Initialize/reset accumulated state for each attempt
            accumulated_content = []
            accumulated_thinking = []
            accumulated_tool_calls = {}
            accumulated_usage = None  # usage information from final chunk (done=true)
            done_reason = None  # ...and why the answer ended ("length": cut at num_predict)

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
                        
                        await self._raise_for_status(response)

                        # Use timeout from config for chunk-level timeout
                        chunk_timeout = self._timeout
                        line_iter = response.aiter_lines().__aiter__()
                        done = False
                        first_lines: list[str] = []  # for the error when no line was Ollama's
                        
                        while True:
                            if cancellation_token and cancellation_token.is_cancelled:
                                raise asyncio.CancelledError("Request cancelled by user")
                            
                            try:
                                line = await asyncio.wait_for(line_iter.__anext__(), timeout=chunk_timeout)
                            except StopAsyncIteration:
                                break  # Stream completed
                            except asyncio.TimeoutError:
                                await report_status(f"Stream timeout after {chunk_timeout}s: {self.model}")
                                logger.warning(f"Ollama stream chunk timeout after {chunk_timeout}s")
                                raise httpx.ReadTimeout(f"Stream stalled - no data for {chunk_timeout}s")

                            if not line.strip():
                                continue

                            try:
                                chunk_data = json.loads(line)
                            except json.JSONDecodeError:
                                if len(first_lines) < 5:
                                    first_lines.append(line)
                                continue

                            # A failure after the 200 comes as its own line;
                            # read past, it looked like an empty answer.
                            if chunk_data.get("error"):
                                raise RuntimeError(f"Ollama: {chunk_data['error']}")

                            # Check if stream is done - final chunk may contain usage info
                            if chunk_data.get("done"):
                                done_reason = chunk_data.get("done_reason")
                                # Extract usage metadata if available (prompt_eval_count, eval_count, etc.)
                                # Ollama provides: eval_count (completion tokens), prompt_eval_count (prompt tokens)
                                accumulated_usage = self._usage(chunk_data)
                                done = True
                                break

                            message = chunk_data.get("message", {})

                            # Thinking streams ahead of the answer, in its own
                            # field. Unread, a thinking model looked silent to
                            # the panel and to the reasoning-loop watchdog.
                            thinking = message.get("thinking")
                            if thinking:
                                accumulated_thinking.append(thinking)
                                yielded_delta = True
                                yield {
                                    "type": "thinking_delta",
                                    "delta": thinking,
                                    "accumulated": "".join(accumulated_thinking)
                                }

                            # Handle content delta
                            content = message.get("content")
                            if content:
                                accumulated_content.append(content)
                                yielded_delta = True
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

                                    # Inside the for loop: one delta PER tool
                                    # call -- outside it, only the last of a
                                    # multi-call chunk was ever emitted.
                                    yielded_delta = True
                                    yield {
                                        "type": "tool_call_delta",
                                        "index": index,
                                        "delta": tc,
                                        "accumulated": accumulated_tool_calls[index]
                                    }

                        if not done:
                            # Ollama ends every stream with a done line; without
                            # one the answer is cut or was never Ollama's (a
                            # proxy's HTML page read as an empty success).
                            if first_lines:
                                raise self._no_json(response.status_code, "\n".join(first_lines))
                            raise LLMConnectionError("Ollama's stream ended without its done line",
                                                     provider="ollama", model=self.model)

                # Build final assistant message (after async with block)
                assistant = {
                    "role": "assistant",
                    "content": "".join(accumulated_content) if accumulated_content else None
                }
                if accumulated_thinking:
                    assistant["reasoning_content"] = "".join(accumulated_thinking)

                if accumulated_tool_calls:
                    tool_calls_list = [accumulated_tool_calls[i] for i in sorted(accumulated_tool_calls.keys())]
                    assistant["tool_calls"] = tool_calls_list

                # Build final result with usage
                final_result = {"assistant": assistant}
                if accumulated_usage:
                    final_result["usage"] = accumulated_usage
                if done_reason:
                    final_result["finish_reason"] = done_reason

                await report_end(usage=accumulated_usage, finish_reason=done_reason)

                yield {"type": "final", **final_result}
                return  # Success - exit retry loop

            except (httpx.RemoteProtocolError, httpx.NetworkError) as e:
                if attempt < max_retries:
                    backoff_time = retry_backoff * (2 ** attempt)
                    await report_status(f"Stream interrupted, retry {attempt + 1}/{max_retries} in {backoff_time:.0f}s: {self.model}")
                    logger.warning(f"Ollama stream interrupted (attempt {attempt + 1}/{max_retries + 1}), retrying in {backoff_time}s: {e}")
                    await self._notify_retry("ollama", self.model, url, True, f"Stream interrupted: {e}", attempt, max_retries + 1)
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                    continue
                await report_status(f"Stream failed after {max_retries + 1} attempts: {self.model}")
                logger.error(f"Ollama streaming failed after {max_retries + 1} attempts: {e}")
                # Typed, so the agent server falls back at once: this request
                # has had its whole retry cycle.
                raise LLMConnectionError(f"Stream failed after {max_retries + 1} attempts: {e}",
                                         provider="ollama", model=self.model) from e

            except LLMConnectionError:
                raise  # already typed (no done line): not an error answer

            except httpx.HTTPError as e:
                # A status (a 5xx after the retries, 4xx at once) or a timeout:
                # the types the agent server's fallback reads.
                await report_status(f"Request failed: {self.model}")
                final = self._final_error(e)
                if final is e:
                    raise
                raise final from e

            except Exception as e:
                # An error line after the 200 (the runner died, ...): an error
                # answer, which the agent server asks once more before it falls back.
                await report_status(f"Request failed: {self.model}")
                logger.exception("Ollama streaming failed: %s", e)
                await report_end(error=str(e))
                yield {"type": "final", "assistant": {"role": "assistant", "content": "", "error": {
                    "error": True, "type": "ollama_api_error", "message": str(e)}}}
                return

    def supports_streaming(self) -> bool:
        """Check if this client supports streaming based on model capabilities."""
        # Check if capabilities explicitly disable streaming
        if self.capabilities and hasattr(self.capabilities, 'streaming'):
            return self.capabilities.streaming
        return True  # Default to True if capabilities not set
