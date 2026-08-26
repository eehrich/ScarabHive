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

STATUS: built 2026-07-16; seit 3baf994b BREIT AKTIV — alle openai/gpt-5.x-
Profile in config/llm_openrouter.yaml fahren ``provider: openai_responses``.
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
from agent_system.llm.cache_key import (
    CACHE_BP_SENTINEL,
    CACHE_MODE_TASK_SEQUENCE,
    MARKER_STYLE_ANTHROPIC,
    MARKER_STYLE_NONE,
    MARKER_STYLE_OPENAI,
    anthropic_cache_conversation,
    boundary_registry,
    cap_cache_control,
    derive_prompt_cache_key,
    mark_conversation_tail,
    mark_last_system,
    mark_last_tool,
    messages_have_history,
    plan_cache_blocks,
    strip_cache_breakpoints,
)
from plugins_llm.llm_common.schema_sanitize import sanitize_schema_for_gemini
from .httpx_client import HTTPXTimeoutConfig
from plugins_llm.llm_common.openai_utils import convert_audio_to_input_audio
from agent_system.utils.reasoning_artifacts import strip_all_reasoning_artifacts

logger = logging.getLogger(__name__)

#: Body-level error codes that are the 5xx class in an HTTP 200 envelope:
#: retried here instead of escalating to a model switch.
_TRANSIENT_BODY_ERROR_CODES = frozenset({"server_error"})

#: format tag of the verbatim-items block this client writes/reads.
RESPONSES_ITEMS_FORMAT = "openai-responses-items-v1"
RESPONSES_ITEMS_TYPE = "reasoning.responses_items"

#: Audio containers the Responses input item officially accepts (openai SDK
#: ``ResponseInputAudioParam.format``: Literal["mp3", "wav"]). Other containers
#: are passed through with a warning — the receiving bridge (OpenRouter ->
#: Gemini) accepts more than OpenAI's own literal, and a loud 400 beats
#: silently dropping the audio.
RESPONSES_AUDIO_FORMATS = ("mp3", "wav")


def _get(msg: Any, key: str, default: Any = None) -> Any:
    """Field access for both dict messages and ChatMessage objects."""
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def _part_to_dict(part: Any) -> Any:
    """Normalize one content part to a plain dict.

    The agent loop hands ChatMessage OBJECTS to the client, so a multimodal
    ``content`` list still holds the pydantic models from llm/models.py
    (TextContent, ImageContent, AudioContent, ...) — unlike the Chat
    Completions route, which runs ``model_dump()`` on every message before
    normalizing. Without this step an AudioContent fell through to
    ``str(part)`` and the ENTIRE base64 payload travelled as TEXT (live find
    2026-07-25: a 1.9 MB ``input_text`` part, rejected with "[invalid_prompt]
    The input token count exceeds the maximum ... 1048576").
    """
    if isinstance(part, dict):
        return part
    if hasattr(part, "model_dump"):
        return part.model_dump(exclude_none=True, mode="json")
    return part


def _image_to_input_image(part: dict) -> Optional[dict]:
    """Internal image part -> Responses ``input_image`` (or None if unusable).

    Accepts both house shapes: OpenAI-style (``image_url`` as string or
    ``{"url": ...}``) and Anthropic-style (``source={"type": "base64",
    "media_type": ..., "data": ...}``). The Responses item always carries the
    picture in ``image_url`` — base64 goes in as a ``data:`` URL.
    """
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if not url:
        source = part.get("source")
        if isinstance(source, dict):
            if source.get("data"):
                media_type = (source.get("media_type")
                              or part.get("media_type") or "image/png")
                url = f"data:{media_type};base64,{source['data']}"
            else:
                url = source.get("url")
    if not isinstance(url, str) or not url:
        return None
    converted: dict = {"type": "input_image", "image_url": url}
    detail = part.get("detail")
    if detail:
        converted["detail"] = detail
    return converted


