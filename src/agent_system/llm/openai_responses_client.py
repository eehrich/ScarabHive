"""OpenAI Responses API client (via OpenRouter's /responses beta endpoint).

WHY THIS CLIENT EXISTS
======================
OpenAI reasoning models (GPT-5.x, o-series) natively produce a SEQUENCE OF
ITEMS per turn: ``[reasoning(rs_*, encrypted_content), function_call(fc_*),
..., message]``. The Chat Completions route flattens that sequence into one
assistant message and OpenRouter's bridge must RECONSTRUCT the item sequence
on every follow-up request — a lossy translation that intermittently delivers
defective encrypted blobs (HTTP 400 "encrypted content ... could not be
decrypted or parsed", deterministic per item; see
``HTTPXOpenAIClient._recover_encrypted_reasoning`` for the reactive healing on
that route).

This client removes the translation entirely: it speaks the Responses API's
item format natively and round-trips the model's output items VERBATIM — the
same pattern the openai-python SDK and Codex use. Verified against OpenRouter
2026-07-16: 12/12 multi-turn tool-call chains with encrypted reasoning items
round-tripped without a verification error.

HOW ITEMS ARE PERSISTED
=======================
The session/message model stays ``ChatMessage`` — nothing outside this client
changes. The model's raw output items are stored verbatim in ONE
``reasoning_details`` block on the assistant message:

    {"type": "reasoning.responses_items",
     "format": "openai-responses-items-v1",
     "index": 0,
     "items": [...verbatim response.output...]}

``content`` and ``tool_calls`` are ALSO populated (duplicated from the items)
so the agent loop, session display and tool execution work unchanged. On the
next request the verbatim items are replayed as-is; content/tool_calls of that
message are NOT re-serialized (they would duplicate the items).

Because ``reasoning_details`` remains the carrier, the whole reasoning-artifact
invalidation infrastructure (utils/reasoning_artifacts.py) applies unchanged:
history mutations strip the blocks of all but the latest assistant message and
flag the latest with ``rd_orphaned``. This client honors the flag: an orphaned
message is NOT replayed verbatim (its chain predecessors are gone — a partial
chain fails verification) but reconstructed from content/tool_calls like
foreign history — a clean chain restart, which the Responses route tolerates
(verified: histories reconstructed WITHOUT old reasoning items are accepted;
only replayed reasoning items themselves must be verbatim).

Foreign reasoning_details formats (``openai-responses-v1`` blocks captured on
the Chat Completions route, ``google-gemini-v1`` thought signatures) are
IGNORED when building input — after a provider/route switch the chain restarts
fresh instead of replaying artifacts that cannot verify here.

STATUS: built 2026-07-16, NOT yet enabled for any configured model
(``provider: openai_responses`` opts a model in).
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time as _time
from typing import Any, Optional

import httpx

from agent_system.llm.models import (
    ChatMessage,
    LLMClient,
    LLMQuotaExhaustedError,
    LLMRateLimitError,
    LLMServerError,
)
from agent_system.llm.httpx_client import HTTPXTimeoutConfig
from agent_system.utils.reasoning_artifacts import strip_all_reasoning_artifacts

logger = logging.getLogger(__name__)

#: format tag of the verbatim-items block this client writes/reads.
RESPONSES_ITEMS_FORMAT = "openai-responses-items-v1"
RESPONSES_ITEMS_TYPE = "reasoning.responses_items"


def _get(msg: Any, key: str, default: Any = None) -> Any:
    """Field access for both dict messages and ChatMessage objects."""
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


class OpenAIResponsesClient(LLMClient):
    """Async client for the OpenAI Responses API via OpenRouter (stateless).

    Non-streaming by design: configure models with ``capabilities.streaming:
    false`` (like the Gemini models) so the agent server takes the
    ``chat_tools`` path. ``chat_tools_streaming`` exists as a thin conformant
    wrapper that emits a single ``final`` chunk.
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        context_window: int = 200000,
        request_timeout: int = 600,
        max_retries: int = 2,
        retry_backoff: float = 2.0,
        ssl_verify: bool = True,
        timeout_config: Optional[HTTPXTimeoutConfig] = None,
        capabilities: Any = None,
        parallel_tool_calls: bool = True,
        thinking_level: Optional[str] = None,
        max_tokens: Optional[int] = None,
        service_tier: Optional[str] = None,
        provider_routing: Optional[dict] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.context_window = context_window
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.ssl_verify = ssl_verify
        self.capabilities = capabilities
        self.parallel_tool_calls = parallel_tool_calls
        self.thinking_level = thinking_level
        self.max_tokens = max_tokens
        self.service_tier = service_tier
        self.provider_routing = provider_routing
        self.timeout_config = timeout_config or HTTPXTimeoutConfig(
            connect=10.0, read=float(request_timeout), write=30.0, pool=5.0
        )

    # ------------------------------------------------------------------
    # Input building: ChatMessage list -> Responses `input` item list
    # ------------------------------------------------------------------

    @staticmethod
    def _content_to_parts(content: Any, role: str) -> Any:
        """Convert message content to Responses format.

        Plain strings pass through (accepted for message items). Chat-format
        content arrays (text / image_url parts, e.g. from the multimodal
        injection) are converted to input_text / input_image parts.
        """
        if not isinstance(content, list):
            return content if content is not None else ""
        parts = []
        for part in content:
            if not isinstance(part, dict):
                parts.append({"type": "input_text", "text": str(part)})
                continue
            ptype = part.get("type")
            if ptype == "text":
                parts.append({"type": "input_text", "text": part.get("text", "")})
            elif ptype == "image_url":
                url = part.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url", "")
                parts.append({"type": "input_image", "image_url": url})
            elif ptype in ("input_text", "input_image", "input_file"):
                parts.append(part)  # already Responses format
            else:
                # Unknown part type (e.g. unsupported audio): degrade to text
                # so the request stays valid instead of 400ing.
                parts.append({"type": "input_text", "text": json.dumps(part, ensure_ascii=False)[:2000]})
        return parts

    @classmethod
    def _extract_verbatim_items(cls, msg: Any) -> Optional[list]:
        """Return the verbatim output items stored on an assistant message,
        or None if the message carries none (foreign/legacy history)."""
        for block in (_get(msg, "reasoning_details") or []):
            if isinstance(block, dict) and block.get("format") == RESPONSES_ITEMS_FORMAT:
                items = block.get("items")
                if isinstance(items, list) and items:
                    return items
        return None

    def _messages_to_input(self, messages: list) -> list:
        """Build the Responses `input` item list from a ChatMessage history.

        Assistant turns produced by THIS client replay their output items
        verbatim (the lossless path). Assistant turns from other routes are
        reconstructed from content/tool_calls — without their foreign
        reasoning artifacts, which cannot verify here (fresh chain start).
        """
        items: list = []
        for msg in messages:
            role = _get(msg, "role")
            content = _get(msg, "content")

            if role in ("system", "user"):
                items.append({
                    "type": "message",
                    "role": role,
                    "content": self._content_to_parts(content, role),
                })

            elif role == "assistant":
                # rd_orphaned (set by invalidate_reasoning_artifacts after a
                # history mutation): this turn's chain predecessors were
                # stripped — replaying its reasoning items verbatim would send
                # a PARTIAL chain, which fails verification. Reconstruct from
                # content/tool_calls instead (clean chain restart).
                verbatim = None if _get(msg, "rd_orphaned") \
                    else self._extract_verbatim_items(msg)
                if verbatim is not None:
                    items.extend(verbatim)
                    continue
                # Foreign/legacy assistant turn: reconstruct without artifacts.
                if content:
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": content if isinstance(content, str) else self._content_to_parts(content, role),
                    })
                for tc in (_get(msg, "tool_calls") or []):
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    items.append({
                        "type": "function_call",
                        "call_id": tc.get("id") if isinstance(tc, dict) else None,
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    })

            elif role == "tool":
                out = content
                if not isinstance(out, str):
                    out = json.dumps(out, ensure_ascii=False) if out is not None else ""
                items.append({
                    "type": "function_call_output",
                    "call_id": _get(msg, "tool_call_id"),
                    "output": out,
                })
                # Multimodal tool content (images from comfyui etc.): inject as
                # a user message item AFTER the tool output — same placement as
                # the Chat Completions clients use.
                if _get(msg, "multimodal_content"):
                    injection = self._create_multimodal_injection(msg)
                    if injection:
                        items.append({
                            "type": "message",
                            "role": "user",
                            "content": self._content_to_parts(injection.get("content"), "user"),
                        })
        return items

    def _create_multimodal_injection(self, tool_msg: Any) -> Optional[dict]:
        from ..utils.multimodal_tool_content import (
            check_vision_support,
            create_multimodal_injection,
        )
        supports_audio = bool(getattr(self.capabilities, "audio_input", False)) if self.capabilities else False
        return create_multimodal_injection(
            tool_msg=tool_msg,
            supports_vision=check_vision_support(self.capabilities),
            model_name=self.model,
            supports_audio=supports_audio,
        )

    @staticmethod
    def _convert_tools(tools: Optional[list]) -> Optional[list]:
        """Chat-format tool schemas -> Responses-format (flat, no nesting)."""
        if not tools:
            return None
        converted = []
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if fn:
                converted.append({
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                })
            elif isinstance(t, dict) and t.get("name"):
                converted.append(t)  # already flat
        return converted or None

    # ------------------------------------------------------------------
    # Payload / request
    # ------------------------------------------------------------------

    def _build_payload(self, messages: list, tools: Optional[list]) -> dict:
        payload: dict = {
            "model": self.model,
            "input": self._messages_to_input(messages),
            "store": False,
        }
        if self.thinking_level:
            payload["reasoning"] = {"effort": self.thinking_level}
        if self.max_tokens:
            payload["max_output_tokens"] = self.max_tokens
        if self.service_tier:
            payload["service_tier"] = self.service_tier
        if self.provider_routing:
            payload["provider"] = self.provider_routing
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = bool(self.parallel_tool_calls)
        return payload

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Response parsing: output items -> assistant dict
    # ------------------------------------------------------------------

    @staticmethod
    def _map_usage(usage: Optional[dict]) -> Optional[dict]:
        """Responses usage -> Chat-Completions-shaped usage (what the cost
        tracking, session_costs.py and the rest of the system understand).

        OpenRouter extras (``cost``, ``cost_details``, ``is_byok``, …) are
        passed through untouched — cost accounting prefers the billed
        OpenRouter cost over recomputing from token counts.
        """
        if not usage:
            return None
        mapped = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens",
                                      usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
        }
        in_details = usage.get("input_tokens_details") or {}
        out_details = usage.get("output_tokens_details") or {}
        if in_details.get("cached_tokens") is not None:
            mapped["prompt_tokens_details"] = {"cached_tokens": in_details.get("cached_tokens", 0)}
        if out_details.get("reasoning_tokens") is not None:
            mapped["completion_tokens_details"] = {"reasoning_tokens": out_details.get("reasoning_tokens", 0)}
        # Pass through everything we didn't explicitly map (cost, cost_details,
        # is_byok, ...) — without clobbering the mapped keys.
        for k, v in usage.items():
            if k in ("input_tokens", "output_tokens", "total_tokens",
                     "input_tokens_details", "output_tokens_details"):
                continue
            mapped.setdefault(k, v)
        return mapped

    def _format_response(self, response_data: dict) -> dict:
        """Build the ``{"assistant": ..., "usage": ...}`` result the agent
        server consumes (same contract as HTTPXOpenAIClient)."""
        err = response_data.get("error")
        if err:
            msg = err.get("message", "Unknown upstream error") if isinstance(err, dict) else str(err)
            code = err.get("code", "unknown") if isinstance(err, dict) else "unknown"
            logger.warning(f"Responses API returned error in body: [{code}] {msg}")
            return {"assistant": {"role": "assistant", "content": "",
                                  "error": {"message": msg, "type": f"upstream_error_{code}"}}}

        output = response_data.get("output") or []
        content_parts: list[str] = []
        tool_calls: list[dict] = []
        reasoning_summaries: list[str] = []

        for item in output:
            itype = item.get("type")
            if itype == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        content_parts.append(part.get("text", ""))
            elif itype == "function_call":
                tool_calls.append({
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                })
            elif itype == "reasoning":
                for s in item.get("summary") or []:
                    if isinstance(s, dict) and s.get("text"):
                        reasoning_summaries.append(s["text"])
                    elif isinstance(s, str):
                        reasoning_summaries.append(s)

        assistant: dict = {"role": "assistant", "content": "".join(content_parts)}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        if reasoning_summaries:
            assistant["reasoning_content"] = "\n\n".join(reasoning_summaries)
        if output:
            # Verbatim replay block — the whole point of this client. ALL
            # output items (reasoning + function_call + message) are stored so
            # the next request reproduces the exact item sequence.
            assistant["reasoning_details"] = [{
                "type": RESPONSES_ITEMS_TYPE,
                "format": RESPONSES_ITEMS_FORMAT,
                "index": 0,
                "items": output,
            }]

        status = response_data.get("status")
        incomplete = response_data.get("incomplete_details")
        if status == "incomplete" and incomplete:
            logger.warning(
                "Responses API returned incomplete response (%s), model=%s",
                incomplete.get("reason"), self.model,
            )

        result: dict = {"assistant": assistant}
        usage = self._map_usage(response_data.get("usage"))
        if usage:
            result["usage"] = usage
        return result

    # ------------------------------------------------------------------
    # HTTP request loop
    # ------------------------------------------------------------------

    @staticmethod
    def _is_encrypted_reasoning_400(body_text: str) -> bool:
        return ("encrypted content" in body_text and "rs_" in body_text) or \
               "invalid_encrypted_content" in body_text

    async def _notify_error(self, url: str, duration_ms: float, error_msg: str) -> None:
        """Post-response notification for terminal failures — keeps the
        message debugger seeing failed requests, like the sibling clients."""
        await self._notify_post_response({
            "provider": "openai_responses", "model": self.model, "url": url,
            "is_streaming": False, "duration_ms": duration_ms,
            "error": error_msg, "timestamp_ms": _time.time() * 1000,
        })

    async def _request(self, messages: list, tools: Optional[list],
                       cancellation_token=None, status_scope=None) -> dict:
        payload = self._build_payload(messages, tools)
        url = f"{self.base_url}/responses"
        _enc_retried = False

        timeout = httpx.Timeout(
            connect=self.timeout_config.connect,
            read=self.timeout_config.read,
            write=self.timeout_config.write,
            pool=self.timeout_config.pool,
        )
        verify: Any = self.ssl_verify
        if self.ssl_verify:
            try:
                verify = ssl.create_default_context()
            except Exception:
                verify = True

        async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
            # while-loop with explicit increments: the one-shot
            # encrypted-reasoning heal must NOT consume a retry slot — with a
            # for-loop a heal on the final attempt would strip the session and
            # then never send the healed request.
            attempt = 0
            while attempt <= self.max_retries:
                if cancellation_token and cancellation_token.is_cancelled:
                    raise asyncio.CancelledError("Request cancelled by user")

                _request_start = _time.time()
                await self._notify_pre_request({
                    "provider": "openai_responses", "model": self.model, "url": url,
                    "is_streaming": False, "payload": payload,
                    "timestamp_ms": _request_start * 1000,
                })
                try:
                    response = await client.post(url, json=payload, headers=self._headers())
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    if attempt < self.max_retries:
                        backoff = self.retry_backoff * (2 ** attempt)
                        logger.warning(
                            f"Responses request transport error ({type(e).__name__}), "
                            f"retry {attempt + 1}/{self.max_retries} in {backoff:.0f}s: {self.model}"
                        )
                        await self._notify_retry(
                            "openai_responses", self.model, url, False,
                            f"transport: {type(e).__name__}", attempt, self.max_retries + 1)
                        await self._cancellable_sleep(backoff, cancellation_token)
                        attempt += 1
                        continue
                    await self._notify_error(
                        url, (_time.time() - _request_start) * 1000,
                        f"transport exhausted: {type(e).__name__}: {e}")
                    raise

                duration_ms = (_time.time() - _request_start) * 1000
                body_text = response.text or ""

                if response.status_code == 429:
                    retry_after = None
                    try:
                        retry_after = float(response.headers.get("retry-after", ""))
                    except (TypeError, ValueError):
                        pass
                    await self._notify_error(url, duration_ms, f"HTTP 429: {body_text[:200]}")
                    if "quota" in body_text.lower() or "exhausted" in body_text.lower():
                        raise LLMQuotaExhaustedError(
                            f"Quota exhausted: {body_text[:200]}",
                            provider="openai_responses", model=self.model, retry_after=retry_after)
                    raise LLMRateLimitError(
                        f"Rate limit exceeded: {body_text[:200]}",
                        provider="openai_responses", model=self.model, retry_after=retry_after)

                if response.status_code >= 500:
                    if attempt < self.max_retries:
                        backoff = self.retry_backoff * (2 ** attempt)
                        logger.warning(
                            f"Responses request {response.status_code}, "
                            f"retry {attempt + 1}/{self.max_retries} in {backoff:.0f}s: {self.model}")
                        await self._notify_retry(
                            "openai_responses", self.model, url, False,
                            f"HTTP {response.status_code}", attempt, self.max_retries + 1)
                        await self._cancellable_sleep(backoff, cancellation_token)
                        attempt += 1
                        continue
                    await self._notify_error(url, duration_ms, f"HTTP {response.status_code}: {body_text[:300]}")
                    raise LLMServerError(
                        f"HTTP {response.status_code}: {body_text[:300]}",
                        provider="openai_responses", model=self.model,
                        status_code=response.status_code)

                if response.status_code >= 400:
                    # Backstop only — the native item round-trip is the fix for
                    # the encrypted-reasoning 400s of the Chat Completions
                    # bridge. Should one still occur (defective item straight
                    # from the provider), heal exactly like the httpx client:
                    # drop the artifacts (session AND next payload) and retry
                    # once with a fresh chain. Does NOT consume a retry slot.
                    if (response.status_code == 400 and not _enc_retried
                            and self._is_encrypted_reasoning_400(body_text)):
                        _enc_retried = True
                        n = strip_all_reasoning_artifacts(messages)
                        payload = self._build_payload(messages, tools)
                        logger.warning(
                            "Responses-400 retry: stripped reasoning artifacts from "
                            "%d message(s) (defective reasoning item). model=%s detail=%r",
                            n, self.model, body_text[:500])
                        await self._notify_retry(
                            "openai_responses", self.model, url, False,
                            "http-400 reasoning-items strip", attempt, self.max_retries + 1)
                        continue
                    error_msg = f"HTTP {response.status_code}: {body_text[:300]}"
                    logger.error(f"Responses request failed: {error_msg}")
                    await self._notify_error(url, duration_ms, error_msg)
                    raise httpx.HTTPStatusError(error_msg, request=response.request, response=response)

                try:
                    response_data = response.json()
                except json.JSONDecodeError:
                    if attempt < self.max_retries:
                        logger.warning(
                            f"Responses body JSON decode failed (len={len(body_text)}), "
                            f"retry {attempt + 1}/{self.max_retries}: {self.model}")
                        await self._cancellable_sleep(self.retry_backoff, cancellation_token)
                        attempt += 1
                        continue
                    await self._notify_error(
                        url, duration_ms, f"JSON decode failed (len={len(body_text)})")
                    raise

                # Body-level error inside an HTTP 200 (OpenRouter proxies
                # upstream errors this way on /responses too). Two cases are
                # handled here instead of surfacing as assistant.error (which
                # would trigger an unnecessary persistent model fallback):
                body_err = response_data.get("error")

                # 1) Transient upstream rate limit (flex-tier overload sends
                #    "rate_limit_exceeded ... too many requests" as a body
                #    error). Backoff and retry like the httpx client does for
                #    body-429s — the condition clears within seconds.
                if body_err and attempt < self.max_retries:
                    err_code = str(body_err.get("code", "")) if isinstance(body_err, dict) else ""
                    err_msg = str(body_err.get("message", "")) if isinstance(body_err, dict) else str(body_err)
                    if ("rate_limit" in err_code or err_code == "429"
                            or "too many requests" in err_msg.lower()):
                        backoff = self.retry_backoff * (2 ** attempt)
                        logger.warning(
                            f"Responses body rate-limit, retry {attempt + 1}/"
                            f"{self.max_retries} in {backoff:.0f}s: {self.model}")
                        await self._notify_retry(
                            "openai_responses", self.model, url, False,
                            "body rate-limit", attempt, self.max_retries + 1)
                        await self._cancellable_sleep(backoff, cancellation_token)
                        attempt += 1
                        continue

                # 2) Defective encrypted reasoning item reported body-level.
                if body_err and not _enc_retried and \
                        self._is_encrypted_reasoning_400(json.dumps(body_err, ensure_ascii=False)):
                    _enc_retried = True
                    n = strip_all_reasoning_artifacts(messages)
                    payload = self._build_payload(messages, tools)
                    logger.warning(
                        "Responses body-error retry: stripped reasoning artifacts "
                        "from %d message(s) (defective reasoning item). model=%s detail=%r",
                        n, self.model, str(body_err)[:500])
                    await self._notify_retry(
                        "openai_responses", self.model, url, False,
                        "body-error reasoning-items strip", attempt, self.max_retries + 1)
                    continue

                await self._notify_post_response({
                    "provider": "openai_responses", "model": self.model, "url": url,
                    "is_streaming": False, "duration_ms": duration_ms,
                    "response_data": response_data,
                    # Chat-shaped usage: session_costs.py & co. read
                    # $.prompt_tokens/$.completion_tokens from the stored
                    # usage_json — the raw Responses shape would yield 0s.
                    "usage": self._map_usage(response_data.get("usage")),
                    "timestamp_ms": _time.time() * 1000,
                })
                return self._format_response(response_data)

        raise LLMServerError(  # pragma: no cover — loop always returns/raises
            "Responses request retries exhausted",
            provider="openai_responses", model=self.model, status_code=599)

    # ------------------------------------------------------------------
    # LLMClient interface
    # ------------------------------------------------------------------

    async def chat(self, messages: list[ChatMessage], cancellation_token=None, status_scope=None) -> str:
        result = await self._request(messages, None, cancellation_token, status_scope)
        return result.get("assistant", {}).get("content", "") or ""

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict],
                         cancellation_token=None, status_scope=None) -> dict:
        return await self._request(messages, tools, cancellation_token, status_scope)

    async def chat_tools_streaming(self, messages: list[ChatMessage], tools: list[dict],
                                   cancellation_token=None, status_scope=None):
        """Non-streaming wrapper: one ``final`` chunk with the full result.

        Configure Responses models with ``capabilities.streaming: false`` so
        the server prefers ``chat_tools``; this wrapper keeps things working
        if the streaming path is taken anyway.
        """
        result = await self._request(messages, tools, cancellation_token, status_scope)
        chunk = {"type": "final", "assistant": result.get("assistant", {})}
        if "usage" in result:
            chunk["usage"] = result["usage"]
        yield chunk

    async def close(self) -> None:  # per-request AsyncClient — nothing to close
        return None
