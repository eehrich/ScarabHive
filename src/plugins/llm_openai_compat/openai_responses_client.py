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
import time as _time
from typing import Any, Optional

import httpx

from agent_system.llm.tls import httpx_verify
from agent_system.llm.message_roles import (
    ASSISTANT, DEVELOPER, NOTE_CLOSE, NOTE_OPEN, SYSTEM, TOOL, USER,
    as_note, conversation_opener, resolve_rung, rung_for_position,
)
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
from plugins.llm_common.model_dialects import (
    reasoning_replay_flags,
    resolve_reasoning_details_mode,
    resolve_tool_schema_dialect,
    tool_schema_sanitizer,
)
from .httpx_client import (
    HTTPXTimeoutConfig,
    openrouter_routing_info,
    routing_pinned_to_last_backend,
)
from plugins.llm_common.openai_utils import convert_audio_to_input_audio
from agent_system.utils.reasoning_artifacts import strip_all_reasoning_artifacts

logger = logging.getLogger(__name__)

#: Body-level error codes that are the 5xx class in an HTTP 200 envelope:
#: retried here instead of escalating to a model switch.
_TRANSIENT_BODY_ERROR_CODES = frozenset({"server_error"})

#: The Responses API names its truncation reasons differently from the
#: chat-completions ``finish_reason`` the agent server's guards key on.
#: An unmapped reason is passed through verbatim rather than dropped — a
#: new upstream reason must stay visible, not vanish into a "complete"
#: looking answer.
_INCOMPLETE_REASON_TO_FINISH = {
    "max_output_tokens": "length",
    "content_filter": "content_filter",
}

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

    Streams when the model's ``capabilities.streaming`` allows it, and takes
    the ``chat_tools`` path otherwise — same rule as the sibling clients. Both
    paths share ONE request loop (``_request_events``): the retry, the
    flex-tier drop and the reasoning-artifact healing are the same code, and
    the final result comes from ``_format_response`` in both cases, so the
    verbatim item replay, the backend pin and the truncation guard do not
    depend on which path ran.
    """

    #: Name this client reports to the hook consumers (message debugger, cost
    #: accounting) and puts on its typed errors. A subclass that swaps only
    #: the transport overrides it, so the two routes stay distinguishable in
    #: the debugger and in session_costs — without a second copy of the
    #: retry/healing loop.
    _PROVIDER = "openai_responses"

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
        parallel_tool_calls: Optional[bool] = True,
        tool_schema_dialect: Optional[str] = None,
        reasoning_details_mode: Optional[str] = None,
        thinking_level: Optional[str] = None,
        max_tokens: Optional[int] = None,
        service_tier: Optional[str] = None,
        provider_routing: Optional[dict] = None,
        safety_settings: Optional[dict] = None,
        prompt_cache_key: Optional[str] = None,
        prompt_cache_mode: Optional[str] = None,
        prompt_cache_marker_style: Optional[str] = None,
        temperature: Optional[float] = None,
        plugins: Optional[list] = None,
        prompt_cache_options: Optional[dict] = None,
        safety_identifier: Optional[str] = None,
        stream_silence_timeout: Optional[float] = None,
    ) -> None:
        self.model = model
        self.stream_silence_timeout = stream_silence_timeout
        self.api_key = api_key
        self.plugins = plugins
        if plugins and "openrouter.ai" not in base_url.lower():
            logger.warning(
                "plugins are configured for model=%s but its endpoint is not "
                "OpenRouter (%s) — they are NOT sent.", model, base_url)
        self.prompt_cache_options = prompt_cache_options
        self.safety_identifier = safety_identifier
        self.base_url = base_url.rstrip("/")
        self.context_window = context_window
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.ssl_verify = ssl_verify
        self.capabilities = capabilities
        self.parallel_tool_calls = parallel_tool_calls
        self.tool_schema_dialect = resolve_tool_schema_dialect(
            tool_schema_dialect, model=model)
        # keep_all is what this route has always done: it replays the model's
        # output items VERBATIM, and an encrypted chain missing its earlier
        # links is exactly the "could not be verified" 400 this client exists
        # to avoid. The chat route's keep_last default is a Gemini
        # thought-signature rule and does not apply here.
        self.reasoning_details_mode = resolve_reasoning_details_mode(
            reasoning_details_mode, model=model, default="keep_all")
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
    def _developer_rung(self) -> str:
        # The Responses API has the role itself -- a declaration can only lower
        # it, for a gateway that forwards to a backend without one.
        return resolve_rung(getattr(self.capabilities, "developer_role", None),
                            ceiling=DEVELOPER, default=DEVELOPER,
                            route=f"the Responses API at {self.base_url}")

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

    def _lower_developer_items(self, items: list) -> None:
        """Put every developer item on the rung this route uses.

        LAST, after the cache markers: while the item still says `developer`,
        mark_last_system and mark_conversation_tail both look straight past it,
        which is the point. Lowered first, a note on the `system` rung would
        take the system breakpoint off the prompt, and one at the end would
        take the conversation breakpoint -- in both cases the marker would sit
        on the one text that is rewritten every call.

        Only the user rung touches the content: there the role no longer says
        what the text is, so the tags have to. Parts are wrapped part by part
        rather than stringified, so an image inside a note survives.

        THE LAST ITEM, AND THE ONE THE CONVERSATION OPENS ON, ARE SPECIAL
        CASES, whatever the rung says -- ``message_roles.rung_for_position``
        decides that and carries the measurement. Every route needs it, so it
        does not live here.
        """
        rung = self._developer_rung
        last = items[-1] if items else None
        opener = conversation_opener(items)
        for item in items:
            item.pop("injected_by", None)  # read by conversation_opener, never sent
            if item.get("role") != DEVELOPER:
                continue
            item["role"] = rung_for_position(rung, last=item is last, opens=item is opener)
            if item["role"] != USER:
                continue
            content = item.get("content")
            if isinstance(content, list):
                item["content"] = [{"type": "input_text", "text": NOTE_OPEN}, *content,
                                   {"type": "input_text", "text": NOTE_CLOSE}]
            else:
                item["content"] = as_note(content if isinstance(content, str) else "")

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

        ``reasoning_details_mode`` decides HOW MANY of the own turns replay:
        ``keep_all`` (default here) every one of them, ``keep_last`` only the
        most recent, ``strip`` none. A turn that may not replay is
        reconstructed from content/tool_calls exactly like a foreign one, so
        the request stays valid either way — what changes is whether the
        encrypted chain (and the cached prefix) survives the turn.
        """
        may_replay = reasoning_replay_flags(messages, self.reasoning_details_mode)
        items: list = []
        for index, msg in enumerate(messages):
            role = _get(msg, "role")
            content = _get(msg, "content")

            # A developer item stays `developer` through this whole function,
            # even when the rung is lowered: the cache markers run after it and
            # must be able to tell a run note from the conversation. The rung
            # is applied at the very end of _build_payload.
            if role in (SYSTEM, USER, DEVELOPER):
                # Cache-Breakpoint-Sentinels bleiben hier im String erhalten —
                # der Split passiert in _build_payload NACH der Key-Ableitung
                # (die Segment-Leiter braucht den aufgeloesten Key).
                item = {
                    "type": "message",
                    "role": role,
                    "content": self._content_to_parts(content, role),
                }
                if role == DEVELOPER and _get(msg, "injected_by"):
                    # read and dropped by _lower_developer_items: it tells a wake from a note
                    item["injected_by"] = _get(msg, "injected_by")
                items.append(item)

            elif role == ASSISTANT:
                # rd_orphaned (set by invalidate_reasoning_artifacts after a
                # history mutation): this turn's chain predecessors were
                # stripped — replaying its reasoning items verbatim would send
                # a PARTIAL chain, which fails verification. Reconstruct from
                # content/tool_calls instead (clean chain restart).
                verbatim = None if (_get(msg, "rd_orphaned") or not may_replay[index]) \
                    else self._extract_verbatim_items(msg)
                if verbatim is not None:
                    # The block keeps the model's RAW arguments string, while
                    # tool_calls on the same message carry the copy
                    # history_safe_tool_calls repaired. Replaying the raw one
                    # got every later request rejected ("function.arguments
                    # must be valid JSON") until a fallback model rebuilt the
                    # turn from tool_calls. Resolving here, at replay, also
                    # heals sessions persisted before this fix.
                    safe_args = {
                        tc.get("id"): tc["function"]["arguments"]
                        for tc in (_get(msg, "tool_calls") or [])
                        if isinstance(tc, dict)
                        and isinstance((tc.get("function") or {}).get("arguments"), str)
                    }
                    for item in verbatim:
                        if isinstance(item, dict) and item.get("type") == "function_call":
                            args = safe_args.get(item.get("call_id") or item.get("id"))
                            if args is not None and args != item.get("arguments"):
                                item = {**item, "arguments": args}
                        items.append(item)
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

            elif role == TOOL:
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

            else:
                # No else used to exist here, so a role this chain does not know
                # left the request without a trace -- which is how a developer
                # message was lost before this client learned the role. Send it
                # as a user turn rather than drop it, and say so.
                logger.warning("unknown message role %r at index %d sent as a "
                               "user turn (model=%s)", role, index, self.model)
                items.append({
                    "type": "message",
                    "role": USER,
                    "content": self._content_to_parts(content, USER),
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

    def _convert_tools(self, tools: Optional[list]) -> Optional[list]:
        """Chat-format tool schemas -> Responses-format (flat, no nesting).

        The declared ``tool_schema_dialect`` names the sanitiser the endpoint
        needs (llm_common.model_dialects), and this route looks it up rather
        than asking which family it is: e.g. Gemini's Function Declarations
        reject JSON Schema keywords (additionalProperties, default, format,
        oneOf, ...) and complex nested schemas then fail with
        MALFORMED_FUNCTION_CALL. Declared per model entry because the model
        NAME says nothing about it — the same family is reachable through
        gateway aliases (``~google/gemini-flash-latest``) and through
        translating gateways.

        Unlike the Chat Completions route this one always sends
        ``parameters``, ``{}`` for a tool without any.
        """
        if not tools:
            return None
        sanitize = tool_schema_sanitizer(self.tool_schema_dialect)
        converted = []
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if fn:
                params = fn.get("parameters", {})
                if sanitize and params:
                    params = sanitize(params)
                converted.append({
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": params,
                })
            elif isinstance(t, dict) and t.get("name"):
                clean = dict(t)
                if sanitize and clean.get("parameters"):
                    clean["parameters"] = sanitize(clean["parameters"])
                # "Flat" does not imply "tagged": a caller that hands over
                # {name, description, parameters} produced an item without a
                # discriminator, which is not a valid Responses tool. The
                # nested branch above always tags; this one used to pass the
                # gap through to the provider.
                clean.setdefault("type", "function")
                converted.append(clean)
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
        # No `instructions` field. It used to carry the newest volatile note
        # (a developer message with `injected_by`), lifted out of `input` --
        # the one place a text can sit without joining the conversation, since
        # OpenAI does not carry it over with previous_response_id. Two things
        # killed it. It stands at the TOP of the context, so a text that
        # changes every call rewrites the head of the prompt and takes the
        # cached prefix with it. And "the newest marked note" stopped naming
        # one thing once the hook plugins began appending marked blocks of
        # their own: the pick became a race, and the loop's own last word --
        # the max-steps request, which only works as the LAST thing the model
        # reads -- was the most likely one to be carried off to the head.
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
            # From the ORIGINAL messages, not payload["input"]: its items are
            # built fresh, and only developer items keep `injected_by` (until
            # _lower_developer_items). Without the marker the derivation takes a
            # plugin block rebuilt on every call for part of the prompt.
            resolved_key = derive_prompt_cache_key(
                self.prompt_cache_key, messages
            )
            payload["prompt_cache_key"] = resolved_key
            # Sticky routing on the same key. OpenRouter's prompt cache is
            # BACKEND-local, so calls that share a prefix only hit it while
            # they share a backend; session_id is the gateway's key for
            # keeping them together (measured 2026-09-01: 6/6 calls on one
            # provider with it, 4 different providers without). The cache key
            # is exactly the right grouping — it already means "same stable
            # prefix" — and needs no plumbing the client does not have.
            if self._is_openrouter:
                payload["session_id"] = resolved_key
        if self.plugins and self._is_openrouter:
            payload["plugins"] = self.plugins
        if self.prompt_cache_options:
            payload["prompt_cache_options"] = self.prompt_cache_options
        if self.safety_identifier:
            payload["safety_identifier"] = self.safety_identifier
        converted_tools = self._convert_tools(tools)
        if self.prompt_cache_marker_style == MARKER_STYLE_ANTHROPIC:
            # Zukunfts-Pfad: Claude via Responses-API -> cache_control statt
            # OpenAI-Breakpoints (dieselbe geteilte Policy wie httpx/native).
            self._apply_anthropic_cache_blocks(payload["input"], converted_tools)
        else:
            self._apply_cache_blocks(payload["input"], resolved_key)
        # After every marker pass, never before -- see _lower_developer_items.
        self._lower_developer_items(payload["input"])
        if converted_tools:
            payload["tools"] = converted_tools
            payload["tool_choice"] = "auto"
            # None means "leave the field out" (declared per model): a backend
            # that does not know it refuses the request, and Gemini answers
            # worse when it is sent at all. bool(None) would have sent False.
            if self.parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = bool(self.parallel_tool_calls)
        return payload

    @property
    def _is_openrouter(self) -> bool:
        return "openrouter.ai" in self.base_url.lower()

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self._is_openrouter:
            # Opt-in, off by default at the gateway: without it the response
            # carries no `openrouter_metadata` and nothing says WHICH backend
            # answered. With provider_routing.order in play that is the one
            # thing worth knowing, and it costs a header.
            headers["X-OpenRouter-Metadata"] = "enabled"
        return headers

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
            # A reasoning item needs no extraction here: the verbatim block
            # below keeps it whole, and that is where the thinking is read
            # from (reasoning_artifacts.thinking_text). Copying its text onto
            # the message as well stored every thought twice.

        assistant: dict = {"role": "assistant", "content": "".join(content_parts)}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        if output:
            # Verbatim replay block — the whole point of this client. ALL
            # output items (reasoning + function_call + message) are stored so
            # the next request reproduces the exact item sequence.
            #
            # It is also the ONE home of this turn's thinking: a reasoning item
            # carries it in ``content[]`` (raw) or ``summary[]`` (condensed) —
            # measured over 2.345 stored items, ``summary`` was empty in ALL of
            # them while ``content[].text`` held up to 294.763 characters. The
            # block has to survive verbatim for the replay anyway, so nothing
            # is copied onto the message; readers use
            # ``reasoning_artifacts.thinking_text``, and the strip functions
            # rescue the text before this block is dropped.
            assistant["reasoning_details"] = [{
                "type": RESPONSES_ITEMS_TYPE,
                "format": RESPONSES_ITEMS_FORMAT,
                "index": 0,
                # Who produced these items. The encrypted payload only verifies
                # against this model, so the replay side checks it.
                "model": self.model,
                "items": output,
            }]
        # Which backend answered: the next request of this run goes back to
        # it (routing_pinned_to_last_backend).
        served_by = (openrouter_routing_info(response_data) or {}).get("selected")
        if served_by:
            assistant["served_by"] = served_by

        status = response_data.get("status")
        incomplete = response_data.get("incomplete_details")
        result: dict = {"assistant": assistant}
        # ``status`` alone decides, not incomplete_details: the SDK types the
        # reason as OPTIONAL (``Literal["max_output_tokens", "content_filter"]
        # | None``), and upstream is known to send the object empty. Keying on
        # the details would drop exactly those cases back into silence — the
        # status already says the answer is cut off.
        if status == "incomplete":
            reason = (incomplete or {}).get("reason")
            logger.warning(
                "Responses API returned incomplete response (%s), model=%s",
                reason or "reason absent", self.model,
            )
            # Hand the truncation to the caller, not just to the log. Without
            # this the agent server cannot tell a complete answer from one the
            # provider cut short: its content-filter guard and its truncation
            # guard both key on ``finish_reason``, and this client never set
            # it — so for every Responses model both guards were dead.
            #
            # No reason given -> "length", the conservative reading: it warns
            # and fails fast on an empty answer, but does NOT move the
            # run onto the fallback profile the way "content_filter" does.
            # Guessing the heavier reason would punish the wrong model.
            result["finish_reason"] = _INCOMPLETE_REASON_TO_FINISH.get(
                reason, reason or "length",
            )

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

    async def _notify_error(self, url: str, duration_ms: float, error_msg: str,
                            is_streaming: bool = False) -> None:
        """Post-response notification for terminal failures — keeps the
        message debugger seeing failed requests, like the sibling clients."""
        await self._notify_post_response({
            "provider": self._PROVIDER, "model": self.model, "url": url,
            "is_streaming": is_streaming, "duration_ms": duration_ms,
            "error": error_msg, "timestamp_ms": _time.time() * 1000,
        })

    async def _consume_stream(self, response: httpx.Response, cancellation_token):
        """Read one server-sent event stream; yield deltas, end with the body.

        Yields ``("delta", chunk)`` for every token the gateway sends and
        finally ``("body", response_data)`` — the object carried by whichever
        terminal event ended the run. THREE events end one: ``completed``,
        ``incomplete`` (the output cap was reached) and ``failed``. Each
        carries the SAME shape the non-streaming route returns as its body,
        ``openrouter_metadata`` and the output items with their encrypted
        reasoning included, so the caller can hand it to ``_format_response``
        and nothing downstream can tell which path ran.

        Measured against the live gateway 2026-09-12: a run capped at 16
        tokens ends with ``response.incomplete`` carrying ``status:
        "incomplete"``, ``incomplete_details.reason: "max_output_tokens"``,
        output, usage and metadata. Reading only ``completed`` would turn that
        into a missing body — and a deterministic cap would be regenerated
        until the retries ran out, with the truncation guard never seeing it.

        A stream that ends without any terminal event yields ``None`` as the
        body: the caller retries it like an unreadable body rather than
        inventing a result out of the deltas it happens to have seen.

        A stream that sends nothing but the gateway's keep-alive comments for
        longer than ``stream_silence_timeout`` raises ``httpx.ReadTimeout``,
        which the request loop retries. The gateway sends those comments about
        every half second, so the socket's own read timeout never fires: without
        the declared limit, an upstream that stopped answering holds its call
        for as long as the gateway keeps it open. A run whose terminal event
        has arrived is kept, however the stream ends after it.
        """
        accumulated = []
        body = None
        buffer = b""
        silence_limit = self.stream_silence_timeout
        last_event = _time.monotonic()
        # Bytes, not lines. httpx' line decoder splits like ``str.splitlines()``
        # — which also cuts at U+2028, U+2029 and U+0085. SSE ends a line at
        # CR/LF only, and JSON allows those characters RAW inside a string, so
        # one of them in the prose would slice an event in half and cost the
        # whole answer. The sibling client reads bytes for the same reason.
        # Splitting on b"\n" can never cut a multi-byte character in half, so
        # every line decodes on its own.
        try:
            async for raw in response.aiter_bytes():
                if cancellation_token and cancellation_token.is_cancelled:
                    raise asyncio.CancelledError("Request cancelled by user")
                buffer += raw
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    # Only ``data:`` lines carry events, and only they count as a
                    # sign of life; the gateway's keep-alive comments (": ") are
                    # dropped here.
                    if not line or not line.startswith("data:"):
                        continue
                    last_event = _time.monotonic()
                    data = line[5:].strip()
                    if data == "[DONE]":
                        yield "body", body
                        return
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        logger.debug("Responses stream: unparsable event %r", data[:200])
                        continue
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        delta = event.get("delta") or ""
                        accumulated.append(delta)
                        yield "delta", {"type": "content_delta", "delta": delta,
                                        "accumulated": "".join(accumulated)}
                    elif kind in ("response.reasoning_text.delta",
                                  "response.reasoning_summary_text.delta"):
                        # The thinking channel, under both its names: models that
                        # expose raw reasoning use the first (measured: five of
                        # them before the first answer token), while the OpenAI
                        # family only ever emits a SUMMARY of its thinking, under
                        # the second. The agent server forwards either as
                        # ``reasoning_delta`` — the only live view of a run's
                        # reasoning.
                        yield "delta", {"type": "thinking_delta",
                                        "delta": event.get("delta") or ""}
                    elif kind == "response.function_call_arguments.delta":
                        yield "delta", {
                            "type": "tool_call_delta",
                            "index": event.get("output_index", 0),
                            "delta": {"function": {"arguments": event.get("delta") or ""}},
                        }
                    elif kind in ("response.completed", "response.incomplete",
                                  "response.failed"):
                        body = event.get("response")
                    elif kind == "error" or (kind is None and event.get("error")):
                        # Upstream failure mid-stream: hand it over in the body
                        # shape the loop already knows how to read.
                        body = {"error": event.get("error") or event}
                if silence_limit and _time.monotonic() - last_event > silence_limit:
                    raise httpx.ReadTimeout(
                        f"no stream event for {silence_limit:g}s, only keep-alives")
        except httpx.TransportError as error:
            # Keep-alives only, nothing at all, or the connection dropped: a run that has ended is kept.
            if body is None:
                logger.warning("Responses stream: no terminal event (%r): %s", error, self.model)
                raise
            logger.warning("Responses stream: run ended, stream not closed (%r): %s", error, self.model)
        yield "body", body

    async def _request(self, messages: list, tools: Optional[list],
                       cancellation_token=None, status_scope=None) -> dict:
        """The non-streaming result: the shared loop, consumed to its end."""
        result: dict = {}
        async for chunk in self._request_events(
                messages, tools, cancellation_token, status_scope, stream=False):
            if chunk.get("type") == "final":
                result = {key: value for key, value in chunk.items() if key != "type"}
        return result

    async def _post(self, client: httpx.AsyncClient, url: str,
                    payload: dict) -> httpx.Response:
        """The transport seam — one POST, raw response back.

        Everything the loop below does (429 tier drop, reasoning-artifact
        healing, body-level errors, retries, hook notification) reads only
        ``status_code`` and the raw body, so a subclass can swap the way the
        request travels without owning a second copy of that logic.

        Transport failures must keep raising ``httpx.TimeoutException`` /
        ``httpx.TransportError``: the retry branch catches exactly those.
        """
        return await client.post(url, json=payload, headers=self._headers())

    def _stream(self, client: httpx.AsyncClient, url: str, payload: dict):
        """The streaming twin of ``_post`` — an async context manager.

        Its own seam on purpose: without it the streaming path would reach
        past every substitute installed for ``_post``. That is not theory —
        the first version of this client had no such seam, and a unit test
        that swapped the transport went out to the real gateway and came back
        with a 401.
        """
        return client.stream("POST", url, json=payload, headers=self._headers())

    async def _request_events(self, messages: list, tools: Optional[list],
                              cancellation_token=None, status_scope=None,
                              stream: bool = False):
        """The ONE request loop, as an event generator.

        With ``stream=True`` it yields ``content_delta`` / ``thinking_delta`` /
        ``tool_call_delta`` chunks while the answer arrives; either way it ends
        with exactly one ``final`` chunk carrying what ``_format_response``
        produced. Both paths share the retries, the flex-tier drop and the
        reasoning-artifact healing — a second copy of that logic is how the two
        routes would drift apart.
        """
        # Resolved once: the reasoning-artifact heals below rebuild the
        # payload, and the rebuilt request must keep the pin. OpenRouter only,
        # like the httpx route: another endpoint is never asked for the list.
        provider = (await routing_pinned_to_last_backend(
            self.provider_routing, messages, self.base_url,
            httpx_verify(self.ssl_verify))
            if self._is_openrouter else self.provider_routing)

        def build_payload() -> dict:
            payload = self._build_payload(messages, tools)
            if provider:
                payload["provider"] = provider
            return payload

        payload = build_payload()
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
        async with httpx.AsyncClient(timeout=timeout, verify=httpx_verify(self.ssl_verify)) as client:
            # while-loop with explicit increments: the one-shot
            # encrypted-reasoning heal must NOT consume a retry slot — with a
            # for-loop a heal on the final attempt would strip the session and
            # then never send the healed request.
            attempt = 0
            while attempt <= self.max_retries:
                if cancellation_token and cancellation_token.is_cancelled:
                    raise asyncio.CancelledError("Request cancelled by user")

                # What actually travels. The loop mutates ``payload`` (the tier
                # drop pops a key), so the stream flag is added per attempt
                # instead of being baked into the payload builder.
                sent_payload = {**payload, "stream": True} if stream else payload

                _request_start = _time.time()
                await self._notify_pre_request({
                    "provider": self._PROVIDER, "model": self.model, "url": url,
                    "is_streaming": stream, "payload": sent_payload,
                    "timestamp_ms": _request_start * 1000,
                })
                response_data = None
                body_text = ""
                streamed = False
                partial_text = ""
                try:
                    if stream:
                        async with self._stream(client, url, sent_payload) as response:
                            if (response.status_code >= 400 or "event-stream"
                                    not in response.headers.get("content-type", "")):
                                # Nothing to read event by event: either an
                                # error status, or the gateway answered the
                                # stream request with a plain JSON body — which
                                # is how it proxies upstream errors on
                                # /responses (see the body-error branch below).
                                # Both are small, so pull the body in before
                                # leaving the context and let the branches below
                                # read it exactly as on the non-streaming path.
                                await response.aread()
                                body_text = response.text or ""
                            else:
                                streamed = True
                                async for kind, item in self._consume_stream(
                                        response, cancellation_token):
                                    if kind == "delta":
                                        if item["type"] == "content_delta":
                                            partial_text = item["accumulated"]
                                        yield item
                                    else:
                                        response_data = item
                    else:
                        response = await self._post(client, url, payload)
                        body_text = response.text or ""
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    if attempt < self.max_retries:
                        backoff = self.retry_backoff * (2 ** attempt)
                        logger.warning(
                            f"Responses request transport error ({type(e).__name__}), "
                            f"retry {attempt + 1}/{self.max_retries} in {backoff:.0f}s: {self.model}"
                        )
                        await self._notify_retry(
                            self._PROVIDER, self.model, url, stream,
                            f"transport: {type(e).__name__}", attempt, self.max_retries + 1)
                        await self._cancellable_sleep(backoff, cancellation_token)
                        attempt += 1
                        continue
                    await self._notify_error(
                        url, (_time.time() - _request_start) * 1000,
                        f"transport exhausted: {type(e).__name__}: {e}", stream)
                    raise

                duration_ms = (_time.time() - _request_start) * 1000

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
                            self._PROVIDER, self.model, url, stream,
                            "429 flex->standard tier drop", attempt, self.max_retries + 1)
                        continue
                    retry_after = None
                    try:
                        retry_after = float(response.headers.get("retry-after", ""))
                    except (TypeError, ValueError):
                        pass
                    await self._notify_error(url, duration_ms, f"HTTP 429: {body_text[:200]}", stream)
                    if "quota" in body_text.lower() or "exhausted" in body_text.lower():
                        raise LLMQuotaExhaustedError(
                            f"Quota exhausted: {body_text[:200]}",
                            provider=self._PROVIDER, model=self.model, retry_after=retry_after)
                    raise LLMRateLimitError(
                        f"Rate limit exceeded: {body_text[:200]}",
                        provider=self._PROVIDER, model=self.model, retry_after=retry_after)

                if response.status_code >= 500:
                    if attempt < self.max_retries:
                        backoff = self.retry_backoff * (2 ** attempt)
                        logger.warning(
                            f"Responses request {response.status_code}, "
                            f"retry {attempt + 1}/{self.max_retries} in {backoff:.0f}s: {self.model}")
                        await self._notify_retry(
                            self._PROVIDER, self.model, url, stream,
                            f"HTTP {response.status_code}", attempt, self.max_retries + 1)
                        await self._cancellable_sleep(backoff, cancellation_token)
                        attempt += 1
                        continue
                    await self._notify_error(
                        url, duration_ms,
                        f"HTTP {response.status_code}: {body_text[:300]}", stream)
                    raise LLMServerError(
                        f"HTTP {response.status_code}: {body_text[:300]}",
                        provider=self._PROVIDER, model=self.model,
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
                        payload = build_payload()
                        if _tier_dropped:
                            payload.pop("service_tier", None)
                        logger.warning(
                            "Responses-%d retry: stripped reasoning artifacts from "
                            "%d message(s) (defective reasoning item). model=%s detail=%r",
                            response.status_code, n, self.model, body_text[:500])
                        await self._notify_retry(
                            self._PROVIDER, self.model, url, stream,
                            f"http-{response.status_code} reasoning-items strip",
                            attempt, self.max_retries + 1)
                        continue
                    error_msg = f"HTTP {response.status_code}: {body_text[:300]}"
                    logger.error(f"Responses request failed: {error_msg}")
                    await self._notify_error(url, duration_ms, error_msg, stream)
                    raise httpx.HTTPStatusError(error_msg, request=response.request, response=response)

                if response_data is None and streamed:
                    # The stream ended without ``response.completed``. Whatever
                    # deltas arrived are NOT a result: the items, the usage and
                    # the backend all live in that final object, so this is
                    # retried like an unreadable body rather than assembled
                    # from fragments.
                    if attempt < self.max_retries:
                        logger.warning(
                            "Responses stream ended without a completed event, "
                            "retry %d/%d: %s", attempt + 1, self.max_retries, self.model)
                        await self._notify_retry(
                            self._PROVIDER, self.model, url, stream,
                            "stream ended without completed event",
                            attempt, self.max_retries + 1)
                        await self._cancellable_sleep(self.retry_backoff, cancellation_token)
                        attempt += 1
                        continue
                    # Retries spent. Deliberately NOT an error: the agent
                    # server spells out why in its own incomplete_stream
                    # branch — an error here switches the fallback profile
                    # persistently and ends a run that has no fallback chain,
                    # far too heavy a hammer for a dropped connection. The
                    # sibling client hands over what arrived under exactly
                    # this flag; this one does the same, so that decision
                    # stays in one place instead of two.
                    logger.warning(
                        "Responses stream ended without a terminal event, "
                        "handing over %d char(s) as incomplete: %s",
                        len(partial_text), self.model)
                    await self._notify_post_response({
                        "provider": self._PROVIDER, "model": self.model, "url": url,
                        "is_streaming": True, "duration_ms": duration_ms,
                        "finish_reason": "incomplete_stream",
                        "timestamp_ms": _time.time() * 1000,
                    })
                    yield {"type": "final",
                           "assistant": {"role": "assistant", "content": partial_text},
                           "finish_reason": "incomplete_stream"}
                    return

                if response_data is None:
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
                            url, duration_ms, f"JSON decode failed (len={len(body_text)})", stream)
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
                                self._PROVIDER, self.model, url, stream,
                                "body-429 flex->standard tier drop",
                                attempt, self.max_retries + 1)
                            continue
                        backoff = self.retry_backoff * (2 ** attempt)
                        logger.warning(
                            f"Responses body rate-limit, retry {attempt + 1}/"
                            f"{self.max_retries} in {backoff:.0f}s: {self.model}")
                        await self._notify_retry(
                            self._PROVIDER, self.model, url, stream,
                            "body rate-limit", attempt, self.max_retries + 1)
                        await self._cancellable_sleep(backoff, cancellation_token)
                        attempt += 1
                        continue

                # 2) Defective encrypted reasoning item reported body-level.
                if body_err and not _enc_retried and \
                        self._is_reasoning_artifact_rejection(json.dumps(body_err, ensure_ascii=False)):
                    _enc_retried = True
                    n = strip_all_reasoning_artifacts(messages)
                    payload = build_payload()
                    if _tier_dropped:
                        payload.pop("service_tier", None)
                    logger.warning(
                        "Responses body-error retry: stripped reasoning artifacts "
                        "from %d message(s) (defective reasoning item). model=%s detail=%r",
                        n, self.model, str(body_err)[:500])
                    await self._notify_retry(
                        self._PROVIDER, self.model, url, stream,
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
                        self._PROVIDER, self.model, url, stream,
                        "body server-error", attempt, self.max_retries + 1)
                    await self._cancellable_sleep(backoff, cancellation_token)
                    attempt += 1
                    continue

                routing = openrouter_routing_info(response_data)
                if routing:
                    logger.debug("Routing %s: %s", self.model, routing)
                await self._notify_post_response({
                    "provider": self._PROVIDER, "model": self.model, "url": url,
                    "is_streaming": stream, "duration_ms": duration_ms,
                    "response_data": response_data,
                    # Own key, not just buried in response_data: the message
                    # debugger and any cost/routing audit read the flat
                    # fields, and "which backend served this call" was not
                    # answerable at all before.
                    "routing": routing,
                    # Chat-shaped usage: session_costs.py & co. read
                    # $.prompt_tokens/$.completion_tokens from the stored
                    # usage_json — the raw Responses shape would yield 0s.
                    "usage": self._map_usage(response_data.get("usage")),
                    "timestamp_ms": _time.time() * 1000,
                })
                yield {"type": "final", **self._format_response(response_data)}
                return

        raise LLMServerError(  # pragma: no cover — loop always returns/raises
            "Responses request retries exhausted",
            provider=self._PROVIDER, model=self.model, status_code=599)

    # ------------------------------------------------------------------
    # LLMClient interface
    # ------------------------------------------------------------------

    #: Whether this class reads the gateway's event stream itself. A subclass
    #: that swaps the transport seam (``_post``) does NOT inherit a working
    #: reader — the reader speaks to httpx directly, so streaming there would
    #: silently bypass that transport. Such a subclass sets this to False and
    #: keeps the non-streaming path.
    _STREAMS_SSE = True

    def supports_streaming(self) -> bool:
        """Streaming unless the model's capabilities disable it.

        Same rule as every sibling client (httpx, openai, ollama, gemini,
        anthropic): the configuration decides, not the class. Measured
        2026-09-12 against the gateway's ``/responses``: reasoning arrives as
        ``response.reasoning_text.delta`` events long before the first answer
        token — which is what makes watching a run's thinking possible.
        """
        if not self._STREAMS_SSE:
            return False
        if self.capabilities is not None:
            if isinstance(self.capabilities, dict):
                return self.capabilities.get("streaming", True)
            if hasattr(self.capabilities, "streaming"):
                return self.capabilities.streaming
        return True

    async def chat(self, messages: list[ChatMessage], cancellation_token=None, status_scope=None) -> str:
        result = await self._request(messages, None, cancellation_token, status_scope)
        return result.get("assistant", {}).get("content", "") or ""

    async def chat_tools(self, messages: list[ChatMessage], tools: list[dict],
                         cancellation_token=None, status_scope=None) -> dict:
        return await self._request(messages, tools, cancellation_token, status_scope)

    async def chat_tools_streaming(self, messages: list[ChatMessage], tools: list[dict],
                                   cancellation_token=None, status_scope=None):
        """Token deltas as they arrive — or one ``final`` chunk if this model
        does not stream.

        ``supports_streaming`` decides, so a model configured with
        ``capabilities.streaming: false``, and the SDK subclass whose transport
        the reader would bypass, keep the complete-result behaviour the agent
        server saw before. The final chunk is the same object either way: it
        carries the assistant, the usage and the ``finish_reason`` that the
        server's truncation and content-filter guards read.
        """
        async for chunk in self._request_events(
                messages, tools, cancellation_token, status_scope,
                stream=self.supports_streaming()):
            yield chunk

    async def close(self) -> None:  # per-request AsyncClient — nothing to close
        return None