def _audio_to_input_audio(part: dict) -> Optional[dict]:
    """Internal audio part -> Responses ``input_audio`` (or None if unusable).

    Same item shape as on the Chat Completions route (proven there: the
    OpenRouter -> Gemini bridge consumes ``input_audio``), so the converter is
    reused from openai_utils — no second audio dialect.
    """
    converted = convert_audio_to_input_audio(part)
    if not isinstance(converted, dict) or converted.get("type") != "input_audio":
        return None  # converter could not read the payload
    fmt = (converted.get("input_audio") or {}).get("format")
    if fmt not in RESPONSES_AUDIO_FORMATS:
        logger.warning(
            "Responses input_audio: format %r is outside the officially "
            "supported set %s — passing through to the provider.",
            fmt, RESPONSES_AUDIO_FORMATS,
        )
    return converted


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
        safety_settings: Optional[dict] = None,
        prompt_cache_key: Optional[str] = None,
        prompt_cache_mode: Optional[str] = None,
        prompt_cache_marker_style: Optional[str] = None,
        temperature: Optional[float] = None,
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
        self.safety_settings = safety_settings
        self.prompt_cache_key = prompt_cache_key
        self.prompt_cache_mode = prompt_cache_mode
        self.prompt_cache_marker_style = prompt_cache_marker_style
        self.temperature = temperature
        self.timeout_config = timeout_config or HTTPXTimeoutConfig(
            connect=10.0, read=float(request_timeout), write=30.0, pool=5.0
        )
        #: Number of media parts dropped for lack of a model capability —
        #: countable signal for callers/tests (see _drop_media).
        self.dropped_media_parts: int = 0

    # ------------------------------------------------------------------
    # Input building: ChatMessage list -> Responses `input` item list
    # ------------------------------------------------------------------

    @property
    def _supports_audio_input(self) -> bool:
        return bool(getattr(self.capabilities, "audio_input", False)) if self.capabilities else False

    @property
    def _supports_vision(self) -> bool:
        from agent_system.utils.multimodal_tool_content import check_vision_support
        return check_vision_support(self.capabilities)

    def _drop_media(self, kind: str, reason: str, part: dict) -> dict:
        """Replace an un-sendable media part with a VISIBLE text note.

        Never a serialized blob of the payload: that is what let the model
        judge audio against a truncated JSON string and answer confidently
        anyway (live find 2026-07-25). The note tells the model the media is
        missing, the WARNING tells the operator, and ``dropped_media_parts``
        gives the caller a countable signal.
        """
        self.dropped_media_parts += 1
        name = part.get("name")
        logger.warning(
            "Responses input: dropped %s part%s — %s (model=%s). The model "
            "answers WITHOUT this media.",
            kind, f" {name!r}" if name else "", reason, self.model,
        )
        return {"type": "input_text",
                "text": f"[{kind} input omitted: {reason}]"}

    def _content_to_parts(self, content: Any, role: str) -> Any:
        """Convert message content to Responses format.

        Plain strings pass through (accepted for message items). Content arrays
        — chat-format parts from the multimodal injection AS WELL AS the
        pydantic content models a caller puts on ChatMessage — are converted to
        the Responses items input_text / input_image / input_audio.

        Media is capability-gated: if the model has no audio/vision input the
        part is DROPPED and replaced by a visible note (see ``_drop_media``).
        The generic ``[:2000]`` degradation stays as the last resort for
        genuinely unknown part types, and can no longer swallow media.
        """
        if not isinstance(content, list):
            return content if content is not None else ""
        parts = []
        for raw_part in content:
            part = _part_to_dict(raw_part)
            if not isinstance(part, dict):
                parts.append({"type": "input_text", "text": str(part)})
                continue
            ptype = part.get("type")
            if ptype == "text":
                parts.append({"type": "input_text",
                              "text": strip_cache_breakpoints(part.get("text", ""))})
            elif ptype == "text_file":
                # parity with openai_utils.normalize_content_item
                parts.append({"type": "input_text",
                              "text": f"[File: {part.get('name') or 'file'}]\n"
                                      f"{part.get('content', '')}"})
            elif ptype in ("image", "image_url", "input_image"):
                if not self._supports_vision:
                    parts.append(self._drop_media(
                        "image", "model has no image_input capability", part))
                    continue
                converted = _image_to_input_image(part)
                parts.append(converted if converted else self._drop_media(
                    "image", "no usable image data (neither image_url nor source)", part))
            elif ptype in ("audio", "input_audio"):
                if not self._supports_audio_input:
                    parts.append(self._drop_media(
                        "audio", "model has no audio_input capability", part))
                    continue
                converted = _audio_to_input_audio(part)
                parts.append(converted if converted else self._drop_media(
                    "audio", "no usable audio data (neither audio_url nor base64 source)", part))
            elif ptype == "video":
                # The Responses API has no video input item (openai SDK
                # ResponseInputContentParam = text | image | file).
                parts.append(self._drop_media(
                    "video", "the Responses API has no video input item", part))
            elif ptype in ("input_text", "input_file"):
                parts.append(part)  # already Responses format
            else:
                # Unknown part type: degrade to text so the request stays valid
                # instead of 400ing. Media never reaches this branch.
                parts.append({"type": "input_text", "text": json.dumps(part, ensure_ascii=False)[:2000]})
        return parts

    def _extract_verbatim_items(self, msg: Any) -> Optional[list]:
        """Return the verbatim output items stored on an assistant message,
        or None if the message carries none (foreign/legacy history).

        The format tag is not enough to decide that: every model on this route
        writes the same tag, while the encrypted payload inside is bound to the
        model that produced it. A block from a DIFFERENT model is therefore
        foreign — replaying it gets the turn rejected (the gateway answers
        "produced under a different model"), which is how a mid-run model
        switch used to poison the whole session. Blocks written before the
        model was recorded count as foreign too: a fresh chain start is the
        cheap side of that bet.

        A message can carry MORE than one matching block: the message
        validator merges consecutive assistant messages by concatenating
        their reasoning_details. Collect ALL matching blocks in order —
        returning only the first silently dropped the second turn's items
        (reasoning, text AND function_calls), and a tool result answering a
        dropped call is a 400.
        """
        collected: list = []
        for block in (_get(msg, "reasoning_details") or []):
            if isinstance(block, dict) and block.get("format") == RESPONSES_ITEMS_FORMAT:
                if block.get("model") != self.model:
                    continue
                items = block.get("items")
                if isinstance(items, list) and items:
                    collected.extend(items)
        return collected or None

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
                # Cache-Breakpoint-Sentinels bleiben hier im String erhalten —
                # der Split passiert in _build_payload NACH der Key-Ableitung
                # (die Segment-Leiter braucht den aufgeloesten Key).
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
        from agent_system.utils.multimodal_tool_content import create_multimodal_injection
        return create_multimodal_injection(
            tool_msg=tool_msg,
            supports_vision=self._supports_vision,
            model_name=self.model,
            supports_audio=self._supports_audio_input,
        )

    @property
    def _is_gemini_model(self) -> bool:
        """Gemini via OpenRouter — needs tool-schema sanitization (same as
        the Chat Completions route's _sanitize_tools_for_gemini)."""
        return self.model.startswith("google/gemini")

    def _convert_tools(self, tools: Optional[list]) -> Optional[list]:
        """Chat-format tool schemas -> Responses-format (flat, no nesting).

        For Gemini models the parameter schemas are sanitized like on the
        Chat Completions route: Gemini's Function Declarations reject JSON
        Schema keywords (additionalProperties, default, format, oneOf, ...)
        and complex nested schemas then fail with MALFORMED_FUNCTION_CALL.
        """
        if not tools:
            return None
        sanitize = self._is_gemini_model
        converted = []
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if fn:
                params = fn.get("parameters", {})
                if sanitize and params:
                    params = sanitize_schema_for_gemini(params)
                converted.append({
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": params,
                })
            elif isinstance(t, dict) and t.get("name"):
                clean = dict(t)
                if sanitize and clean.get("parameters"):
                    clean["parameters"] = sanitize_schema_for_gemini(clean["parameters"])
                converted.append(clean)  # already flat
        return converted or None

    # ------------------------------------------------------------------
    # Payload / request
    # ------------------------------------------------------------------

    def _apply_cache_blocks(self, items: list, resolved_key: Optional[str]) -> None:
        """Cache-Breakpoint-Sentinels in input-Items verarbeiten (in place).

        Dieser Client bedient nur OpenAI-Modelle -> Marker-Stil ist openai
        (prompt_cache_breakpoint), sofern nicht per Config auf none gestellt.
        Bei prompt_cache_mode=task_sequence ergaenzt die Segment-Leiter den
        BP1-Read-Anker aus der Prozess-Registry (s. cache_key.py).
        """
        style = self.prompt_cache_marker_style or MARKER_STYLE_OPENAI
        ladder_used = False
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, str) or CACHE_BP_SENTINEL not in content:
                continue
            if style == MARKER_STYLE_NONE or not resolved_key:
                item["content"] = strip_cache_breakpoints(content)
                continue
            # Leiter nur fuer die ERSTE Sentinel-Message pro Request — zwei
            # Messages wuerden sonst denselben Registry-Key thrashen.
            item_mode = self.prompt_cache_mode
            if item_mode == CACHE_MODE_TASK_SEQUENCE:
                if ladder_used:
                    item_mode = None
                ladder_used = True
            plan = plan_cache_blocks(
                content, mode=item_mode, key=resolved_key,
                registry=boundary_registry,
            )
            if plan is None:
                continue
            parts = []
            for block, marked in plan:
                part: dict = {"type": "input_text", "text": block}
                if marked:
                    part["prompt_cache_breakpoint"] = {"mode": "explicit"}
                parts.append(part)
            item["content"] = parts

    def _apply_anthropic_cache_blocks(self, items: list, tools: Optional[list]) -> None:
        """Anthropic cache_control auf Responses-input-Items (Zukunfts-Pfad).

        Aktuell routet KEIN Claude-Modell ueber die Responses-API — alle Claude
        laufen via httpx-OpenRouter (Chat Completions) bzw. natives SDK. Diese
        Verdrahtung greift, sobald ein Modell mit ``provider=openai_responses``
        UND ``prompt_cache_marker_style=anthropic`` konfiguriert wird; sie nutzt
        exakt dieselbe geteilte Policy (cache_key.py) wie die anderen Claude-
        Pfade — kein zweiter Cache-Dialekt.

        Anthropic verwendet cache_control (nicht die OpenAI-Breakpoint-Sentinels),
        deshalb: etwaige Sentinels rueckstandsfrei entfernen und stattdessen den
        System-Prefix + (bei Multi-Turn) den wachsenden Konversations-Tail + die
        letzte Tool-Definition als Breakpoints markieren; der Cap erzwingt das
        harte 4-Block-Limit.

        String-Content wird zuerst in Responses-``input_text``-Parts gehoben,
        damit der Marker auf einem gueltigen Responses-Blocktyp landet und nicht
        auf ``{"type": "text"}`` (Chat-Completions-Format) — sonst waere das
        Payload ungueltig.
        """
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if isinstance(content, str):
                # Responses-API: Assistant-Text ist output_text, sonst input_text.
                part_type = "output_text" if item.get("role") == "assistant" else "input_text"
                item["content"] = [
                    {"type": part_type, "text": strip_cache_breakpoints(content)}
                ]
            elif isinstance(content, list):
                for part in content:
                    if (isinstance(part, dict)
                            and isinstance(part.get("text"), str)
                            and CACHE_BP_SENTINEL in part["text"]):
                        part["text"] = strip_cache_breakpoints(part["text"])
        mark_last_system(items)
        if anthropic_cache_conversation(
            self.prompt_cache_mode, messages_have_history(items)
        ):
            mark_conversation_tail(items)
        mark_last_tool(tools or [])
        cap_cache_control([tools or [], items])

    def _build_payload(self, messages: list, tools: Optional[list]) -> dict:
        payload: dict = {
            "model": self.model,
            "input": self._messages_to_input(messages),
            "store": False,
        }
        if self.thinking_level:
            payload["reasoning"] = {"effort": self.thinking_level}
        # Sampling-Temperatur nur ohne Reasoning: die o-/gpt-5.x-Serie
        # akzeptiert den Param nicht (400 "temperature is not supported"),
        # dort steuert reasoning.effort. 0.0 ist gültig → auf None prüfen.
        # BEWUSST auch bei thinking_level="none" unterdrückt: OpenAI-Hybride
        # lehnen temperature≠1 auch mit abgeschaltetem Thinking ab.
        if self.temperature is not None and not self.thinking_level:
            payload["temperature"] = self.temperature
        if self.max_tokens:
            payload["max_output_tokens"] = self.max_tokens
        if self.service_tier:
            payload["service_tier"] = self.service_tier
        if self.provider_routing:
            payload["provider"] = self.provider_routing
        # Gemini via OpenRouter: content-filter thresholds (BLOCK_NONE for
        # fiction prose etc.) — same shape the Chat Completions route sends.
        if self.safety_settings:
            payload["safety_settings"] = [
                {"category": category, "threshold": threshold}
                for category, threshold in self.safety_settings.items()
            ]
        # GPT-5.6+: ohne prompt_cache_key praktisch kein Cache-Matching
        # (OpenAI-Doku: "you must set prompt_cache_key ..."). "auto" =
        # Praefix-Hash, kollisionsfrei bei parallelen Buechern
        # (s. cache_key.py); kein Extended-Retention-Opt-in.
        resolved_key = None
        if self.prompt_cache_key:
            resolved_key = derive_prompt_cache_key(
                self.prompt_cache_key, payload["input"]
            )
            payload["prompt_cache_key"] = resolved_key
        converted_tools = self._convert_tools(tools)
        if self.prompt_cache_marker_style == MARKER_STYLE_ANTHROPIC:
            # Zukunfts-Pfad: Claude via Responses-API -> cache_control statt
            # OpenAI-Breakpoints (dieselbe geteilte Policy wie httpx/native).
            self._apply_anthropic_cache_blocks(payload["input"], converted_tools)
        else:
            self._apply_cache_blocks(payload["input"], resolved_key)
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
                # Who produced these items. The encrypted payload only verifies
                # against this model, so the replay side checks it.
                "model": self.model,
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
    def _is_reasoning_artifact_rejection(body_text: str) -> bool:
        """Reasoning-artifact rejection — OpenAI encrypted items, Gemini
        thought signatures, or a cross-model replay refused by the gateway.
        All heal the same way: drop the artifacts, retry on the SAME model.

        The last two clauses catch the gateway's cross-model wording
        ("encrypted REASONING ... produced under a different model"), which
        the "encrypted content" clause misses.
        """
        lowered = body_text.lower()
        return ("encrypted content" in body_text and "rs_" in body_text) or \
               "invalid_encrypted_content" in body_text or \
               "thought signature" in lowered or \
               "encrypted reasoning" in lowered or \
               "produced under a different model" in lowered

    @staticmethod
    def _is_transient_body_error(body_err: Any) -> bool:
        """A body-level error that is worth one more call on the SAME model.

        Only the gateway's 5xx-equivalent qualifies. Everything else (content
        filter, malformed request, unknown model) is deterministic: repeating
        it would burn calls and delay the fallback chain that exists for it.
        """
        if not isinstance(body_err, dict):
            return False
        return str(body_err.get("code", "")).lower() in _TRANSIENT_BODY_ERROR_CODES

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
        # Muss VOR der Schleife stehen: gesetzt wird es nur in den 429-Zweigen,
        # gelesen aber in der Encrypted-Reasoning-400-Heilung — ein 400 ohne
        # vorheriges 429 lief sonst in UnboundLocalError (Live-Fund 2026-07-25,
        # gemini-3.6-flash-Lauf: "cannot access local variable '_tier_dropped'").
        _tier_dropped = False

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
                    # Flex-tier fallback (parity with the httpx route): flex
                    # queues saturate with 429s while the standard tier is
                    # fine. Drop service_tier ONCE and retry immediately —
                    # does not consume a retry slot (the request changes
                    # substantially). Only after that: typed raise so the
                    # agent-level fallback chain takes over.
                    if payload.pop("service_tier", None) is not None:
                        _tier_dropped = True
                        logger.warning(
                            f"HTTP 429 on flex tier — dropping service_tier and "
                            f"retrying at standard tier: {self.model}")
                        await self._notify_retry(
                            "openai_responses", self.model, url, False,
                            "429 flex->standard tier drop", attempt, self.max_retries + 1)
                        continue
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
                    # 404 is included because the gateway answers a cross-model
                    # replay with it — but ONLY via the body test, since 404 is
                    # also "no such model", which must keep reaching the chain.
                    if (response.status_code in (400, 404) and not _enc_retried
                            and self._is_reasoning_artifact_rejection(body_text)):
                        _enc_retried = True
                        n = strip_all_reasoning_artifacts(messages)
                        payload = self._build_payload(messages, tools)
                        if _tier_dropped:
                            payload.pop("service_tier", None)
                        logger.warning(
                            "Responses-%d retry: stripped reasoning artifacts from "
                            "%d message(s) (defective reasoning item). model=%s detail=%r",
                            response.status_code, n, self.model, body_text[:500])
                        await self._notify_retry(
                            "openai_responses", self.model, url, False,
                            f"http-{response.status_code} reasoning-items strip",
                            attempt, self.max_retries + 1)
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
                        # First 429: drop the flex tier (saturated flex queue,
                        # standard is usually fine) and retry without consuming
                        # a slot — ported from the httpx route's body-429 heal.
                        if payload.pop("service_tier", None) is not None:
                            _tier_dropped = True
                            logger.warning(
                                f"Body rate-limit on flex tier — dropping "
                                f"service_tier, retrying at standard: {self.model}")
                            await self._notify_retry(
                                "openai_responses", self.model, url, False,
                                "body-429 flex->standard tier drop",
                                attempt, self.max_retries + 1)
                            continue
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
                        self._is_reasoning_artifact_rejection(json.dumps(body_err, ensure_ascii=False)):
                    _enc_retried = True
                    n = strip_all_reasoning_artifacts(messages)
                    payload = self._build_payload(messages, tools)
                    if _tier_dropped:
                        payload.pop("service_tier", None)
                    logger.warning(
                        "Responses body-error retry: stripped reasoning artifacts "
                        "from %d message(s) (defective reasoning item). model=%s detail=%r",
                        n, self.model, str(body_err)[:500])
                    await self._notify_retry(
                        "openai_responses", self.model, url, False,
                        "body-error reasoning-items strip", attempt, self.max_retries + 1)
                    continue

                # 3) Transient upstream failure reported body-level: the 5xx
                #    class in a 200 envelope. Retried here because the agent
                #    server's only lever on assistant.error is a model switch.
                #    Code-gated, not blanket: a deterministic refusal (content
                #    filter) must keep reaching the fallback chain at once.
                if (body_err and attempt < self.max_retries
                        and self._is_transient_body_error(body_err)):
                    backoff = self.retry_backoff * (2 ** attempt)
                    logger.warning(
                        "Responses body server-error, retry %d/%d in %.0fs: %s (%s)",
                        attempt + 1, self.max_retries, backoff, self.model,
                        str(body_err)[:200])
                    await self._notify_retry(
                        "openai_responses", self.model, url, False,
                        "body server-error", attempt, self.max_retries + 1)
                    await self._cancellable_sleep(backoff, cancellation_token)
                    attempt += 1
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
