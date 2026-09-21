"""
HTTPX-based LLM client with superior cancellation, timeout, and error handling.

This client uses HTTPX directly for better async control compared to the official
OpenAI client which has known hanging/timeout issues.
"""

import asyncio
import codecs
import json
import logging
import random
import re
import socket
import time
from typing import Any, Callable, Optional
from dataclasses import dataclass

import httpx
import ssl

from agent_system.llm.tls import httpx_verify

from agent_system.llm.cache_key import (
    ANTHROPIC_MAX_CACHE_BLOCKS,
    CACHE_BP_SENTINEL,
    CACHE_MODE_TASK_SEQUENCE,
    MARKER_STYLE_ANTHROPIC,
    MARKER_STYLE_NONE,
    MARKER_STYLE_OPENAI,
    MAX_EXPLICIT_BREAKPOINTS,
    anthropic_cache_conversation,
    boundary_registry,
    cap_cache_control,
    derive_prompt_cache_key,
    mark_conversation_tail as _shared_mark_conversation_tail,
    mark_last_system as _shared_mark_last_system,
    mark_last_tool as _shared_mark_last_tool,
    mark_message_tail as _shared_mark_message_tail,
    messages_have_history,
    plan_cache_blocks,
    strip_cache_breakpoints,
)
from agent_system.llm.message_roles import (
    DEVELOPER, NOTE_CLOSE, NOTE_OPEN, SYSTEM, USER, as_note, conversation_opener,
    resolve_rung, rung_for_position,
)
from agent_system.llm.models import LLMClient, LLMRateLimitError, LLMQuotaExhaustedError, LLMServerError, LLMConnectionError
from agent_system.core.cancellation import CancellationToken
from plugins.llm_common import openai_utils
from plugins.llm_common.model_dialects import (
    ASSISTANT_REASONING_FIELDS,
    DIALECT_GEMINI_FUNCTION_DECLARATIONS,
    MARKER_STYLES,
    declared_choice,
    reasoning_replay_flags,
    resolve_reasoning_details_mode,
    resolve_tool_schema_dialect,
    tool_schema_sanitizer,
)
from agent_system.utils.reasoning_artifacts import (
    strip_all_reasoning_artifacts,
    strip_reasoning_artifacts_containing,
    thinking_text,
    thinking_text_from_details,
)

logger = logging.getLogger(__name__)


def openrouter_routing_info(response_data: dict) -> Optional[dict]:
    """Which backend actually answered — from ``openrouter_metadata``.

    The gateway only sends that block when the request carried
    ``X-OpenRouter-Metadata: enabled``; without the header there is nothing
    in the response that names the backend on the Responses route (the chat
    route also has a plain top-level ``provider``, used here as a fallback).

    Deliberately NOT a verdict on whether ``provider_routing.order`` held:
    the config names gateway SLUGS while the metadata reports DISPLAY names,
    and only the gateway's provider list translates between them (see
    routing_pinned_to_last_backend) — a naive comparison would cry wolf on
    exactly the entries that are pinned hardest. Reported, not judged.
    """
    meta = response_data.get("openrouter_metadata")
    if not isinstance(meta, dict):
        fallback = response_data.get("provider")
        return {"selected": fallback} if isinstance(fallback, str) else None
    endpoints = meta.get("endpoints") or {}
    available = [e for e in (endpoints.get("available") or []) if isinstance(e, dict)]
    # Names only: "selected" becomes ChatMessage.served_by, a str field, after
    # the model has answered -- a malformed record must not fail that turn.
    selected = [e.get("provider") for e in available
                if e.get("selected") and isinstance(e.get("provider"), str)]
    fallback = response_data.get("provider")
    info = {
        "selected": (selected[0] if selected
                     else fallback if isinstance(fallback, str) else None),
        "available": [e.get("provider") for e in available],
        "attempt": meta.get("attempt"),
        "strategy": meta.get("strategy"),
        "region": meta.get("region"),
    }
    # `or None`: ein Metadaten-Block, aus dem nichts Brauchbares
    # herausfaellt, darf kein leeres Dict melden — ein Feld, das immer
    # etwas enthaelt, ist von einem funktionierenden nicht zu unterscheiden.
    # `!= []` neben `is not None`: eine leere Anbieterliste ist keine
    # Information. `attempt: 0` waere eine — deshalb kein Falsy-Test.
    return {k: v for k, v in info.items()
            if v is not None and v != []} or None


# Display name -> routing slug, as the gateway publishes it (GET /providers).
# Process-wide on purpose: it is the gateway's catalogue, not run state.
_provider_slugs: dict[str, str] = {}
_provider_slugs_loaded_at = float("-inf")


async def _provider_slug(base_url: str, name: str, verify: Any) -> Optional[str]:
    """Routing slug for a backend display name, from OpenRouter's own list.

    Reloaded when a name is missing (a new backend, or a failed load), at most
    every ten minutes: a gateway without the list must not cost every request
    a timeout. Until it loads, nothing is pinned. ``verify`` is the calling
    client's TLS setting: behind an intercepting proxy the default would fail.
    """
    global _provider_slugs_loaded_at
    if name not in _provider_slugs and time.monotonic() - _provider_slugs_loaded_at > 600:
        try:
            async with httpx.AsyncClient(timeout=10, verify=verify) as client:
                response = await client.get(f"{base_url.rstrip('/')}/providers")
                response.raise_for_status()
                # Entry by entry: one malformed record must not cost the rest.
                _provider_slugs.update(
                    {p["name"]: p["slug"] for p in response.json()["data"]
                     if isinstance(p, dict) and isinstance(p.get("name"), str)
                     and isinstance(p.get("slug"), str)})
        except Exception as exc:
            logger.warning("OpenRouter provider list unavailable, no provider pin: %r", exc)
        # Stamped after the attempt, not before: requests arriving during a
        # load fetch too instead of going out unpinned, and a cancelled load
        # leaves no stamp that would block the retry for ten minutes.
        _provider_slugs_loaded_at = time.monotonic()
    return _provider_slugs.get(name)


async def routing_pinned_to_last_backend(provider_routing: Optional[dict],
                                         messages: list,
                                         base_url: str,
                                         verify: Any) -> Optional[dict]:
    """provider_routing with the backend of the latest recorded turn in front.

    That backend holds the run's prompt cache, and it is the only one that can
    verify the encrypted reasoning the next request replays. Measured
    2026-09-11 with two backends configured for one model: the first refused
    102 of 102 histories whose last turn the second had served and accepted
    135 of 135 of its own -- every later turn of such a run paid a 400 and
    lost the cache.

    Reordered only, never narrowed or widened: the other configured backends
    stay behind it, so an outage of the pinned one still falls through to
    them, and a backend the configuration does not list is not added. Derived
    from the history (``served_by`` on the assistant messages) on every
    request and never kept on a client -- one client serves every parallel
    session of an agent type.

    The metadata names the backend by display name, ``order`` takes slugs; the
    gateway's provider list translates. A configured entry matches by its
    slug before the first "/" and keeps its suffix (``<slug>/fp8``).
    """
    order = provider_routing.get("order") if isinstance(provider_routing, dict) else None
    if not isinstance(order, list):
        return provider_routing
    served = None
    for msg in reversed(messages):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role != "assistant":
            continue
        value = msg.get("served_by") if isinstance(msg, dict) else getattr(msg, "served_by", None)
        if isinstance(value, str) and value:
            served = value
            break
    if served is None:
        return provider_routing
    slug = await _provider_slug(base_url, served, verify)
    front = [entry for entry in order
             if isinstance(entry, str) and entry.split("/")[0] == slug]
    if not front or order[:len(front)] == front:
        return provider_routing
    return {**provider_routing,
            "order": front + [entry for entry in order if entry not in front]}


@dataclass
class HTTPXTimeoutConfig:
    """Fine-grained timeout configuration for HTTPX client."""
    connect: float = 30.0      # Connection establishment timeout (incl. TLS handshake)
    read: float = 180.0        # Read timeout (waiting for response data)
    write: float = 10.0        # Write timeout (sending request data)
    pool: float = 5.0          # Pool timeout (getting connection from pool)


class HTTPXOpenAIClient(LLMClient):
    """
    HTTPX-based OpenAI API client with superior async handling.

    Advantages over official OpenAI client:
    - Native asyncio.CancelledError support (no polling required)
    - Fine-grained timeout control (connect, read, write, pool)
    - Direct HTTP error handling without exception wrapping
    - Better connection management and retry logic
    - Cleaner cancellation without complex task management
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_config: Optional[HTTPXTimeoutConfig] = None,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        rate_limit_backoff: float = 60.0,
        rate_limit_max_retries: int = 2,
        verify: Optional[bool] = None,
        context_window: Optional[int] = None,
        capabilities: Optional[dict] = None,
        parallel_tool_calls: Optional[bool] = True,
        max_tokens: Optional[int] = None,
        safety_settings: Optional[dict[str, str]] = None,
        **extra_params
    ):
        # LLMClient doesn't have __init__, so no super() call needed
        self.model = model
        self.provider = "openai"
        self.context_window = context_window
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_config = timeout_config or HTTPXTimeoutConfig()
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.rate_limit_backoff = rate_limit_backoff
        self.rate_limit_max_retries = rate_limit_max_retries
        self.verify = verify
        self.parallel_tool_calls = parallel_tool_calls
        self.max_tokens = max_tokens  # Limit output tokens (None = provider default)
        self.extra_params = extra_params

        # Thinking/reasoning params — pop from extra_params to avoid raw injection
        self.thinking_level: str | None = self.extra_params.pop("thinking_level", None)
        self.thinking_budget: int | None = self.extra_params.pop("thinking_budget", None)

        # Sampling-Temperatur. None = Provider-Default (bei DeepSeek/OpenAI
        # ~1,0) — bis 2026-07 war der Param framework-seitig überhaupt nicht
        # setzbar, mechanische Aufgaben liefen also mit voller Varianz. Wie
        # service_tier bewusst aus extra_params gepopt: explizites Feld im
        # Payload statt opaker Passthrough.
        self.temperature: float | None = self.extra_params.pop("temperature", None)

        # Service tier (Google Flex etc.) — set via config, injected as a
        # top-level field in the chat-completions payload. Common values:
        #   - "flex":     Google Flex Processing (cheaper, slower)
        #   - "standard": default speed/billing (often equivalent to omitting it)
        #   - "priority": fast queue (where supported, usually more expensive)
        # Popped out of extra_params so the field gets explicit per-call
        # handling (and a clear log surface) instead of opaque passthrough.
        self.service_tier: str | None = self.extra_params.pop("service_tier", None)

        # OpenAI GPT-5.6+ Cache-Routing-Key: ohne ihn matcht der Prompt-Cache
        # ab der 5.6-Familie praktisch nie (Doku: "you must set
        # prompt_cache_key"). Config-diszipliniert nur auf OpenAI-Profilen
        # setzen — Fremd-Provider koennten den Param ablehnen.
        self.prompt_cache_key: str | None = self.extra_params.pop("prompt_cache_key", None)

        # Cache-Verhalten (Agent, via llm_params "*") + Marker-Stil (Modell) —
        # docs/prompt_cache_design.md §3/§4.
        self.prompt_cache_mode: str | None = self.extra_params.pop("prompt_cache_mode", None)
        self.prompt_cache_marker_style: str | None = declared_choice(
            self.extra_params.pop("prompt_cache_marker_style", None),
            default=None, allowed=MARKER_STYLES,
            field="prompt_cache_marker_style", model=model)

        # Which tool schema the endpoint accepts. "json_schema" (default) sends
        # the schema as it is; "gemini_function_declarations" strips what
        # Gemini's function declarations reject. Declared per model — the same
        # family is reachable through gateways that translate and gateways that
        # do not, so the model name says nothing about it.
        self.tool_schema_dialect: str = resolve_tool_schema_dialect(
            self.extra_params.pop("tool_schema_dialect", None), model=model)

        # What an assistant message carries back as its thinking, next to the
        # standard reasoning_details. "omit" (default) sends the field to no
        # one — it is not part of the OpenAI schema. "reasoning_content" fills
        # it on EVERY assistant message, empty string included: DeepSeek's
        # thinking mode answers 400 without it on a tool round trip.
        # See: https://api-docs.deepseek.com/guides/thinking_mode#tool-call
        self.assistant_reasoning_field: str = declared_choice(
            self.extra_params.pop("assistant_reasoning_field", None),
            default="omit", allowed=ASSISTANT_REASONING_FIELDS,
            field="assistant_reasoning_field", model=model)

        # Provider routing (OpenRouter) — soft preference over the available backends.
        # Example: {"order": ["google-vertex", "google-ai-studio"], "allow_fallbacks": true}
        # Pinning to a sticky backend keeps OpenRouter's implicit prompt cache
        # warm (cache is backend-local; cross-backend load-balancing breaks it).
        # Popped from extra_params and injected as top-level "provider" field below.
        self.provider_routing: dict | None = self.extra_params.pop("provider_routing", None)

        # Gateway request plugins (context-compression, response-healing,
        # moderation, ...) and the request-level cache controls. Both are
        # passed through verbatim and default to unset — see the config model
        # for why none of them is on by default.
        self.plugins: list | None = self.extra_params.pop("plugins", None)
        self.prompt_cache_options: dict | None = self.extra_params.pop(
            "prompt_cache_options", None)

        # How to round-trip provider-side reasoning blocks (reasoning_details)
        # across turns. Provider-specific requirement, set per-model in config —
        # NOT inferred from the model name. Values:
        #   "keep_last" (default): keep reasoning_details only on the most recent
        #       assistant message, strip it from older ones. Correct for Gemini —
        #       thought signatures are validated for the CURRENT turn only, and
        #       accumulating stale encrypted blocks raises the 400 surface + cost.
        #   "keep_all": keep reasoning_details on EVERY assistant message. Required
        #       for OpenAI reasoning models (gpt-5.x, o-series): their encrypted
        #       reasoning items form a chain — the latest item is verified against
        #       the prior ones, so dropping earlier items yields HTTP 400
        #       "encrypted content for item rs_… could not be verified". Keeping
        #       the chain also avoids re-reasoning (cheaper: cached input vs new
        #       output reasoning tokens).
        #   "strip": drop reasoning_details entirely (providers that don't accept
        #       it back).
        self.reasoning_details_mode: str = resolve_reasoning_details_mode(
            self.extra_params.pop("reasoning_details_mode", None),
            model=model, default="keep_last")
        # Seconds a stream may send only keep-alive comments before the attempt
        # is retried; None leaves such a stream to the endpoint (see the model key).
        self.stream_silence_timeout: Optional[float] = self.extra_params.pop("stream_silence_timeout", None)

        # Content-filter thresholds, sent whenever they are configured — a
        # model whose backend does not know the field must not carry it.
        self.safety_settings = safety_settings

        self.capabilities = capabilities or {}
        self._verify: ssl.SSLContext | bool | None = None  # Normalized verify value
        
        # Gates the gateway-only parts of a request: provider_routing, the
        # app-title header and the metadata header. Derived from the declared
        # base_url — the gateway extras really do hang off the endpoint.
        self._is_openrouter = "openrouter.ai" in base_url.lower()
        if self.plugins and not self._is_openrouter:
            # Konfiguriert und trotzdem nicht gesendet ist genau die stille
            # Drift, die dieses Feld sichtbar machen soll.
            logger.warning(
                "plugins are configured for model=%s but its endpoint is not "
                "OpenRouter (%s) — they are NOT sent.", model, base_url)
        
        # Validate API type - HTTPX client only supports chat_completions
        if self.capabilities and hasattr(self.capabilities, 'default_api_type'):
            api_type = self.capabilities.default_api_type
            # Extract value from enum if it's an enum
            if hasattr(api_type, 'value'):
                api_type = api_type.value
            else:
                api_type = str(api_type) if api_type else 'chat_completions'

            if api_type != 'chat_completions':
                raise NotImplementedError(
                    f"HTTPX client only supports 'chat_completions' API. "
                    f"Requested API type: '{api_type}'. "
                    f"For Realtime API, use provider='openai' instead of 'openai_httpx'. "
                    f"Current model: {self.model}"
                )

        # One shared SSLContext (verifying or not) instead of letting httpx
        # build a fresh one per AsyncClient -- this client opens a new
        # AsyncClient per REQUEST, so that was 160 ms on every LLM call.
        self._verify = httpx_verify(self.verify)

        logger.debug(f"HTTPXOpenAIClient initialized model={model} base_url={base_url} verify={self._verify}")

        # Create timeout object for HTTPX
        self._timeout = httpx.Timeout(
            connect=self.timeout_config.connect,
            read=self.timeout_config.read,
            write=self.timeout_config.write,
            pool=self.timeout_config.pool
        )

        # HTTPX client will be created per request to ensure proper cleanup
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "ScarabHive-HTTPX/1.0"
        }

        # OpenRouter app attribution headers
        # See https://openrouter.ai/docs/api-reference/overview#headers
        # OpenRouter creates a unique app_id per (API-key, HTTP-Referer) pair.
        # We use set_app_title() to append the agent name to the referer,
        # giving each agent its own entry in the OpenRouter dashboard.
        self._openrouter_base_referer = "https://github.com/eehrich/ScarabHive"
        if self._is_openrouter:
            self._headers["HTTP-Referer"] = self._openrouter_base_referer
            self._headers["X-Title"] = "ScarabHive"
            # Opt-in, off by default at the gateway: without it no response
            # says WHICH backend answered. With provider_routing.order in
            # play that is the one thing worth knowing, and it costs a header.
            self._headers["X-OpenRouter-Metadata"] = "enabled"

    def _apply_gateway_extras(self, payload: dict,
                              resolved_cache_key: str | None) -> None:
        """Fields both request paths send identically — written once.

        Streaming and non-streaming build their payloads separately in this
        file, and every field added to only one of them has drifted since.

        ``session_id`` is the gateway's sticky-routing key: OpenRouter's
        prompt cache is backend-local, so calls sharing a prefix only hit it
        while they share a backend. The resolved cache key IS that grouping,
        so it doubles as the session. OpenRouter-only — a plain OpenAI
        endpoint rejects unknown parameters.
        """
        if resolved_cache_key:
            # GPT-5.6+ Cache-Routing-Key (s. __init__); "auto" = Praefix-Hash,
            # kollisionsfrei bei parallelen Buechern (s. cache_key.py).
            payload["prompt_cache_key"] = resolved_cache_key
            if self._is_openrouter:
                payload["session_id"] = resolved_cache_key
        if self.plugins and self._is_openrouter:
            payload["plugins"] = self.plugins
        if self.prompt_cache_options:
            payload["prompt_cache_options"] = self.prompt_cache_options

    def _apply_tool_fields(self, payload: dict, tools: Optional[list],
                           message_dicts: list) -> None:
        """Everything a request's tools decide — written once for both paths.

        Streaming and non-streaming build their payloads separately in this
        file, and every field added to only one of them has drifted since.

        All three decisions come from declared per-model keys:
        ``tool_schema_dialect`` picks the schema the endpoint accepts,
        ``prompt_cache_marker_style == "anthropic"`` adds the tool cache marker
        and enforces Anthropic's hard 4-block limit, and
        ``parallel_tool_calls`` is sent as configured — *None* means the field
        stays out of the request, because a backend that does not know it
        refuses it while a backend that does defaults to true.
        """
        if not tools:
            return
        sanitize = tool_schema_sanitizer(self.tool_schema_dialect)
        if sanitize is not None:
            payload["tools"] = self._sanitize_tools_for_dialect(tools, sanitize)
            logger.info(
                "Sanitized %d tool schemas for dialect %s "
                "(model=%s, stream=%s)", len(tools), self.tool_schema_dialect,
                self.model, payload.get("stream"))
        else:
            payload["tools"] = tools
        if self._marker_style() == MARKER_STYLE_ANTHROPIC:
            self._apply_anthropic_tool_cache_control(payload["tools"])
            # Enforce Anthropic's hard 4-block cache_control limit across
            # system + conversation-tail + tools + any sentinels.
            self._cap_anthropic_cache_control(message_dicts, payload["tools"])
        payload["tool_choice"] = "auto"
        # Send the configured value explicitly: omitting it means the provider
        # default applies (OpenAI: true), so a configured False MUST be sent.
        # parallel_tool_calls=false is required for OpenAI reasoning models —
        # a turn with multiple parallel tool_calls loses the fc_* item ids in
        # Chat-Completions format, the Responses backend can't reconstruct the
        # item sequence, and the turn's encrypted reasoning item fails
        # verification (HTTP 400 "encrypted content ... could not be verified").
        if self.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = bool(self.parallel_tool_calls)

    def _marker_style(self) -> str:
        """The model's declared cache-marker style — one resolution, all sites.

        Unset falls back to the OpenAI style exactly when a prompt_cache_key is
        configured (config discipline: the key only sits on GPT entries), and
        to "none" otherwise. A model that speaks cache_control says so in its
        config; nothing here reads the model name.
        """
        if self.prompt_cache_marker_style is not None:
            return self.prompt_cache_marker_style
        return MARKER_STYLE_OPENAI if self.prompt_cache_key else MARKER_STYLE_NONE

    def _apply_cache_breakpoints(
        self, message_dicts: list, resolved_key: str | None = None,
    ) -> None:
        """Cache-Breakpoint-Sentinels in Message-Contents verarbeiten (in place).

        Marker-Stil kommt aus der Modell-Config (prompt_cache_marker_style):
        "openai" -> prompt_cache_breakpoint-Parts (GPT-5.6+), "anthropic" ->
        cache_control-Parts, "none"/ohne Key -> Sentinel rueckstandsfrei
        strippen (deepseek & Co. kennen weder Marker noch Feld).
        Bei prompt_cache_mode=task_sequence ergaenzt die Segment-Leiter den
        BP1-Read-Anker aus der Prozess-Registry (s. cache_key.py).
        """
        style = self._marker_style()
        mode = self.prompt_cache_mode
        # Anthropic: hartes 4-Marker-Limit (System-/Tool-Marker belegen schon
        # 2 Slots) und nativer Prefix-Match -> KEINE kumulative Leiter,
        # nur deklarierte Grenzen mit knappem Budget.
        if style == MARKER_STYLE_ANTHROPIC:
            # Request-weites Budget: hartes Anthropic-Limit von 4 Markern,
            # System- + Tool-Marker belegen bereits 2 Slots.
            marker_budget = 2
            if mode == "task_sequence":
                mode = None
        else:
            marker_budget = None  # OpenAI: kein hartes Marker-Limit
        ladder_used = False
        for msg in message_dicts:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            # Listen-Content (z.B. System-Message nach _apply_anthropic_
            # cache_control, Multimodal-Parts): Sentinels in text-Parts
            # strippen — Marker-Platzierung passiert nur auf String-Content.
            if isinstance(content, list):
                for part in content:
                    if (isinstance(part, dict)
                            and isinstance(part.get("text"), str)
                            and CACHE_BP_SENTINEL in part["text"]):
                        part["text"] = strip_cache_breakpoints(part["text"])
                continue
            if not isinstance(content, str) or CACHE_BP_SENTINEL not in content:
                continue
            # Leiter nur fuer die ERSTE Sentinel-Message pro Request — zwei
            # Messages wuerden sonst denselben Registry-Key thrashen.
            msg_mode = mode
            if mode == CACHE_MODE_TASK_SEQUENCE:
                if ladder_used:
                    msg_mode = None
                ladder_used = True
            if style == MARKER_STYLE_NONE or (
                style == MARKER_STYLE_OPENAI and not self.prompt_cache_key
            ):
                # Kein Marker-Feld bzw. OpenAI-Stil ohne Key (5.6 braucht den
                # Key zum Matching) -> Sentinel rueckstandsfrei strippen.
                msg["content"] = strip_cache_breakpoints(content)
                continue
            if marker_budget is not None and marker_budget <= 0:
                # Anthropic-Budget aufgebraucht: weitere Sentinel-Messages
                # nur noch strippen (hartes 4-Marker-Limit, sonst HTTP 400).
                msg["content"] = strip_cache_breakpoints(content)
                continue
            plan = plan_cache_blocks(
                content, mode=msg_mode, key=resolved_key or self.prompt_cache_key,
                max_markers=(marker_budget if marker_budget is not None
                             else MAX_EXPLICIT_BREAKPOINTS),
                registry=boundary_registry,
            )
            if plan is None:
                continue
            parts = []
            for block, marked in plan:
                part: dict = {"type": "text", "text": block}
                if marked:
                    if style == MARKER_STYLE_ANTHROPIC:
                        part["cache_control"] = {"type": "ephemeral"}
                    else:
                        part["prompt_cache_breakpoint"] = {"mode": "explicit"}
                parts.append(part)
            if marker_budget is not None:
                marker_budget -= sum(1 for _, m in plan if m)
            msg["content"] = parts

    def set_app_title(self, title: str) -> None:
        """Set per-agent OpenRouter app identity.

        OpenRouter keys apps by (API-key, HTTP-Referer). By appending the
        agent name to the referer URL each agent gets its own row in the
        OpenRouter activity dashboard.  X-Title is set to match so the
        dashboard shows a human-readable name.
        """
        if self._is_openrouter and title:
            self._headers["HTTP-Referer"] = f"{self._openrouter_base_referer}/{title}"
            self._headers["X-Title"] = title

    def _get_keepalive_socket_options(self) -> list:
        """Get TCP keep-alive socket options for the current platform.
        
        This prevents connection drops during long "thinking" pauses (e.g., DeepSeek reasoning).
        Especially important on Linux servers where firewalls/proxies may close idle connections.
        """
        options = [
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),  # Enable keep-alive
        ]
        # Linux-specific: set keep-alive timing (not available on all platforms)
        if hasattr(socket, 'TCP_KEEPIDLE'):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15))  # Start after 15s idle
        if hasattr(socket, 'TCP_KEEPINTVL'):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15))  # Probe every 15s
        if hasattr(socket, 'TCP_KEEPCNT'):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5))  # 5 probes before giving up
        return options

    def _create_multimodal_injection(self, tool_msg) -> Optional[dict]:
        """Create injected user message for multimodal tool content.
        
        Delegates to the central utility function in multimodal_tool_content.py.
        """
        from agent_system.utils.multimodal_tool_content import create_multimodal_injection, check_vision_support
        
        # Check if model supports audio input
        supports_audio = False
        if self.capabilities:
            supports_audio = getattr(self.capabilities, 'audio_input', False)
        
        return create_multimodal_injection(
            tool_msg=tool_msg,
            supports_vision=check_vision_support(self.capabilities),
            model_name=self.model,
            supports_audio=supports_audio
        )

    # Fields accepted by the OpenAI Chat Completions API
    # Note: reasoning_content is NOT universally accepted — it's a DeepSeek extension.
    # It's handled separately in _postprocess_messages_for_provider().
    # reasoning_details is OpenRouter's pass-through of provider-side thinking blocks
    # (e.g. Gemini 3.x thought_signature). Must round-trip to upstream or Gemini 3.x
    # rejects subsequent turns with MALFORMED_FUNCTION_CALL (verified 2026-05-26).
    # rd_orphaned is an INTERNAL marker (utils/reasoning_artifacts.py): history
    # mutation removed this message's reasoning-chain predecessors. It must
    # survive _sanitize_message_for_api so _postprocess_messages_for_provider
    # (which runs after serialization in both request paths) can honor it —
    # postprocess unconditionally pops it before the payload is built, so it
    # never reaches a provider. injected_by likewise: _apply_developer_rung tells
    # a wake from a note by it (message_roles.conversation_opener), then pops it.
    _API_MESSAGE_FIELDS = {"role", "content", "name", "tool_call_id", "tool_calls", "reasoning_details",
                           "rd_orphaned", "injected_by"}

    @staticmethod
    def _sanitize_tool_calls(tool_calls: list) -> list:
        """Strip non-standard fields from tool_calls.
        
        OpenRouter/Gemini may include extra fields like 'index' from the
        streaming delta format in non-streaming responses. When these
        contaminated tool_calls are re-sent to Gemini, they cause
        MALFORMED_FUNCTION_CALL errors.
        
        Standard OpenAI tool_call fields: id, type, function
        """
        sanitized = []
        for tc in tool_calls:
            clean = {
                "id": tc.get("id", ""),
                "type": tc.get("type", "function"),
                "function": tc.get("function", {})
            }
            # Preserve extra_content if present (Gemini thought_signature for round-trip)
            if "extra_content" in tc:
                clean["extra_content"] = tc["extra_content"]
            sanitized.append(clean)
        return sanitized

    @staticmethod
    def _sanitize_tools_for_dialect(tools: list, sanitize: Callable[[dict], dict]) -> list:
        """Rewrite every tool's parameter schema with the dialect's sanitiser.

        Which sanitiser that is comes from the model's declared
        ``tool_schema_dialect`` (llm_common.model_dialects) — this route never
        asks which family it is talking to. The endpoint behind an
        OpenAI-shaped API may accept far less than JSON Schema: Gemini's
        Function Declarations reject additionalProperties, default, format,
        title, oneOf, anyOf and answer MALFORMED_FUNCTION_CALL for a schema
        that keeps them.

        A tool without parameters keeps the field OUT of the request — the
        chat route has always sent it that way.
        """
        sanitized = []
        for tool in tools:
            if tool.get("type") != "function" or "function" not in tool:
                sanitized.append(tool)
                continue
            func = tool["function"]
            clean_tool = {
                "type": "function",
                "function": {
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                }
            }
            params = func.get("parameters", {})
            if params:
                clean_tool["function"]["parameters"] = sanitize(params)
            sanitized.append(clean_tool)
        return sanitized

    @classmethod
    def _sanitize_message_for_api(cls, d: dict) -> dict:
        """Keep only fields accepted by the OpenAI Chat Completions API.
        
        Strips internal BookKeeping fields like estimated_tokens, timestamp,
        content_format, etc. that would cause errors with strict providers
        like Gemini via OpenRouter.
        """
        clean = {k: v for k, v in d.items() if k in cls._API_MESSAGE_FIELDS}
        # Sanitize tool_calls sub-structures too
        if 'tool_calls' in clean and clean['tool_calls']:
            clean['tool_calls'] = cls._sanitize_tool_calls(clean['tool_calls'])
        # Preserve reasoning_content if present (handled by _postprocess_messages_for_provider)
        if 'reasoning_content' in d:
            clean['reasoning_content'] = d['reasoning_content']
        return clean

    # Anthropic-Cache-Policy lebt zentral in cache_key.py (Single-Source-of-Truth
    # fuer alle Claude-Pfade). Diese Methoden sind duenne, format-adaptierende
    # Delegatoren: sie reichen die OpenAI-Chat-Completions-Dicts dieses Clients an
    # die geteilten Helfer weiter. Anthropic via OpenRouter tunnelt Claude durch
    # die OpenAI-kompatible API — deshalb MUSS die cache_control-Injektion hier im
    # OpenAI-Payload passieren; die ENTSCHEIDUNG (was/wie oft) liegt aber geteilt.

    #: Rueckwaerts-kompatibles Alias auf das geteilte harte API-Limit.
    ANTHROPIC_MAX_CACHE_BLOCKS = ANTHROPIC_MAX_CACHE_BLOCKS

    def _apply_anthropic_cache_control(self, message_dicts: list) -> None:
        """Mark the last system message for Anthropic prompt caching (delegates to
        the shared policy). Modifies message_dicts in-place."""
        _shared_mark_last_system(message_dicts)

    def _apply_anthropic_tool_cache_control(self, tools: list) -> None:
        """Mark the last tool definition (delegates). Modifies tools in-place."""
        _shared_mark_last_tool(tools)

    @staticmethod
    def _mark_last_text_block(msg: dict) -> bool:
        """Delegate: put cache_control on a message's last text block."""
        return _shared_mark_message_tail(msg)

    def _apply_anthropic_conversation_cache_control(self, message_dicts: list) -> None:
        """Mark the growing conversation tail for multi-turn caching (delegates).
        Gated by the caller on ``prompt_cache_mode``."""
        _shared_mark_conversation_tail(message_dicts)

    @classmethod
    def _cap_anthropic_cache_control(cls, message_dicts: list, tools: Optional[list]) -> None:
        """Delegate: keep at most ANTHROPIC_MAX_CACHE_BLOCKS cache_control blocks
        across tools + messages (Anthropic prefix order), in-place."""
        cap_cache_control([tools, message_dicts])

    def _postprocess_messages_for_provider(self, message_dicts: list) -> None:
        """Post-process serialized messages for the model's declared dialect.

        ``assistant_reasoning_field == "reasoning_content"`` fills that field on
        ALL assistant messages (empty string included), otherwise it is
        stripped — it is not part of the OpenAI Chat Completions schema.

        ``prompt_cache_marker_style == "anthropic"``: inject cache_control on
        system messages (and the conversation tail) for prompt caching.

        Modifies message_dicts in-place.
        """
        if self._marker_style() == MARKER_STYLE_ANTHROPIC:
            self._apply_anthropic_cache_control(message_dicts)
            # Conversation-tail caching for multi-turn agents — shared policy
            # (multi_turn seeds from turn 1; auto/None only once real history
            # exists; task_sequence/one_shot/off opt out).
            if anthropic_cache_conversation(
                self.prompt_cache_mode, messages_have_history(message_dicts)
            ):
                self._apply_anthropic_conversation_cache_control(message_dicts)

        if self.assistant_reasoning_field == "reasoning_content":
            # Ensure ALL assistant messages carry the field (DeepSeek & co.).
            # With tools in the request the REAL text has to come back, not
            # just the key — an empty string satisfies the schema while losing
            # the chain of thought, and the model then re-derives it on every
            # turn. Since the text is stored once, it may sit in the artifacts
            # instead of on the message, so ask the shared reader first; "" is
            # only the last resort that keeps the field present.
            for msg in message_dicts:
                if msg.get("role") == "assistant" and not msg.get("reasoning_content"):
                    msg["reasoning_content"] = thinking_text(msg)
        else:
            # "omit": strip reasoning_content (non-standard field)
            for msg in message_dicts:
                msg.pop("reasoning_content", None)

        # reasoning_details round-trip is governed by the per-model config field
        # `reasoning_details_mode` (see __init__) — NOT by model-name detection.
        #
        #   keep_all  → keep on every assistant message. OpenAI reasoning models
        #               need the full encrypted chain (latest item verified
        #               against prior ones); also avoids re-reasoning.
        #   strip     → drop entirely.
        #   keep_last → (default) keep only on the most recent assistant message.
        #               Correct for Gemini: thought signatures are validated for
        #               the CURRENT turn only; accumulating stale encrypted blocks
        #               raises the 400 surface + cost. NOT a standalone fix for
        #               "Corrupted thought signature" 400s (a known Gemini 3.x
        #               parallel-call bug — see the body-400 signature-bypass
        #               retry in the request loop).
        #
        # History-mutation interplay (rd_orphaned, set by
        # utils/reasoning_artifacts.invalidate_reasoning_artifacts when
        # compaction/summarization rewrote history and removed a message's
        # chain predecessors):
        #   keep_all  → an orphaned message's reasoning_details are stripped:
        #               its chain is broken beyond repair, and sending a
        #               partial chain is exactly the "encrypted content could
        #               not be verified" 400. Items generated on LATER turns
        #               (after the reset request ran clean) form a fresh chain
        #               and are kept. EXCEPT while the flagged turn still has
        #               tool_calls: invalidate_reasoning_artifacts flags the
        #               MOST RECENT assistant message, which at a pre_llm_call
        #               hook is the OPEN tool-use turn — and Anthropic wants
        #               that turn's thinking echoed back complete, so dropping
        #               it is the 400 the strip was meant to avoid. The chain
        #               must travel exactly here.
        #   keep_last → flag ignored: the latest signature is still required
        #               for the open Gemini tool round-trip; older ones are
        #               stripped here anyway. (Contract of
        #               utils/reasoning_artifacts, which sets the flag.)
        # Which messages may replay at all is the shared policy, used by all
        # three routes — the flag is the only thing this route adds on top.
        # The flag is ALWAYS popped below — it never reaches a provider.
        mode = self.reasoning_details_mode
        may_replay = reasoning_replay_flags(message_dicts, mode)
        try:
            for msg, replay in zip(message_dicts, may_replay):
                if msg.get("role") != "assistant":
                    continue
                orphaned = (mode == "keep_all" and msg.get("rd_orphaned")
                            and not msg.get("tool_calls"))
                if not replay or orphaned:
                    msg.pop("reasoning_details", None)
        finally:
            for msg in message_dicts:
                msg.pop("rd_orphaned", None)

        # LAST, deliberately: a lowered developer note becomes a `system`
        # message here, and mark_last_system above picks system messages. A
        # note that changes every turn must not carry the cache breakpoint, so
        # the markers have to run while it still says `developer`. (The tail
        # marker skips developer notes by itself -- cache_key.py -- because it
        # takes the last message whatever its role.)
        self._apply_developer_rung(message_dicts)

    def _apply_developer_rung(self, message_dicts: list) -> None:
        """Rewrite developer messages to the rung this endpoint takes.

        The wire format has the role -- "with o1 models and newer, `developer`
        messages replace the previous `system` messages" -- but this client
        points at whatever ``base_url`` says. OpenRouter documents only
        user/assistant/system/tool, and an OpenAI-compatible host may be
        llama.cpp, whose chat template renders a role it does not know as
        nothing at all: the note would vanish with a 200 and no log line. So
        `system` is the default and the model entry opts up.
        """
        # capabilities is a ModelCapabilitiesConfig, or the empty dict __init__
        # substitutes for None -- getattr answers both.
        declared = getattr(self.capabilities, "developer_role", None)
        rung = resolve_rung(declared, ceiling=DEVELOPER, default=SYSTEM,
                            route=f"the endpoint at {self.base_url}")
        # No early return on the developer rung any more: the LAST developer
        # message rides the user rung whatever the route allows, because a
        # request ending on something that demands no answer gets none. The
        # rule and its measurement live in message_roles.rung_for_position --
        # this route learned it an hour after the Responses route, from a
        # review, and that hour is the argument for it living in one place.
        last = message_dicts[-1] if message_dicts else None
        opener = conversation_opener(message_dicts)
        for msg in message_dicts:
            msg.pop("injected_by", None)  # read by conversation_opener, never sent
            if msg.get("role") != DEVELOPER:
                continue
            content = msg.get("content")
            target = rung_for_position(rung, last=msg is last, opens=msg is opener)
            if target != USER:
                msg["role"] = target
                continue
            msg["role"] = USER
            if isinstance(content, list):
                msg["content"] = [{"type": "text", "text": NOTE_OPEN}, *content,
                                  {"type": "text", "text": NOTE_CLOSE}]
            else:
                msg["content"] = as_note(content if isinstance(content, str) else "")

    def _build_reasoning_param(self) -> dict | None:
        """Build the ``reasoning`` parameter for providers that support it.

        OpenRouter (and the OpenAI o-series API) accept::

            "reasoning": {"effort": "high"}

        Maps ``thinking_level`` (from config) → ``effort`` value.
        ``thinking_budget`` maps to ``max_tokens`` inside ``reasoning``.
        OpenRouter treats ``effort`` and ``max_tokens`` as mutually exclusive
        ("one of the following, not both"), so when both are configured only
        ``effort`` is sent.

        Returns:
            Dict suitable for ``payload["reasoning"]``, or *None* if no
            thinking parameters are configured (then NO reasoning field is
            sent and the provider default applies).
        """
        if not self.thinking_level and not self.thinking_budget:
            return None

        reasoning: dict[str, Any] = {}
        if self.thinking_level:
            reasoning["effort"] = self.thinking_level
            if self.thinking_budget:
                logger.debug(
                    "thinking_budget=%s dropped: effort and max_tokens are "
                    "mutually exclusive in the reasoning param (model=%s)",
                    self.thinking_budget, self.model,
                )
        elif self.thinking_budget:
            reasoning["max_tokens"] = self.thinking_budget
        return reasoning

    @staticmethod
    def _accumulate_reasoning_detail(accumulated: dict[int, dict], rd: dict) -> None:
        """Merge one streamed ``reasoning_details`` fragment into the accumulator.

        Keyed by the fragment's ``index``; later fragments for the same index
        append ``data`` and overwrite the remaining keys. Shared by the main
        chunk loop and the tail-buffer parser so a fragment arriving only in
        the unterminated rest buffer is not lost (a dropped Gemini signature
        means MALFORMED_FUNCTION_CALL on the next turn).
        """
        rd_index = rd.get("index", 0)
        if rd_index in accumulated:
            existing = accumulated[rd_index]
            for k, v in rd.items():
                # Every field that carries payload arrives as FRAGMENTS of one
                # block and must be concatenated: ``text`` (plain thinking),
                # ``summary`` (condensed thinking) and ``data`` (encrypted).
                # Which one is used depends on the block's type, so all three
                # need the same treatment. Overwriting kept only the LAST
                # fragment — silently, because a short thought still looks valid.
                if k in ("data", "text", "summary") and existing.get(k) and isinstance(v, str):
                    existing[k] += v
                else:
                    existing[k] = v
        else:
            accumulated[rd_index] = dict(rd)

    @staticmethod
    def _reasoning_text_from_details(blocks: Any) -> str:
        """Plain thinking text carried inside ``reasoning_details`` blocks.

        Which block type keeps its text in which field is decided once, in
        ``reasoning_artifacts.thinking_text_from_details``; this is the name
        the client calls it by. Accepts the streaming accumulator
        (index -> block) or a plain list.

        Why it exists: three names carry one payload — DeepSeek sends
        ``reasoning_content``, OpenRouter sends ``reasoning``, and both routes
        also carry the text inside ``reasoning_details``. Measured over 4.000
        stored answers before the fix, ``reasoning_content`` was filled in NONE
        of them while the text was present in 52. Reading a single name meant
        receiving the thinking and dropping it on the floor.
        """
        return thinking_text_from_details(blocks)

    @staticmethod
    def _accumulate_tool_call_delta(accumulated: dict[int, dict], tc_delta: dict) -> int:
        """Merge one streamed tool-call delta into the accumulator (by index).

        Returns the tool-call index the delta belongs to. Shared by the main
        chunk loop and the tail-buffer parser — an argument fragment arriving
        only in the rest buffer would otherwise leave truncated JSON args.
        """
        index = tc_delta.get("index", 0)
        if index not in accumulated:
            accumulated[index] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""}
            }
        if "id" in tc_delta:
            accumulated[index]["id"] = tc_delta["id"]
        if "function" in tc_delta:
            func_delta = tc_delta["function"]
            if "name" in func_delta:
                accumulated[index]["function"]["name"] += func_delta["name"]
            if "arguments" in func_delta:
                accumulated[index]["function"]["arguments"] += func_delta["arguments"]
        return index

    def _filter_audio_from_content(self, content: Any) -> Any:
        """Filter and normalize content for OpenAI API.
        
        Uses shared openai_utils for consistent normalization across all OpenAI clients.
        Respects model capabilities - if model supports audio/video, keeps that content.
        
        Args:
            content: Message content (str, list, or dict)
            
        Returns:
            Normalized content for OpenAI API
        """
        # Check capabilities to determine what to allow
        allow_audio = False
        allow_video = False
        if self.capabilities:
            allow_audio = getattr(self.capabilities, 'audio_input', False)
            allow_video = getattr(self.capabilities, 'video_input', False)
        
        return openai_utils.normalize_message_content(
            content,
            allow_audio=allow_audio,
            allow_video=allow_video
        )

    async def chat(
        self,
        messages: list,
        cancellation_token: Optional[CancellationToken] = None
    ) -> str:
        """Send chat completion request without tools."""
        result = await self._make_request(messages, tools=[], cancellation_token=cancellation_token)
        return result.get("assistant", {}).get("content", "")

    async def chat_tools(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None,
        status_scope=None
    ) -> dict:
        """Send chat completion request with tools."""
        return await self._make_request(messages, tools=tools, cancellation_token=cancellation_token, status_scope=status_scope)

    async def chat_tools_streaming(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None,
        status_scope=None
    ):
        """Stream chat completion request with tools.

        Yields:
            dict: Streaming chunks with different types:
                {"type": "content_delta", "delta": str, "accumulated": str}
                {"type": "tool_call_delta", "index": int, "delta": dict}
                {"type": "final", "assistant": dict}
        """
        async for chunk in self._make_request_streaming(messages, tools=tools, cancellation_token=cancellation_token, status_scope=status_scope):
            yield chunk

    def supports_streaming(self) -> bool:
        """Check if this client supports streaming based on model capabilities."""
        # Check if capabilities explicitly disable streaming (handle both dict and object)
        if self.capabilities:
            if isinstance(self.capabilities, dict):
                return self.capabilities.get('streaming', True)
            elif hasattr(self.capabilities, 'streaming'):
                return self.capabilities.streaming
        return True  # Default to True if capabilities not set

    async def _report_status(self, status_scope, message: str) -> None:
        """Report status update if scope is available."""
        if status_scope is None:
            return
        try:
            await status_scope.progress(message)
        except Exception as e:
            logger.debug(f"Failed to report LLM status: {e}")

    async def _make_request(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None,
        status_scope=None
    ) -> dict:
        """Make the actual HTTP request with proper cancellation and error handling."""

        # Check if streaming is disabled in capabilities (handle both dict and object)
        streaming_enabled = True  # Default
        if self.capabilities:
            if isinstance(self.capabilities, dict):
                streaming_enabled = self.capabilities.get('streaming', True)
            elif hasattr(self.capabilities, 'streaming'):
                streaming_enabled = self.capabilities.streaming

        logger.debug(f"_make_request: streaming_enabled={streaming_enabled}, capabilities type={type(self.capabilities)}")

        if not streaming_enabled:
            # Use non-streaming request
            logger.debug("Using non-streaming request path")
            return await self._make_request_non_streaming(messages, tools, cancellation_token, status_scope)

        # Use streaming request (default behavior)
        logger.debug("Using streaming request path")
        final_result = None
        async for chunk in self._make_request_streaming(messages, tools, cancellation_token, status_scope):
            if chunk.get("type") == "final":
                # Extract all fields from final chunk (assistant, usage, etc.)
                final_result = {k: v for k, v in chunk.items() if k != "type"}
                break

        return final_result if final_result else {"assistant": {"role": "assistant", "content": ""}}

    async def _make_request_non_streaming(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None,
        status_scope=None
    ) -> dict:
        """Make non-streaming HTTP POST request for models that don't support streaming.

        Returns:
            dict: Response with 'assistant' key containing the assistant message
        """
        logger.debug(f"_make_request_non_streaming called for model {self.model}")

        # Build request payload - convert ChatMessage objects to dicts
        # NOTE: model_dump() is CPU-intensive for large messages (can take 150ms+ for 30+ messages)
        # Run in thread pool to avoid blocking event loop
        def _serialize_messages() -> list:
            result = []
            for msg in messages:
                if hasattr(msg, 'model_dump'):
                    d = msg.model_dump(exclude_none=True, mode='json')
                    # Remove multimodal_content from serialized dict - it's processed separately
                    d.pop('multimodal_content', None)
                    # Filter out audio/video content - not supported by Chat Completions API
                    if 'content' in d:
                        d['content'] = self._filter_audio_from_content(d['content'])
                    # Whitelist only API-accepted fields, sanitize tool_calls
                    d = HTTPXOpenAIClient._sanitize_message_for_api(d)
                    result.append(d)
                    
                    # Inject multimodal content as synthetic user message after tool response
                    if getattr(msg, 'role', None) == 'tool' and getattr(msg, 'multimodal_content', None):
                        injection = self._create_multimodal_injection(msg)
                        if injection:
                            result.append(injection)
                elif isinstance(msg, dict):
                    d = dict(msg)
                    if 'content' in d:
                        d['content'] = self._filter_audio_from_content(d['content'])
                    d = HTTPXOpenAIClient._sanitize_message_for_api(d)
                    result.append(d)
                else:
                    result.append(dict(msg))
            return result
        
        message_dicts = await asyncio.to_thread(_serialize_messages)
        self._postprocess_messages_for_provider(message_dicts)
        # The key is resolved BEFORE the block split: the segment ladder needs it
        # for the registry (docs/prompt_cache_design.md). From the ORIGINAL
        # messages, not message_dicts: the rung step in the postprocess above has
        # dropped `injected_by` from them, and without the marker the derivation
        # takes a plugin block rebuilt on every call for prompt.
        resolved_cache_key = (
            derive_prompt_cache_key(self.prompt_cache_key, messages)
            if self.prompt_cache_key else None
        )
        self._apply_cache_breakpoints(message_dicts, resolved_cache_key)

        payload = {
            "model": self.model,
            "messages": message_dicts,
            "stream": False,  # ⚡ Disable streaming
            **self.extra_params
        }
        
        # Thinking/reasoning config for thinking models (OpenRouter, DeepSeek, etc.)
        reasoning = self._build_reasoning_param()
        if reasoning:
            payload["reasoning"] = reasoning

        # Service tier (e.g. Google Flex via OpenRouter)
        if self.service_tier:
            payload["service_tier"] = self.service_tier

        self._apply_gateway_extras(payload, resolved_cache_key)

        # Provider routing (OpenRouter): bias toward a sticky backend so the
        # implicit prompt cache stays warm. Only honored by OpenRouter.
        if self.provider_routing and self._is_openrouter:
            payload["provider"] = await routing_pinned_to_last_backend(
                self.provider_routing, messages, self.base_url, self._verify)

        # Add max_tokens if configured (limits output length)
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens

        # Sampling-Temperatur, nur wenn explizit konfiguriert (0.0 ist ein
        # gültiger Wert → auf None prüfen, nicht auf Falsy). Reasoning-
        # Modelle akzeptieren den Param nicht: genau dann, wenn ein
        # reasoning-Feld gesendet wird, NICHT senden — sonst 400er.
        # (Dasselbe Prädikat wie der Payload-Bau — vorher divergierten die
        # Guards und thinking_budget=0 unterdrückte temperature, obwohl gar
        # kein reasoning-Feld gesendet wurde. Gilt BEWUSST auch für
        # effort="none": OpenAI-Hybride lehnen temperature≠1 auch bei
        # abgeschaltetem Thinking ab — konservativ unterdrücken.)
        if self.temperature is not None:
            if reasoning is not None:
                logger.debug(
                    "temperature=%s ignoriert (Reasoning-Modell, model=%s)",
                    self.temperature, self.model,
                )
            else:
                payload["temperature"] = self.temperature

        self._apply_tool_fields(payload, tools, message_dicts)

        # Content-filter thresholds, sent whenever they are configured
        if self.safety_settings:
            payload["safety_settings"] = [
                {"category": category, "threshold": threshold}
                for category, threshold in self.safety_settings.items()
            ]

        url = f"{self.base_url}/chat/completions"

        # Notify pre-request hook (LLM-client level)
        import time as _time
        await self._notify_pre_request({
            "provider": "openai_httpx",
            "model": self.model,
            "url": url,
            "payload": payload,
            "is_streaming": False,
            "timestamp_ms": _time.time() * 1000,
        })

        # Retry logic with exponential backoff
        _request_start = _time.time()
        last_exception = None
        _effective_max = max(self.max_retries, self.rate_limit_max_retries)
        # One-shot self-healing retry for cross-backend thought-signature mismatch.
        # See _detect_body_400_signature_issue for the failure mode.
        _sig_retried = False
        # Two-stage retry for OpenAI encrypted-reasoning 400s (0=targeted item
        # strip, 1=full strip). See _recover_encrypted_reasoning.
        _enc_retries = 0
        for attempt in range(_effective_max + 1):
            # Check cancellation before each attempt
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")

            try:
                # Create fresh client for each request
                client_kwargs: dict[str, Any] = {"timeout": self._timeout}
                if getattr(self, "_verify", None) is not None:
                    client_kwargs["verify"] = self._verify

                async with httpx.AsyncClient(**client_kwargs) as client:
                    logger.debug(f"HTTPX non-streaming request attempt {attempt + 1}/{self.max_retries + 1} to {url}")

                    # Make regular POST request (not streaming)
                    response = await client.post(url=url, headers=self._headers, json=payload)

                    # Handle rate limiting (429) - longer backoff + jitter to avoid thundering herd
                    if response.status_code == 429:
                        retry_after = self._parse_retry_after(response.headers.get("retry-after"))
                        if attempt < self.rate_limit_max_retries:
                            base = retry_after or (self.rate_limit_backoff * (1.5 ** attempt))
                            jitter = base * random.uniform(0.0, 0.5)
                            backoff_time = base + jitter
                            logger.warning(f"Rate limited (429), retrying in {backoff_time:.0f}s (attempt {attempt + 1}/{self.rate_limit_max_retries})")
                            await self._report_status(status_scope, f"Rate limited, retry {attempt + 1}/{self.rate_limit_max_retries}: {self.model} (wait {backoff_time:.0f}s)")
                            await self._notify_retry("openai_httpx", self.model, url, False, "Rate limited (429)", attempt, self.rate_limit_max_retries + 1)
                            await self._cancellable_sleep(backoff_time, cancellation_token)
                            continue
                        # Retries exhausted - raise for fallback
                        error_text = response.text[:200] if response.text else ""
                        await self._report_status(status_scope, f"Rate limit exceeded: {self.model}")
                        # Notify response hook on error
                        _duration_ms = (_time.time() - _request_start) * 1000
                        await self._notify_post_response({
                            "provider": "openai_httpx", "model": self.model, "url": url,
                            "is_streaming": False, "duration_ms": _duration_ms,
                            "error": f"Rate limit: {error_text}", "timestamp_ms": _time.time() * 1000,
                        })
                        if "quota" in error_text.lower() or "exhausted" in error_text.lower():
                            raise LLMQuotaExhaustedError(
                                f"Quota exhausted: {error_text}",
                                provider="httpx", model=self.model, retry_after=retry_after
                            )
                        raise LLMRateLimitError(
                            f"Rate limit exceeded: {error_text}",
                            provider="httpx", model=self.model, retry_after=retry_after
                        )

                    # Handle server errors (5xx) - retry with exponential backoff
                    if response.status_code >= 500 and attempt < self.max_retries:
                        backoff_time = self.retry_backoff * (2 ** attempt)
                        logger.warning(f"Server error {response.status_code}, retrying in {backoff_time}s")
                        await self._report_status(status_scope, f"Server error, retry {attempt + 1}/{self.max_retries}: {self.model}")
                        await self._notify_retry("openai_httpx", self.model, url, False, f"Server error ({response.status_code})", attempt, self.max_retries + 1)
                        await self._cancellable_sleep(backoff_time, cancellation_token)
                        continue

                    # Check for HTTP errors (4xx client errors or exhausted retries)
                    if response.status_code >= 400:
                        error_text = response.text[:200] if response.text else ""
                        error_msg = f"HTTP {response.status_code}: {error_text}"

                        # Self-healing: OpenAI encrypted-reasoning 400 (defective
                        # blob from the OpenRouter bridge) can arrive as an
                        # HTTP-STATUS 400 — not just a body-level 400. The
                        # body-level retry below only runs after json() on a 2xx,
                        # so we must also catch it here. Recovery happens BEFORE
                        # the ERROR log: the healed case is routine self-repair
                        # and only logs its own WARNING.
                        if response.status_code == 400 and _enc_retries < 2:
                            _full_body = response.text or ""
                            _enc_hit = ("encrypted content" in _full_body
                                        and "rs_" in _full_body)
                            if _enc_hit:
                                recovery = self._recover_encrypted_reasoning(
                                    _full_body, payload, messages, _enc_retries)
                                if recovery:
                                    _enc_retries += 1
                                    logger.warning(
                                        "HTTP-400 retry: %s (defective encrypted "
                                        "reasoning item). model=%s detail=%r",
                                        recovery, self.model, _full_body[:500],
                                    )
                                    await self._report_status(
                                        status_scope,
                                        f"Encrypted-reasoning retry: {self.model}",
                                    )
                                    await self._notify_retry(
                                        "openai_httpx", self.model, url, False,
                                        "http-400 encrypted-reasoning strip",
                                        attempt, self.max_retries + 1,
                                    )
                                    continue

                        # Self-healing: Gemini "Corrupted thought signature" can
                        # ALSO arrive as an HTTP-STATUS 400 (observed 2026-07-24,
                        # gemini-3.5-flash-lite) — the signature-bypass below
                        # only covers the body-level shape, so without this
                        # branch the run crashed hard instead of healing.
                        if (response.status_code == 400 and not _sig_retried
                                and "thought signature" in (response.text or "").lower()):
                            n_patched = self._inject_signature_bypass(payload)
                            if n_patched > 0:
                                _sig_retried = True
                                logger.warning(
                                    "HTTP-400 retry: injecting signature bypass "
                                    "token into reasoning_details (%d block(s)). "
                                    "model=%s detail=%r",
                                    n_patched, self.model, (response.text or "")[:500],
                                )
                                await self._report_status(
                                    status_scope,
                                    f"Signature bypass retry: {self.model}",
                                )
                                await self._notify_retry(
                                    "openai_httpx", self.model, url, False,
                                    "http-400 signature bypass",
                                    attempt, self.max_retries + 1,
                                )
                                continue

                        logger.error(f"HTTPX non-streaming request failed: {error_msg}")
                        _duration_ms = (_time.time() - _request_start) * 1000
                        await self._notify_post_response({
                            "provider": "openai_httpx", "model": self.model, "url": url,
                            "is_streaming": False, "duration_ms": _duration_ms,
                            "error": error_msg, "timestamp_ms": _time.time() * 1000,
                        })
                        if response.status_code >= 500:
                            raise LLMServerError(
                                error_msg, provider="httpx", model=self.model,
                                status_code=response.status_code,
                            )
                        raise httpx.HTTPStatusError(error_msg, request=response.request, response=response)

                    # Parse successful response.
                    # If JSON parsing fails (occasional truncated bodies seen
                    # from OpenRouter on large multi-MB requests), log enough
                    # diagnostics to discriminate between truncation, wrong
                    # content-type, and silent gateway errors before the
                    # outer except re-raises and triggers the retry.
                    try:
                        response_data = response.json()
                    except json.JSONDecodeError as _json_err:
                        body_bytes = response.content or b""
                        content_length_hdr = response.headers.get("content-length")
                        try:
                            cl_int = int(content_length_hdr) if content_length_hdr else None
                        except ValueError:
                            cl_int = None
                        truncated = cl_int is not None and len(body_bytes) < cl_int
                        # Sample body endpoints; encrypt-safe slicing on bytes
                        head = body_bytes[:300].decode("utf-8", errors="replace")
                        tail = body_bytes[-300:].decode("utf-8", errors="replace")
                        logger.warning(
                            "JSON decode failed on LLM response (likely truncated body). "
                            "model=%s status=%s content_type=%r content_length_hdr=%s "
                            "received_bytes=%d truncated=%s transfer_encoding=%r "
                            "cf_ray=%r server=%r error=%s",
                            self.model,
                            response.status_code,
                            response.headers.get("content-type"),
                            content_length_hdr,
                            len(body_bytes),
                            truncated,
                            response.headers.get("transfer-encoding"),
                            response.headers.get("cf-ray"),
                            response.headers.get("server"),
                            _json_err,
                        )
                        logger.warning("  body head[0:300]: %r", head)
                        logger.warning("  body tail[-300:]: %r", tail)
                        raise

                    # Body-level upstream 429 (e.g. OpenRouter proxying upstream
                    # rate-limit from OpenAI/Gemini Flex). Same backoff schedule
                    # as HTTP-status 429. Drop service_tier from the *local*
                    # payload (flex -> standard) on the first 429 so the retry
                    # tries the standard tier - without mutating self, which
                    # keeps the singleton clean for parallel requests.
                    _body_429_msg = self._detect_body_429(response_data)
                    if _body_429_msg and attempt < self.rate_limit_max_retries:
                        base = self.rate_limit_backoff * (1.5 ** attempt)
                        jitter = base * random.uniform(0.0, 0.5)
                        backoff_time = base + jitter
                        tier_note = ""
                        if payload.get("service_tier"):
                            dropped = payload.pop("service_tier")
                            tier_note = f", dropping service_tier={dropped!r}"
                        logger.warning(
                            f"Upstream 429 in response body ({_body_429_msg[:80]}), "
                            f"retrying in {backoff_time:.0f}s "
                            f"(attempt {attempt + 1}/{self.rate_limit_max_retries}){tier_note}"
                        )
                        await self._report_status(
                            status_scope,
                            f"Upstream 429, retry {attempt + 1}/{self.rate_limit_max_retries}: {self.model} (wait {backoff_time:.0f}s)"
                        )
                        await self._notify_retry(
                            "openai_httpx", self.model, url, False,
                            f"Upstream 429 body-error{tier_note}",
                            attempt, self.rate_limit_max_retries + 1,
                        )
                        await self._cancellable_sleep(backoff_time, cancellation_token)
                        continue

                    # Body-level 400: OpenAI encrypted-reasoning mismatch (rs_*
                    # item fails verification on the routed backend). Checked
                    # FIRST (more specific) so the permissive generic signature
                    # detector below doesn't claim it — its Gemini bypass token
                    # would overwrite the encrypted `data` and corrupt the chain.
                    # Same ordering as the streaming path.
                    # Recovery via _recover_encrypted_reasoning: stage 0 drops
                    # only the named defective item, stage 1 full-strips. Both
                    # stages also heal the ORIGINAL session messages — stripping
                    # only the payload copy would re-trigger the same 400 on
                    # every following turn.
                    _enc_issue = (
                        self._detect_openai_encrypted_reasoning_400(response_data)
                        if _enc_retries < 2 else None
                    )
                    if _enc_issue is not None:
                        recovery = self._recover_encrypted_reasoning(
                            _enc_issue, payload, messages, _enc_retries)
                        if recovery:
                            _enc_retries += 1
                            logger.warning(
                                "Body-400 retry: %s (defective encrypted "
                                "reasoning item). model=%s detail=%r",
                                recovery, self.model, _enc_issue[:500],
                            )
                            await self._report_status(
                                status_scope,
                                f"Encrypted-reasoning retry: {self.model}",
                            )
                            await self._notify_retry(
                                "openai_httpx", self.model, url, False,
                                "body-400 encrypted-reasoning strip",
                                attempt, self.max_retries + 1,
                            )
                            continue

                    # Body-level 400: probable Gemini cross-backend thought-signature
                    # mismatch. OpenRouter routes Gemini requests between Vertex
                    # and AI Studio (per provider_routing.order + allow_fallbacks).
                    # Thought signatures are encrypted blobs keyed to the signing
                    # backend - the OTHER backend rejects them with "Corrupted
                    # thought signature". Vertex is strict and requires a valid
                    # signature; AI Studio is lenient but OR's translation layer
                    # can also mangle the signature mid-route.
                    #
                    # Recovery: replace `data` in every reasoning.encrypted block
                    # with Google's documented bypass token
                    # ("skip_thought_signature_validator"). Both Vertex and AI
                    # Studio recognize this string as a signal to skip signature
                    # validation. Structure (type, format, id, index) is left
                    # intact so OR's translation to Google's native format still
                    # works. One-shot: if the retry still 400s, fall through to
                    # the agent-level fallback chain.
                    _sig_issue = (
                        self._detect_body_400_signature_issue(response_data)
                        if not _sig_retried else None
                    )
                    if _sig_issue is not None:
                        n_patched = self._inject_signature_bypass(payload)
                        if n_patched > 0:
                            _sig_retried = True
                            response_backend = response_data.get("provider")
                            logger.warning(
                                "Body-400 retry: injecting signature bypass token "
                                "into reasoning_details (likely cross-backend "
                                "Vertex<->AI Studio routing mismatch). model=%s "
                                "response_backend=%r patched_blocks=%d detail=%r",
                                self.model, response_backend, n_patched,
                                _sig_issue[:200],
                            )
                            await self._report_status(
                                status_scope,
                                f"Signature bypass retry: {self.model}",
                            )
                            await self._notify_retry(
                                "openai_httpx", self.model, url, False,
                                "body-400 signature bypass",
                                attempt, self.max_retries + 1,
                            )
                            continue

                    # Detect Gemini MALFORMED_FUNCTION_CALL — a transient model error
                    # where identical payloads can succeed or fail non-deterministically.
                    # Retry instead of returning an empty response to the agent.
                    if self._is_gemini_malformed_response(response_data) and attempt < self.max_retries:
                        backoff_time = self.retry_backoff * (2 ** attempt)
                        logger.warning(
                            f"Gemini MALFORMED_FUNCTION_CALL (transient), "
                            f"retrying in {backoff_time}s (attempt {attempt + 1}/{self.max_retries + 1})"
                        )
                        await self._report_status(
                            status_scope,
                            f"Gemini malformed response, retry {attempt + 1}/{self.max_retries}: {self.model}"
                        )
                        await self._notify_retry("openai_httpx", self.model, url, False, "MALFORMED_FUNCTION_CALL", attempt, self.max_retries + 1, response_data=response_data)
                        await self._cancellable_sleep(backoff_time, cancellation_token)
                        continue

                    # Detect Gemini's internal-format-leak — same family as MALFORMED
                    # but without the error signal: the model emits its function call
                    # as plain text (`call:default_api:NAME{…}`) instead of structured
                    # tool_calls, and finish_reason is just "stop". Without this the
                    # agent loop accepts the response as a final answer and the task
                    # silently ends mid-workflow.
                    if self._is_gemini_internal_format_leak(response_data) and attempt < self.max_retries:
                        backoff_time = self.retry_backoff * (2 ** attempt)
                        leaked = response_data["choices"][0]["message"].get("content", "")[:120]
                        logger.warning(
                            f"Gemini internal-format leak in content (no tool_calls), "
                            f"retrying in {backoff_time}s (attempt {attempt + 1}/{self.max_retries + 1}). "
                            f"Leaked head: {leaked!r}"
                        )
                        await self._report_status(
                            status_scope,
                            f"Gemini internal-format leak, retry {attempt + 1}/{self.max_retries}: {self.model}"
                        )
                        await self._notify_retry(
                            "openai_httpx", self.model, url, False,
                            "GEMINI_INTERNAL_FORMAT_LEAK",
                            attempt, self.max_retries + 1, response_data=response_data,
                        )
                        await self._cancellable_sleep(backoff_time, cancellation_token)
                        continue

                    # Notify post-response hook with successful response
                    _duration_ms = (_time.time() - _request_start) * 1000
                    _usage = response_data.get("usage")
                    _finish = None
                    if response_data.get("choices"):
                        _finish = response_data["choices"][0].get("finish_reason")
                    await self._notify_post_response({
                        "provider": "openai_httpx", "model": self.model, "url": url,
                        "is_streaming": False, "duration_ms": _duration_ms,
                        "response_data": response_data,
                        "usage": _usage, "finish_reason": _finish,
                        "routing": openrouter_routing_info(response_data),
                        "timestamp_ms": _time.time() * 1000,
                    })

                    # Use centralized response formatting (handles usage, tool_calls, etc.)
                    return self._format_response(response_data)

            except httpx.HTTPStatusError:
                raise  # Re-raise HTTP errors immediately
            except asyncio.CancelledError:
                raise  # Re-raise cancellation
            except (LLMRateLimitError, LLMServerError, LLMConnectionError):
                # Typed fallback errors (incl. LLMQuotaExhaustedError, a
                # subclass of LLMRateLimitError) are raised intentionally above
                # for the agent server's LLM-fallback mechanism. They must NOT
                # be caught by the generic handler below (which would re-wrap
                # them in a plain Exception and break fallback detection). The
                # streaming path does not catch them either - this keeps both
                # paths consistent. LLMConnectionError is not raised inside
                # this try today (it is produced by the generic handler below)
                # - listed defensively so a future raise site cannot be
                # re-wrapped into an untyped Exception.
                raise
            except Exception as e:
                last_exception = e
                err_label = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                if attempt < self.max_retries:
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(
                        f"Request failed (attempt {attempt + 1}/{self.max_retries + 1}) "
                        f"model={self.model} url={url}: {err_label}. Retrying in {backoff_time}s"
                    )
                    await self._report_status(status_scope, f"Request failed, retry {attempt + 1}/{self.max_retries}: {self.model} ({err_label})")
                    await self._notify_retry("openai_httpx", self.model, url, False, err_label, attempt, self.max_retries + 1)
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                else:
                    logger.error(
                        f"Request failed after {self.max_retries + 1} attempts "
                        f"model={self.model} url={url}: {err_label}"
                    )
                    await self._report_status(status_scope, f"Request failed after retries: {self.model} ({err_label})")
                    if isinstance(last_exception, httpx.TransportError):
                        # Endpoint nicht erreichbar (ConnectTimeout, ReadTimeout,
                        # Netzfehler) — getypt werfen, damit der Agent-Server auf
                        # das naechste Profil der llm_profile-Kette wechseln kann.
                        raise LLMConnectionError(
                            f"HTTP request failed after {self.max_retries + 1} attempts ({err_label}) url={url}",
                            provider="openai_httpx", model=self.model,
                        ) from last_exception
                    raise Exception(f"HTTP request failed after {self.max_retries + 1} attempts ({err_label}) url={url}") from last_exception

        # Should never reach here
        raise Exception(f"HTTP request failed after {self.max_retries + 1} attempts: {last_exception!r}") from last_exception

    async def _make_request_streaming(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None,
        status_scope=None
    ):
        """Make streaming HTTP request that yields chunks.

        Yields:
            dict: Chunks with types: content_delta, tool_call_delta, final
        """

        # Build request payload - convert ChatMessage objects to dicts
        # NOTE: model_dump() is CPU-intensive for large messages (can take 150ms+ for 30+ messages)
        # Run in thread pool to avoid blocking event loop
        def _serialize_messages() -> list:
            result = []
            for msg in messages:
                if hasattr(msg, 'model_dump'):
                    d = msg.model_dump(exclude_none=True, mode='json')
                    # Filter out audio/video content - not supported by Chat Completions API
                    if 'content' in d:
                        d['content'] = self._filter_audio_from_content(d['content'])
                    # Whitelist only API-accepted fields, sanitize tool_calls
                    d = HTTPXOpenAIClient._sanitize_message_for_api(d)
                    result.append(d)
                    
                    # Inject multimodal content as synthetic user message after tool response
                    if getattr(msg, 'role', None) == 'tool' and getattr(msg, 'multimodal_content', None):
                        injection = self._create_multimodal_injection(msg)
                        if injection:
                            result.append(injection)
                elif isinstance(msg, dict):
                    d = dict(msg)
                    if 'content' in d:
                        d['content'] = self._filter_audio_from_content(d['content'])
                    d = HTTPXOpenAIClient._sanitize_message_for_api(d)
                    result.append(d)
                else:
                    result.append(dict(msg))
            return result
        
        message_dicts = await asyncio.to_thread(_serialize_messages)
        self._postprocess_messages_for_provider(message_dicts)
        # The key is resolved BEFORE the block split: the segment ladder needs it
        # for the registry (docs/prompt_cache_design.md). From the ORIGINAL
        # messages, not message_dicts: the rung step in the postprocess above has
        # dropped `injected_by` from them, and without the marker the derivation
        # takes a plugin block rebuilt on every call for prompt.
        resolved_cache_key = (
            derive_prompt_cache_key(self.prompt_cache_key, messages)
            if self.prompt_cache_key else None
        )
        self._apply_cache_breakpoints(message_dicts, resolved_cache_key)

        payload = {
            "model": self.model,
            "messages": message_dicts,
            "stream": True,  # ⚡ Enable streaming
            "stream_options": {"include_usage": True},  # Request usage stats in stream
            **self.extra_params
        }
        
        # Thinking/reasoning config for thinking models (OpenRouter, DeepSeek, etc.)
        reasoning = self._build_reasoning_param()
        if reasoning:
            payload["reasoning"] = reasoning

        # Service tier (e.g. Google Flex via OpenRouter)
        if self.service_tier:
            payload["service_tier"] = self.service_tier

        self._apply_gateway_extras(payload, resolved_cache_key)

        # Provider routing (OpenRouter): bias toward a sticky backend so the
        # implicit prompt cache stays warm. Only honored by OpenRouter.
        if self.provider_routing and self._is_openrouter:
            payload["provider"] = await routing_pinned_to_last_backend(
                self.provider_routing, messages, self.base_url, self._verify)

        # Add max_tokens if configured (limits output length)
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens

        # Sampling-Temperatur, nur wenn explizit konfiguriert (0.0 ist ein
        # gültiger Wert → auf None prüfen, nicht auf Falsy). Reasoning-
        # Modelle akzeptieren den Param nicht: genau dann, wenn ein
        # reasoning-Feld gesendet wird, NICHT senden — sonst 400er.
        # (Dasselbe Prädikat wie der Payload-Bau — vorher divergierten die
        # Guards und thinking_budget=0 unterdrückte temperature, obwohl gar
        # kein reasoning-Feld gesendet wurde. Gilt BEWUSST auch für
        # effort="none": OpenAI-Hybride lehnen temperature≠1 auch bei
        # abgeschaltetem Thinking ab — konservativ unterdrücken.)
        if self.temperature is not None:
            if reasoning is not None:
                logger.debug(
                    "temperature=%s ignoriert (Reasoning-Modell, model=%s)",
                    self.temperature, self.model,
                )
            else:
                payload["temperature"] = self.temperature

        self._apply_tool_fields(payload, tools, message_dicts)

        # Content-filter thresholds, sent whenever they are configured
        if self.safety_settings:
            payload["safety_settings"] = [
                {"category": category, "threshold": threshold}
                for category, threshold in self.safety_settings.items()
            ]

        url = f"{self.base_url}/chat/completions"

        # Notify pre-request hook (LLM-client level)
        import time as _time
        await self._notify_pre_request({
            "provider": "openai_httpx",
            "model": self.model,
            "url": url,
            "payload": payload,
            "is_streaming": True,
            "timestamp_ms": _time.time() * 1000,
        })

        # Retry logic with exponential backoff
        _streaming_request_start = _time.time()
        last_exception: Exception | None = None
        _effective_max = max(self.max_retries, self.rate_limit_max_retries)
        # One-shot self-healing retry for cross-backend thought-signature
        # mismatch (mirrors the non-streaming path). See
        # _detect_body_400_signature_issue + _inject_signature_bypass.
        _sig_retried = False
        # Two-stage retry for OpenAI encrypted-reasoning 400s (0=targeted item
        # strip, 1=full strip; mirrors non-streaming). See
        # _recover_encrypted_reasoning.
        _enc_retries = 0
        for attempt in range(_effective_max + 1):
            # Accumulators for building the complete response. MUST be reset at
            # the start of every attempt: a stream that drops mid-response
            # (RemoteProtocolError on a stalled stream is common during long
            # thinking pauses) retries the whole request - keeping the partial
            # chunks from the failed attempt would concatenate them with the
            # retry's output, duplicating text and corrupting tool-call argument
            # JSON. The other clients (anthropic/gemini) reset on retry too.
            accumulated_content: list[str] = []
            accumulated_reasoning: list[str] = []  # reasoning_content (DeepSeek, OpenAI o-series)
            accumulated_tool_calls: dict[int, dict[str, Any]] = {}  # index -> tool call data
            # OpenRouter delivers Gemini 3.x thought_signature inside reasoning_details
            # blocks (format=google-gemini-v1). Must round-trip on next turn or upstream
            # returns MALFORMED_FUNCTION_CALL. We keep blocks keyed by index so deltas
            # from the same block accumulate cleanly.
            accumulated_reasoning_details: dict[int, dict[str, Any]] = {}
            accumulated_usage = None  # usage information from final chunk
            # Which backend served the stream: the gateway puts
            # openrouter_metadata in the LAST chunk and a plain
            # `provider` earlier, so the richer record wins.
            accumulated_routing = None
            _last_finish_reason: str | None = None  # finish_reason from chunks

            # Check cancellation before each attempt
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")

            # Body-level 429 retry signal — set inside chunk parsing when the
            # upstream wraps a rate-limit in a normal HTTP 200 SSE chunk.
            # Picked up after the async with block exits.
            _body_429_retry_msg: Optional[str] = None
            # Body-level 400 signature-bypass retry signal — same pattern as
            # _body_429_retry_msg, but for cross-backend signature mismatch.
            _body_400_sig_retry_msg: Optional[str] = None
            # Body-level 400 OpenAI encrypted-reasoning retry signal (same
            # pattern) — recover by stripping reasoning_details, not a bypass.
            _body_400_enc_retry_msg: Optional[str] = None

            try:
                # Create fresh client for each request to avoid connection issues
                # Enable TCP keep-alive to prevent connection drops during long "thinking" pauses
                # This is especially important on Linux servers where firewalls/proxies may
                # close idle connections after ~30s
                socket_options = self._get_keepalive_socket_options()
                
                # Configure HTTP transport with socket options
                # Note: http2=True can help avoid some SSL shutdown issues on certain platforms
                # but may cause compatibility issues with some APIs, so we stick with HTTP/1.1
                transport = httpx.AsyncHTTPTransport(
                    retries=0,  # We handle retries ourselves
                    socket_options=socket_options,
                    # Disable HTTP/2 to avoid potential compatibility issues
                    http2=False,
                    # httpx ignores the client-level verify once a transport is
                    # passed in -- the TLS context has to be handed to the
                    # transport itself, or every streaming request builds its
                    # own (measured: one CA-bundle load per request).
                    verify=self._verify,
                )
                
                client_kwargs: dict[str, Any] = {
                    "timeout": self._timeout,
                    "transport": transport
                }
                # Only include verify if explicitly configured (None means use httpx default)
                if getattr(self, "_verify", None) is not None:
                    client_kwargs["verify"] = self._verify

                # Create client - we'll handle cleanup carefully to avoid SSL shutdown segfaults
                client = httpx.AsyncClient(**client_kwargs)
                try:
                    logger.debug(f"HTTPX streaming request attempt {attempt + 1}/{self.max_retries + 1} to {url}")

                    # Make streaming request
                    async with client.stream("POST", url=url, headers=self._headers, json=payload) as response:
                        # Check status code (don't use raise_for_status() - it tries to read the body)
                        if response.status_code == 429:
                            retry_after = self._parse_retry_after(response.headers.get("retry-after"))
                            if attempt < self.rate_limit_max_retries:
                                base = retry_after or (self.rate_limit_backoff * (1.5 ** attempt))
                                jitter = base * random.uniform(0.0, 0.5)
                                backoff_time = base + jitter
                                logger.warning(f"Rate limited (429), retrying in {backoff_time:.0f}s (attempt {attempt + 1}/{self.rate_limit_max_retries})")
                                await self._report_status(status_scope, f"Rate limited, retry {attempt + 1}/{self.rate_limit_max_retries}: {self.model} (wait {backoff_time:.0f}s)")
                                await self._notify_retry("openai_httpx", self.model, url, True, "Rate limited (429)", attempt, self.rate_limit_max_retries + 1)
                                await self._cancellable_sleep(backoff_time, cancellation_token)
                                continue
                            # Retries exhausted - raise for fallback
                            error_body = await response.aread()
                            error_text = error_body.decode()[:200] if error_body else ""
                            await self._report_status(status_scope, f"Rate limit exceeded: {self.model}")
                            if "quota" in error_text.lower() or "exhausted" in error_text.lower():
                                raise LLMQuotaExhaustedError(
                                    f"Quota exhausted: {error_text}",
                                    provider="httpx", model=self.model, retry_after=retry_after
                                )
                            raise LLMRateLimitError(
                                f"Rate limit exceeded: {error_text}",
                                provider="httpx", model=self.model, retry_after=retry_after
                            )

                        # Handle server errors (5xx) - retry with exponential backoff
                        if response.status_code >= 500 and attempt < self.max_retries:
                            backoff_time = self.retry_backoff * (2 ** attempt)
                            logger.warning(f"Server error {response.status_code}, retrying in {backoff_time}s")
                            await self._report_status(status_scope, f"Server error, retry {attempt + 1}/{self.max_retries}: {self.model}")
                            await self._notify_retry("openai_httpx", self.model, url, True, f"Server error ({response.status_code})", attempt, self.max_retries + 1)
                            await self._cancellable_sleep(backoff_time, cancellation_token)
                            continue

                        # Check for errors without reading body (streaming response)
                        if response.status_code >= 400:
                            # Read the error body for streaming responses
                            error_body = await response.aread()
                            error_text = error_body.decode('utf-8', errors='replace')
                            error_msg = f"HTTP {response.status_code}: {error_text[:200]}"
                            # Self-healing: OpenAI encrypted-reasoning 400
                            # arriving as an HTTP-status 400 (mirrors the
                            # non-streaming path). Two-stage recovery, heals
                            # payload AND original session messages. Runs BEFORE
                            # the ERROR log: the healed case is routine
                            # self-repair and only logs its own WARNING.
                            if response.status_code == 400 and _enc_retries < 2:
                                if "encrypted content" in error_text and "rs_" in error_text:
                                    recovery = self._recover_encrypted_reasoning(
                                        error_text, payload, messages, _enc_retries)
                                    if recovery:
                                        _enc_retries += 1
                                        logger.warning(
                                            "HTTP-400 stream retry: %s (defective "
                                            "encrypted reasoning item). model=%s detail=%r",
                                            recovery, self.model, error_text[:500],
                                        )
                                        await self._report_status(
                                            status_scope,
                                            f"Encrypted-reasoning retry: {self.model}",
                                        )
                                        await self._notify_retry(
                                            "openai_httpx", self.model, url, True,
                                            "http-400 encrypted-reasoning strip (stream)",
                                            attempt, self.max_retries + 1,
                                        )
                                        continue

                            # Self-healing: Gemini "Corrupted thought signature"
                            # as HTTP-STATUS 400 (mirrors the non-streaming
                            # path; the body-level bypass below doesn't see
                            # status-level 400s).
                            if (response.status_code == 400 and not _sig_retried
                                    and "thought signature" in error_text.lower()):
                                n_patched = self._inject_signature_bypass(payload)
                                if n_patched > 0:
                                    _sig_retried = True
                                    logger.warning(
                                        "HTTP-400 stream retry: injecting signature "
                                        "bypass token into reasoning_details "
                                        "(%d block(s)). model=%s detail=%r",
                                        n_patched, self.model, error_text[:500],
                                    )
                                    await self._report_status(
                                        status_scope,
                                        f"Signature bypass retry: {self.model}",
                                    )
                                    await self._notify_retry(
                                        "openai_httpx", self.model, url, True,
                                        "http-400 signature bypass (stream)",
                                        attempt, self.max_retries + 1,
                                    )
                                    continue

                            logger.error(f"HTTPX streaming request failed: {error_msg}")
                            if response.status_code >= 500:
                                raise LLMServerError(
                                    error_msg, provider="httpx", model=self.model,
                                    status_code=response.status_code,
                                )
                            raise httpx.HTTPStatusError(error_msg, request=response.request, response=response)

                        # Parse SSE stream with chunk timeout
                        # Use aiter_bytes() instead of aiter_lines() because aiter_lines()
                        # can block indefinitely inside httpcore when server keeps connection
                        # open but stops sending data. With aiter_bytes() we get smaller chunks
                        # and our timeout actually works.
                        chunk_timeout = self.timeout_config.read
                        # aiter_bytes() is async iterator that we can iterate over directly
                        line_buffer = ""

                        # Create async iterator manually to apply timeout per chunk
                        byte_stream = response.aiter_bytes()
                        # Keep-alive comments reset the per-chunk timeout, so
                        # only events count as progress; without the declared
                        # limit an upstream that stopped answering holds the
                        # call as long as the endpoint keeps it open.
                        silence_limit = self.stream_silence_timeout
                        last_event = time.monotonic()
                        # The network cuts where it likes: a character split
                        # across two chunks must not decode as two U+FFFD.
                        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

                        while True:
                            if cancellation_token and cancellation_token.is_cancelled:
                                raise asyncio.CancelledError("Request cancelled during streaming")

                            try:
                                # Get next chunk with timeout
                                chunk_bytes = await asyncio.wait_for(byte_stream.__anext__(), timeout=chunk_timeout)
                                line_buffer += decoder.decode(chunk_bytes)
                            except StopAsyncIteration:
                                # Stream completed - process any remaining data in buffer
                                break
                            except (asyncio.TimeoutError, httpx.TransportError) as error:
                                # the answer is complete; only [DONE] is missing -- whether the
                                # stream then stalled or the connection dropped
                                if _last_finish_reason:
                                    logger.warning("HTTPX stream: finish arrived, then %r: %s", error, self.model)
                                    break
                                if isinstance(error, httpx.TransportError):
                                    raise
                                logger.warning(f"HTTPX stream chunk timeout after {chunk_timeout}s")
                                raise httpx.RemoteProtocolError(f"Stream stalled - no data for {chunk_timeout}s")
                            
                            # Process complete lines from buffer
                            while '\n' in line_buffer:
                                line, line_buffer = line_buffer.split('\n', 1)
                                line = line.strip()
                                
                                if not line or not line.startswith("data: "):
                                    continue
                                last_event = time.monotonic()

                                data = line[6:]  # Remove "data: " prefix

                                if data == "[DONE]":
                                    # Stream finished - yield final result
                                    assistant = {
                                        "role": "assistant",
                                        "content": "".join(accumulated_content) if accumulated_content else ""
                                    }

                                    # Add the thinking text if any — but store
                                    # it ONCE. OpenRouter sends the same text
                                    # as ``reasoning`` deltas AND inside the
                                    # reasoning_details blocks below, which
                                    # have to be kept verbatim for the replay;
                                    # a copy here would double every thought in
                                    # the session. Readers ask
                                    # ``reasoning_artifacts.thinking_text``,
                                    # and the strip functions rescue the text
                                    # before the artifacts are dropped.
                                    _thinking = "".join(accumulated_reasoning)
                                    if _thinking and not self._reasoning_text_from_details(
                                            accumulated_reasoning_details):
                                        assistant["reasoning_content"] = _thinking

                                    # Add tool calls if any
                                    if accumulated_tool_calls:
                                        tool_calls_list = [accumulated_tool_calls[idx] for idx in sorted(accumulated_tool_calls.keys())]
                                        assistant["tool_calls"] = tool_calls_list
                                    if accumulated_reasoning_details:
                                        assistant["reasoning_details"] = [
                                            accumulated_reasoning_details[idx]
                                            for idx in sorted(accumulated_reasoning_details.keys())
                                        ]
                                    if accumulated_routing and accumulated_routing.get("selected"):
                                        assistant["served_by"] = accumulated_routing["selected"]

                                    final_result = {"assistant": assistant}

                                    # Add usage if available
                                    if accumulated_usage:
                                        final_result["usage"] = accumulated_usage
                                    # Carry finish_reason to the caller, not just to
                                    # the hook: "length" means the answer was CUT OFF,
                                    # which the agent loop must not mistake for an
                                    # empty response (a model that spends its whole
                                    # budget on reasoning returns no content at all).
                                    if _last_finish_reason:
                                        final_result["finish_reason"] = _last_finish_reason

                                    # Notify post-response hook for streaming
                                    _s_duration = (_time.time() - _streaming_request_start) * 1000
                                    await self._notify_post_response({
                                        "provider": "openai_httpx", "model": self.model,
                                        "url": url, "is_streaming": True,
                                        "duration_ms": _s_duration,
                                        "usage": accumulated_usage,
                                        "routing": accumulated_routing,
                                        "finish_reason": _last_finish_reason,
                                        "timestamp_ms": _time.time() * 1000,
                                    })

                                    yield {"type": "final", **final_result}
                                    return  # Success - exit retry loop

                                try:
                                    chunk_data = json.loads(data)
                                except Exception:
                                    logger.debug(f"Failed to parse chunk data: {data[:100]}")
                                    continue

                                # Upstream-error chunk (e.g. OpenRouter wrapping a
                                # provider 429 as a body-error inside SSE). Signal
                                # the outer attempt loop to retry and break out of
                                # the chunk parser cleanly.
                                _body_err_msg = self._detect_body_429(chunk_data)
                                if _body_err_msg:
                                    _body_429_retry_msg = _body_err_msg
                                    break  # exit "while '\n' in line_buffer"

                                # Body-level 400 (Gemini cross-backend signature
                                # mismatch) - mirror of the non-streaming path.
                                # Check the OpenAI encrypted-reasoning case FIRST
                                # (more specific) so the generic signature detector
                                # doesn't claim it.
                                if _enc_retries < 2:
                                    _enc_err = self._detect_openai_encrypted_reasoning_400(chunk_data)
                                    if _enc_err:
                                        _body_400_enc_retry_msg = _enc_err
                                        break
                                if not _sig_retried:
                                    _sig_err = self._detect_body_400_signature_issue(chunk_data)
                                    if _sig_err:
                                        _body_400_sig_retry_msg = _sig_err
                                        break

                                # Track usage if available in chunk
                                if "usage" in chunk_data:
                                    accumulated_usage = chunk_data["usage"]
                                _routing = openrouter_routing_info(chunk_data)
                                if _routing and (accumulated_routing is None
                                                 or "available" in _routing):
                                    accumulated_routing = _routing

                                # Process chunk
                                choices = chunk_data.get("choices", [])
                                if not choices:
                                    continue

                                choice = choices[0]
                                delta = choice.get("delta", {})

                                # Track finish_reason from chunks
                                _fr = choice.get("finish_reason")
                                if _fr:
                                    _last_finish_reason = _fr

                                # Handle the thinking delta. DeepSeek calls the
                                # field reasoning_content, OpenRouter calls it
                                # reasoning — same payload, and only ever one of
                                # them arrives. Reading just the first name made
                                # every OpenRouter thinking model look silent.
                                # This comes BEFORE the actual content in thinking models
                                _reasoning_delta = (delta.get("reasoning_content")
                                                    or delta.get("reasoning"))
                                if _reasoning_delta:
                                    accumulated_reasoning.append(_reasoning_delta)
                                    yield {
                                        "type": "thinking_delta",
                                        "delta": _reasoning_delta,
                                        "accumulated": "".join(accumulated_reasoning)
                                    }

                                # Capture reasoning_details verbatim (Gemini 3.x thought_signature).
                                # OpenRouter delivers the encrypted signature here keyed by index;
                                # required on round-trip or upstream returns MALFORMED_FUNCTION_CALL.
                                if "reasoning_details" in delta and delta["reasoning_details"]:
                                    for rd in delta["reasoning_details"]:
                                        self._accumulate_reasoning_detail(
                                            accumulated_reasoning_details, rd)

                                # Handle content delta
                                if "content" in delta and delta["content"]:
                                    accumulated_content.append(delta["content"])
                                    yield {
                                        "type": "content_delta",
                                        "delta": delta["content"],
                                        "accumulated": "".join(accumulated_content)
                                    }

                                # Handle tool call deltas
                                if "tool_calls" in delta:
                                    for tc_delta in delta["tool_calls"]:
                                        index = self._accumulate_tool_call_delta(
                                            accumulated_tool_calls, tc_delta)

                                        # Yield delta with accumulated state
                                        yield {
                                            "type": "tool_call_delta",
                                            "index": index,
                                            "delta": tc_delta,
                                            "accumulated": accumulated_tool_calls[index]
                                        }

                            # Body-429 signaled from inside chunk parser — abort
                            # the chunk-fetching loop so the outer attempt loop
                            # can apply backoff and retry the whole request.
                            if _body_429_retry_msg:
                                break  # exits the outer "while True" chunk fetcher
                            # Body-400 signature-bypass signaled — same pattern.
                            if _body_400_sig_retry_msg:
                                break
                            # Body-400 encrypted-reasoning signaled — same pattern.
                            if _body_400_enc_retry_msg:
                                break
                            if silence_limit and time.monotonic() - last_event > silence_limit:
                                if _last_finish_reason:
                                    break  # the answer is complete; only [DONE] is missing
                                logger.warning(f"HTTPX stream: only keep-alives for {silence_limit:g}s: {self.model}")
                                raise httpx.RemoteProtocolError(
                                    f"Stream stalled - no event for {silence_limit:g}s, only keep-alives")

                        # A body-level retry was signaled from the chunk
                        # parser. Skip the finalization tail entirely --
                        # falling through to it yielded an EMPTY final
                        # answer and returned, which made the retry
                        # handlers behind the `finally` dead code (an
                        # upstream 429 became a silent empty response).
                        if not (_body_429_retry_msg or _body_400_sig_retry_msg
                                or _body_400_enc_retry_msg):
                            # After stream ends, process any remaining data in buffer
                            # This handles the case where the last chunk doesn't end with \n
                            # or where [DONE] is in the buffer but wasn't processed yet
                            if line_buffer.strip():
                                for line in line_buffer.split('\n'):
                                    line = line.strip()
                                    if not line or not line.startswith("data: "):
                                        continue
                                    data = line[6:]
                                    if data == "[DONE]":
                                        # Found [DONE] in remaining buffer
                                        assistant = {
                                            "role": "assistant",
                                            "content": "".join(accumulated_content) if accumulated_content else ""
                                        }
                                        # One home for the text — see the main
                                        # loop above.
                                        _thinking = "".join(accumulated_reasoning)
                                        if _thinking and not self._reasoning_text_from_details(
                                                accumulated_reasoning_details):
                                            assistant["reasoning_content"] = _thinking
                                        if accumulated_tool_calls:
                                            tool_calls_list = [accumulated_tool_calls[idx] for idx in sorted(accumulated_tool_calls.keys())]
                                            assistant["tool_calls"] = tool_calls_list
                                        if accumulated_reasoning_details:
                                            assistant["reasoning_details"] = [
                                                accumulated_reasoning_details[idx]
                                                for idx in sorted(accumulated_reasoning_details.keys())
                                            ]
                                        if accumulated_routing and accumulated_routing.get("selected"):
                                            assistant["served_by"] = accumulated_routing["selected"]
                                        final_result = {"assistant": assistant}
                                        if accumulated_usage:
                                            final_result["usage"] = accumulated_usage
                                        if _last_finish_reason:  # see note above
                                            final_result["finish_reason"] = _last_finish_reason
                                        _s_duration = (_time.time() - _streaming_request_start) * 1000
                                        await self._notify_post_response({
                                            "provider": "openai_httpx", "model": self.model,
                                            "url": url, "is_streaming": True,
                                            "duration_ms": _s_duration, "usage": accumulated_usage,
                                            "routing": accumulated_routing,
                                            "finish_reason": _last_finish_reason,
                                            "timestamp_ms": _time.time() * 1000,
                                        })
                                        yield {"type": "final", **final_result}
                                        return
                                    # Try to parse remaining JSON chunks
                                    try:
                                        chunk_data = json.loads(data)
                                        if "usage" in chunk_data:
                                            accumulated_usage = chunk_data["usage"]
                                        _routing = openrouter_routing_info(chunk_data)
                                        if _routing and (accumulated_routing is None
                                                         or "available" in _routing):
                                            accumulated_routing = _routing
                                        choices = chunk_data.get("choices", [])
                                        if choices:
                                            delta = choices[0].get("delta", {})
                                            _reasoning_delta = (
                                                delta.get("reasoning_content")
                                                or delta.get("reasoning"))
                                            if _reasoning_delta:
                                                accumulated_reasoning.append(_reasoning_delta)
                                            if "content" in delta and delta["content"]:
                                                accumulated_content.append(delta["content"])
                                            # Same accumulation as the main loop —
                                            # fragments that only arrive in the
                                            # unterminated rest buffer must not
                                            # be dropped (lost signature / broken
                                            # tool-call args otherwise).
                                            if "reasoning_details" in delta and delta["reasoning_details"]:
                                                for rd in delta["reasoning_details"]:
                                                    self._accumulate_reasoning_detail(
                                                        accumulated_reasoning_details, rd)
                                            if "tool_calls" in delta:
                                                for tc_delta in delta["tool_calls"]:
                                                    self._accumulate_tool_call_delta(
                                                        accumulated_tool_calls, tc_delta)
                                    except json.JSONDecodeError:
                                        # EXPECTED here: this is the salvage pass
                                        # over an unterminated buffer, so a partial
                                        # JSON tail is the normal case. Narrow on
                                        # purpose — a TypeError/KeyError from the
                                        # accumulators would be a real bug, and
                                        # `except Exception` used to bury it in a
                                        # path that is already degraded.
                                        pass
                                    except Exception as _salvage_error:
                                        logger.warning(
                                            "Rest-buffer salvage failed on a "
                                            "well-formed chunk (%s): %s",
                                            type(_salvage_error).__name__,
                                            _salvage_error)


                            # Stream ended without [DONE] - yield final result anyway
                            # This can happen with some API implementations
                            logger.warning("Stream ended without [DONE] marker, yielding accumulated content")
                            # ...but say so. Without a marker we cannot tell a
                            # complete answer from one cut off mid-generation, and
                            # accepting the latter as final is silent truncation.
                            if not _last_finish_reason:
                                _last_finish_reason = "incomplete_stream"
                            assistant: dict[str, Any] = {
                                "role": "assistant",
                                "content": "".join(accumulated_content) if accumulated_content else ""
                            }
                            # One home for the text — see the main loop above.
                            _thinking = "".join(accumulated_reasoning)
                            if _thinking and not self._reasoning_text_from_details(
                                    accumulated_reasoning_details):
                                assistant["reasoning_content"] = _thinking
                            if accumulated_tool_calls:
                                tool_calls_list = [accumulated_tool_calls[idx] for idx in sorted(accumulated_tool_calls.keys())]
                                assistant["tool_calls"] = tool_calls_list
                            if accumulated_reasoning_details:
                                assistant["reasoning_details"] = [
                                    accumulated_reasoning_details[idx]
                                    for idx in sorted(accumulated_reasoning_details.keys())
                                ]
                            if accumulated_routing and accumulated_routing.get("selected"):
                                assistant["served_by"] = accumulated_routing["selected"]
                            final_result = {"assistant": assistant}
                            if accumulated_usage:
                                final_result["usage"] = accumulated_usage
                            if _last_finish_reason:
                                final_result["finish_reason"] = _last_finish_reason
                            _s_duration = (_time.time() - _streaming_request_start) * 1000
                            await self._notify_post_response({
                                "provider": "openai_httpx", "model": self.model,
                                "url": url, "is_streaming": True,
                                "duration_ms": _s_duration, "usage": accumulated_usage,
                                "routing": accumulated_routing,
                                "finish_reason": _last_finish_reason,
                                "timestamp_ms": _time.time() * 1000,
                            })
                            yield {"type": "final", **final_result}
                            return  # Success - exit retry loop
                finally:
                    # Safely close client with timeout to avoid SSL shutdown segfaults
                    # This is critical on Linux with OpenSSL 3.x where SSL_shutdown can hang
                    try:
                        await asyncio.wait_for(client.aclose(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning("Client close timed out, forcing close")
                        # Force close without waiting for SSL shutdown
                        try:
                            await asyncio.shield(asyncio.sleep(0))  # Give event loop a tick
                        except Exception:
                            pass
                    except Exception as close_err:
                        logger.debug(f"Error during client close (ignored): {close_err}")

                # Body-level 400 signature-bypass retry for streaming (mirrors
                # the non-streaming path). Flag was set inside the SSE chunk
                # parser; inject the bypass token here and retry the same
                # attempt slot. One-shot per request via _sig_retried.
                if _body_400_sig_retry_msg and not _sig_retried:
                    n_patched = self._inject_signature_bypass(payload)
                    if n_patched > 0:
                        _sig_retried = True
                        logger.warning(
                            "Body-400 stream retry: injecting signature bypass "
                            "token into reasoning_details (likely cross-backend "
                            "Vertex<->AI Studio routing mismatch). model=%s "
                            "patched_blocks=%d detail=%r",
                            self.model, n_patched, _body_400_sig_retry_msg[:200],
                        )
                        await self._report_status(
                            status_scope,
                            f"Signature bypass retry: {self.model}",
                        )
                        await self._notify_retry(
                            "openai_httpx", self.model, url, True,
                            "body-400 signature bypass (stream)",
                            attempt, self.max_retries + 1,
                        )
                        continue

                # Body-level 400 OpenAI encrypted-reasoning retry for streaming
                # (mirrors the non-streaming path). Two-stage recovery, heals
                # payload AND original session messages.
                if _body_400_enc_retry_msg and _enc_retries < 2:
                    recovery = self._recover_encrypted_reasoning(
                        _body_400_enc_retry_msg, payload, messages, _enc_retries)
                    if recovery:
                        _enc_retries += 1
                        logger.warning(
                            "Body-400 stream retry: %s (defective encrypted "
                            "reasoning item). model=%s detail=%r",
                            recovery, self.model, _body_400_enc_retry_msg[:500],
                        )
                        await self._report_status(
                            status_scope,
                            f"Encrypted-reasoning retry: {self.model}",
                        )
                        await self._notify_retry(
                            "openai_httpx", self.model, url, True,
                            "body-400 encrypted-reasoning strip (stream)",
                            attempt, self.max_retries + 1,
                        )
                        continue

                # Body-level upstream 429 retry for streaming (mirrors the
                # non-streaming path). The flag was set inside the SSE chunk
                # parser; here we apply backoff and drop service_tier from the
                # *local* payload (flex -> standard) on the first 429 so the
                # retry tries the standard tier - without mutating
                # self.service_tier (singleton-safe).
                if _body_429_retry_msg and attempt < self.rate_limit_max_retries:
                    base = self.rate_limit_backoff * (1.5 ** attempt)
                    jitter = base * random.uniform(0.0, 0.5)
                    backoff_time = base + jitter
                    tier_note = ""
                    if payload.get("service_tier"):
                        dropped = payload.pop("service_tier")
                        tier_note = f", dropping service_tier={dropped!r}"
                    logger.warning(
                        f"Upstream 429 in stream chunk ({_body_429_retry_msg[:80]}), "
                        f"retrying in {backoff_time:.0f}s "
                        f"(attempt {attempt + 1}/{self.rate_limit_max_retries}){tier_note}"
                    )
                    await self._report_status(
                        status_scope,
                        f"Upstream 429, retry {attempt + 1}/{self.rate_limit_max_retries}: {self.model} (wait {backoff_time:.0f}s)"
                    )
                    await self._notify_retry(
                        "openai_httpx", self.model, url, True,
                        f"Upstream 429 body-error{tier_note}",
                        attempt, self.rate_limit_max_retries + 1,
                    )
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                    continue

                # Retries exhausted (or one-shot recovery already spent):
                # raise instead of silently starting another attempt.
                if _body_429_retry_msg:
                    raise LLMRateLimitError(
                        f"Upstream 429 body-error after retries: {_body_429_retry_msg[:200]}",
                        provider="openai_httpx", model=self.model,
                    )
                if _body_400_sig_retry_msg or _body_400_enc_retry_msg:
                    _msg = _body_400_sig_retry_msg or _body_400_enc_retry_msg
                    raise Exception(
                        f"Unrecoverable body-400 in stream: {_msg[:300]}"
                    )

            except asyncio.CancelledError:
                # Re-raise cancellation without wrapping
                logger.info("HTTPX streaming request cancelled by user")
                raise

            except httpx.TimeoutException as e:
                last_exception = e
                if attempt < self.max_retries:
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Request timeout, retrying in {backoff_time}s: {e}")
                    await self._report_status(status_scope, f"Timeout, retry {attempt + 1}/{self.max_retries}: {self.model}")
                    await self._notify_retry("openai_httpx", self.model, url, True, f"Timeout: {e}", attempt, self.max_retries + 1)
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                    continue
                else:
                    logger.error(f"Request timed out after {self.max_retries + 1} attempts: {e}")
                    await self._report_status(status_scope, f"Timeout after retries: {self.model}")
                    raise LLMConnectionError(
                        f"Request timed out: {e}", provider="openai_httpx", model=self.model,
                    ) from e

            except httpx.HTTPStatusError as e:
                last_exception = e  # type: ignore[assignment]  # Can be HTTPStatusError, TimeoutException, or NetworkError
                if e.response.status_code >= 500 and attempt < self.max_retries:
                    # Server error - retry
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Server error {e.response.status_code}, retrying in {backoff_time}s")
                    await self._report_status(status_scope, f"Server error, retry {attempt + 1}/{self.max_retries}: {self.model}")
                    await self._notify_retry("openai_httpx", self.model, url, True, f"Server error ({e.response.status_code})", attempt, self.max_retries + 1)
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                    continue
                else:
                    # Client error or max retries exceeded
                    # Error message already in exception (we read it before raising in streaming mode)
                    error_msg = str(e)
                    logger.error(f"HTTP error (streaming): {error_msg}")
                    await self._report_status(status_scope, f"HTTP error: {self.model}")
                    raise Exception(error_msg) from e

            except (httpx.NetworkError, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                last_exception = e  # type: ignore[assignment]  # Multiple exception types possible
                if attempt < self.max_retries:
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Network/protocol error (stream interrupted), retrying in {backoff_time}s: {e}")
                    await self._report_status(status_scope, f"Network error, retry {attempt + 1}/{self.max_retries}: {self.model}")
                    await self._notify_retry("openai_httpx", self.model, url, True, f"Network error: {e}", attempt, self.max_retries + 1)
                    await self._cancellable_sleep(backoff_time, cancellation_token)
                    continue
                else:
                    logger.error(f"Network/protocol error after {self.max_retries + 1} attempts: {e}")
                    await self._report_status(status_scope, f"Network error after retries: {self.model}")
                    raise LLMConnectionError(
                        f"Network/protocol error: {e}", provider="openai_httpx", model=self.model,
                    ) from e

        # Should never reach here, but just in case
        raise Exception(f"Request failed after {self.max_retries + 1} attempts") from last_exception

    def _parse_retry_after(self, retry_after: Optional[str]) -> Optional[float]:
        """Parse Retry-After header value."""
        if not retry_after:
            return None

        try:
            # Can be seconds or HTTP date, we only handle seconds for simplicity
            return float(retry_after)
        except ValueError:
            return None

    def _parse_error_response(self, response: httpx.Response) -> str:
        """Extract error message from HTTP error response."""
        try:
            error_data = response.json()
            if "error" in error_data:
                error_info = error_data["error"]
                if isinstance(error_info, dict):
                    return error_info.get("message", f"HTTP {response.status_code}")
                else:
                    return str(error_info)
        except Exception:
            pass

        return f"HTTP {response.status_code}: {response.text[:200]}"

    def _is_gemini_malformed_response(self, response_data: dict) -> bool:
        """Check if a Gemini response contains a MALFORMED_FUNCTION_CALL error.

        This is a transient, non-deterministic Gemini model error where the model
        fails to generate valid function call JSON. Identical payloads can succeed
        or fail randomly. The response comes as HTTP 200 with finish_reason="error"
        and native_finish_reason="MALFORMED_FUNCTION_CALL", with no usable content
        or tool_calls.

        Returns True only when the response is truly unusable (no tool_calls present).
        If tool_calls ARE present despite the error, returns False so they can be used.

        Decided on the ANSWER's shape, not on the model: an endpoint that never
        produces this native_finish_reason never trips the detector.
        """
        choices = response_data.get("choices", [])
        if not choices:
            return False

        choice = choices[0]
        native_reason = choice.get("native_finish_reason", "")
        if native_reason != "MALFORMED_FUNCTION_CALL":
            return False

        # If tool_calls are present despite the error, they're usually usable
        message = choice.get("message", {})
        if message.get("tool_calls"):
            return False

        return True

    # Pattern Gemini emits when its function-call decoder leaks its internal
    # representation into the text channel instead of the structured tool_calls
    # field. Looks like  `call:default_api:NAME{key:value,key:value}` —
    # unquoted keys/values, curly-brace block. Captured 2026-06-14 in
    # cover_artist session mncq6pm209 where the response had finish_reason=stop
    # (NOT MALFORMED), no tool_calls, and the would-be call as content. The
    # agent loop then treated the response as a final text answer and stopped.
    _GEMINI_INTERNAL_CALL_RE = re.compile(r"^\s*call:default_api:\w+\s*\{")

    def _is_gemini_internal_format_leak(self, response_data: dict) -> bool:
        """True when Gemini emitted a function call as plain-text content.

        Detection criteria (the answer's shape, not the model's name):
          - response has no tool_calls (otherwise the call worked)
          - message content starts with the `call:default_api:NAME{` marker

        Unlike the MALFORMED case the finish_reason here is usually "stop" —
        Gemini sees its own text as a valid completion. Without this detection
        the agent loop accepts an empty assistant turn and treats it as final.

        Only where the endpoint speaks that dialect: the marker is plain text,
        and a model WRITING about this bug would otherwise lose its answer to a
        retry.
        """
        if self.tool_schema_dialect != DIALECT_GEMINI_FUNCTION_DECLARATIONS:
            return False
        choices = response_data.get("choices", [])
        if not choices:
            return False

        choice = choices[0]
        message = choice.get("message", {})
        if message.get("tool_calls"):
            return False

        content = message.get("content")
        if not isinstance(content, str) or not content:
            return False

        return bool(self._GEMINI_INTERNAL_CALL_RE.match(content))

    @staticmethod
    def _detect_body_429(response_data: dict) -> Optional[str]:
        """Detect upstream-429 returned as body-error in an HTTP 200 response.

        OpenRouter (and similar proxies) frequently wrap upstream provider
        rate-limits in a normal-looking HTTP 200 response with payload shape:
            {"error": {"code": 429, "message": "...too many requests..."}}

        Returns the upstream error message when detected (truthy for callers),
        or None otherwise. Treated separately from HTTP-status 429 because
        httpx's status-based retry path doesn't see body-level errors.
        """
        if not isinstance(response_data, dict):
            return None
        err = response_data.get("error")
        if not isinstance(err, dict):
            return None
        code = str(err.get("code", ""))
        msg = str(err.get("message", ""))
        lowered = msg.lower()
        is_429 = (
            code == "429"
            or "429" in code
            # Concrete rate-limit markers only. A bare "rate" substring also
            # matched "generate"/"moderate" and sent deterministic upstream
            # errors into minutes of pointless 429 backoff.
            or "rate limit" in lowered
            or "rate_limit" in lowered
            or "rate-limit" in lowered
            or "too many requests" in lowered
        )
        return msg if is_429 else None

    @staticmethod
    def _detect_body_400_signature_issue(response_data: dict) -> Optional[str]:
        """Detect a body-level 400 that LIKELY indicates a Gemini thought-signature
        cross-backend mismatch.

        Background: For Gemini via OpenRouter, OR routes requests between Vertex
        and AI Studio backends (per provider_routing.order with allow_fallbacks=true).
        Thought signatures are encrypted blobs keyed to the signing backend - Vertex
        cannot decrypt AI Studio's signatures and vice versa. When OR routes a
        follow-up request to a different backend than the one that signed the last
        assistant turn, Google rejects with "Corrupted thought signature" 400.
        Vertex is strict; AI Studio is lenient and tolerates absence.

        Detection is permissive on purpose: OR often masks the underlying Google
        error to a generic "Provider returned error". We therefore treat ANY
        body-level 400 as a candidate for the signature-strip retry, gated by the
        actual presence of reasoning_details in the payload (otherwise stripping
        does nothing).

        Returns the underlying error string (truthy) for any body-level 400, or
        None for non-400 bodies. The caller decides whether to retry based on
        whether the payload still has reasoning_details to strip.
        """
        if not isinstance(response_data, dict):
            return None
        err = response_data.get("error")
        if not isinstance(err, dict):
            return None
        code = str(err.get("code", ""))
        if code != "400" and "400" not in code:
            return None
        msg = err.get("message", "")
        meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
        raw = meta.get("raw") if isinstance(meta, dict) else None
        # Compose a useful description for logging
        if raw:
            return f"{msg} | raw={raw[:300]}"
        return msg or "Provider returned 400"

    # Google-documented bypass token. Recognized by both Vertex AI (strict)
    # and AI Studio (lenient) as a signal to skip thought_signature validation.
    # Used in reactive recovery when OpenRouter's translation layer or its
    # backend-routing switch (Vertex <-> AI Studio) has produced a signature
    # the target backend can't validate. AI Studio also accepts
    # "context_engineering_is_the_way_to_go" but Vertex does not - so we use
    # the universal one.
    _GEMINI_SIGNATURE_BYPASS = "skip_thought_signature_validator"

    @classmethod
    def _inject_signature_bypass(cls, payload: dict) -> int:
        """Replace the `data` field of every `reasoning.encrypted` block in the
        payload's assistant messages with Google's documented bypass token.

        Returns the number of blocks that were patched. Used by the reactive
        retry on body-level 400 - the structural shape of reasoning_details is
        preserved (type, format, id, index untouched) so OpenRouter still
        translates each block into a Google `thoughtSignature` part, but the
        target backend now sees the bypass token instead of a cross-backend
        signature it can't decrypt.

        Note: only `reasoning.encrypted` blocks carry signatures. `reasoning.text`
        blocks are left alone - they're descriptive text, not signed material.
        """
        patched = 0
        for msg in payload.get("messages") or []:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            rds = msg.get("reasoning_details")
            if not rds:
                continue
            for block in rds:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "reasoning.encrypted":
                    continue
                if block.get("data") == cls._GEMINI_SIGNATURE_BYPASS:
                    continue  # already patched
                block["data"] = cls._GEMINI_SIGNATURE_BYPASS
                patched += 1
        return patched

    @staticmethod
    def _detect_openai_encrypted_reasoning_400(response_data: dict) -> Optional[str]:
        """Detect a body-level 400 caused by an unverifiable OpenAI reasoning item.

        OpenAI reasoning models (GPT-5.x, o-series) return reasoning items whose
        ``encrypted_content`` must be round-tripped intact. The provider rejects
        a request with

            "The encrypted content for item rs_… could not be verified.
             Reason: Encrypted content could not be decrypted or parsed."

        Verified root cause (by replaying captured payloads against a pinned
        provider): the blob named in the error is DEFECTIVE AS DELIVERED by
        OpenRouter's Responses→Chat-Completions bridge — observed on assistant
        turns with multiple parallel tool_calls. Byte-identical round-tripping
        cannot heal it, and the rejection is deterministic; all OTHER items of
        the chain keep verifying once the named one is dropped. Backend/org
        switching (an outage-forced reroute breaking the org-bound encryption)
        is the residual second cause.

        Unlike Gemini's thought signatures there is NO bypass token. Recovery is
        ``_recover_encrypted_reasoning``: drop the named item (payload AND
        session), escalate to a full strip only if a second 400 follows.

        Returns the underlying error string for a matching 400, else None.
        """
        if not isinstance(response_data, dict):
            return None
        err = response_data.get("error")
        if not isinstance(err, dict):
            return None
        code = str(err.get("code", ""))
        if code != "400" and "400" not in code:
            return None
        msg = str(err.get("message", ""))
        meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
        raw = str(meta.get("raw", "")) if isinstance(meta, dict) else ""
        haystack = f"{msg} {raw}"
        # Signature of the OpenAI encrypted-reasoning failure. "rs_" is the
        # reasoning-item id prefix; the phrase appears verbatim in the upstream
        # error OpenRouter forwards.
        if "encrypted content" in haystack and ("rs_" in haystack or "reasoning" in haystack.lower()):
            return (f"{msg} | raw={raw[:300]}") if raw else (msg or "encrypted reasoning 400")
        return None

    @staticmethod
    def _strip_reasoning_details(payload: dict, item_id: Optional[str] = None) -> int:
        """Remove ``reasoning_details`` from assistant messages in *payload*.

        Recovery for ``_detect_openai_encrypted_reasoning_400``. With *item_id*,
        only the message(s) carrying a block with that id are stripped (the
        surgical path — the rest of the chain keeps verifying); without it,
        every assistant message is stripped (escalation).

        Returns the number of messages that had reasoning_details removed.
        """
        stripped = 0
        for msg in payload.get("messages") or []:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            if item_id is not None:
                blocks = msg.get("reasoning_details") or []
                if not any(isinstance(b, dict) and b.get("id") == item_id for b in blocks):
                    continue
            if msg.pop("reasoning_details", None) is not None:
                stripped += 1
        return stripped

    def _recover_encrypted_reasoning(
        self, detail: str, payload: dict, messages: list, enc_retries: int
    ) -> Optional[str]:
        """Two-stage recovery for the encrypted-reasoning 400.

        Root cause (verified empirically by replaying captured payloads with a
        pinned provider): OpenRouter's Responses→Chat-Completions bridge can
        deliver a DEFECTIVE encrypted blob for assistant turns with multiple
        parallel tool_calls — the provider then rejects it deterministically
        ("could not be decrypted or parsed") even though we round-trip it
        byte-identically, while every other item of the chain still verifies.

        Stage 0 (surgical): drop ONLY the rs_* item named in the error — from
        the request payload AND the original session messages, so the healing
        persists. Chain, prompt cache and reasoning continuity of the healthy
        turns survive.
        Stage 1 (escalation): full strip of all reasoning artifacts, payload +
        session. The model re-reasons once and rebuilds a fresh chain.

        Returns a short description of the applied recovery for logging, or
        None when no further recovery is available.
        """
        if enc_retries == 0:
            m = re.search(r"\brs_[A-Za-z0-9]+", detail or "")
            if m:
                item_id = m.group(0)
                n_payload = self._strip_reasoning_details(payload, item_id=item_id)
                n_session = strip_reasoning_artifacts_containing(messages, item_id)
                if n_payload or n_session:
                    return (f"targeted strip of {item_id} "
                            f"(payload={n_payload}, session={n_session} msgs)")
            # item id absent or not found in our messages -> escalate directly
        if enc_retries <= 1:
            n_payload = self._strip_reasoning_details(payload)
            n_session = strip_all_reasoning_artifacts(messages)
            if n_payload or n_session:
                return f"full strip (payload={n_payload}, session={n_session} msgs)"
        return None

    def _format_response(self, response_data: dict) -> dict:
        """Format OpenAI API response to our standard format."""
        try:
            logger.debug(f"Formatting response_data keys: {list(response_data.keys())}")

            # Check for provider-level errors returned inside a 200 response body
            # (e.g. OpenRouter proxying upstream errors from Qwen/Alibaba, Gemini, etc.)
            if "error" in response_data:
                error_info = response_data["error"]
                error_msg = error_info.get("message", "Unknown upstream error") if isinstance(error_info, dict) else str(error_info)
                error_code = error_info.get("code", "unknown") if isinstance(error_info, dict) else "unknown"
                logger.warning(f"Provider returned error in response body: [{error_code}] {error_msg}")
                return {"assistant": {"role": "assistant", "content": "", "error": {"message": error_msg, "type": f"upstream_error_{error_code}"}}}

            choices = response_data.get("choices", [])
            if not choices:
                return {"assistant": {"role": "assistant", "content": ""}}

            choice = choices[0]
            message = choice.get("message", {})

            # Log finish_reason for debugging (Gemini MALFORMED_FUNCTION_CALL shows up here)
            finish_reason = choice.get("finish_reason")
            tool_calls = message.get("tool_calls")

            if finish_reason and finish_reason not in ("stop", "tool_calls", "end_turn"):
                # Gemini sometimes returns MALFORMED_FUNCTION_CALL but still includes
                # valid tool_calls in the response. This is a known Gemini output bug
                # where the model partially fails to generate one of several function
                # calls. If we got usable tool_calls, log at debug and continue normally.
                native_reason = choice.get("native_finish_reason", "")
                if tool_calls:
                    tc_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                    logger.debug(
                        f"Non-standard finish_reason '{finish_reason}' "
                        f"(native: {native_reason}) but {len(tool_calls)} tool_calls "
                        f"present — using them (model={self.model}): {tc_names}"
                    )
                else:
                    logger.warning(
                        f"Non-standard finish_reason: {finish_reason} "
                        f"(native: {native_reason}, model={self.model})"
                    )
                    import json as _json
                    try:
                        choice_str = _json.dumps(choice, ensure_ascii=False, default=str)[:2000]
                        logger.warning(f"Error response choice data: {choice_str}")
                    except Exception:
                        logger.warning(f"Error response choice (raw): {choice}")

                    # Content-filter block (Gemini PROHIBITED_CONTENT etc.) — surface
                    # as upstream-body-error so the server-side fallback-profile
                    # mechanism switches to the llm_profile fallback chain. Retrying the same
                    # model is pointless: the filter is deterministic per content,
                    # not transient like MALFORMED_FUNCTION_CALL.
                    if finish_reason == "content_filter":
                        native = native_reason or "content_filter"
                        return {
                            "assistant": {
                                "role": "assistant",
                                "content": "",
                                "error": {
                                    "message": (
                                        f"Provider content filter blocked response "
                                        f"({native}, model={self.model})"
                                    ),
                                    "type": f"content_filter_{native.lower()}",
                                },
                            }
                        }

            # Build assistant response
            assistant = {
                "role": "assistant",
                "content": message.get("content", "") or ""
            }

            # Add the thinking text if present. Three names, one payload:
            # DeepSeek sends reasoning_content, OpenRouter sends reasoning, and
            # both may carry it in reasoning_details[].text. Only reading the
            # first name meant DeepSeek's own contract below
            # (_postprocess_messages_for_provider fills in "") could never be
            # satisfied over OpenRouter.
            reasoning_content = (message.get("reasoning_content")
                                 or message.get("reasoning"))
            # ...but only when the artifacts do not already carry the same
            # text. OpenRouter sends both, and storing each would keep every
            # thought twice in the session. Readers ask
            # ``reasoning_artifacts.thinking_text``, which knows both homes.
            if reasoning_content and not self._reasoning_text_from_details(
                    message.get("reasoning_details")):
                assistant["reasoning_content"] = reasoning_content

            # Add tool calls if present — sanitize to standard fields only.
            # Note: tool_calls was already extracted above for finish_reason handling.
            if tool_calls:
                assistant["tool_calls"] = HTTPXOpenAIClient._sanitize_tool_calls(tool_calls)

            # Capture reasoning_details verbatim. For Gemini 3.x via OpenRouter
            # this carries the encrypted thought_signature that MUST be sent back
            # on subsequent turns or Google returns MALFORMED_FUNCTION_CALL.
            reasoning_details = message.get("reasoning_details")
            if reasoning_details:
                assistant["reasoning_details"] = reasoning_details

            # Which backend answered: the next request of this run goes back
            # to it (routing_pinned_to_last_backend).
            served_by = (openrouter_routing_info(response_data) or {}).get("selected")
            if served_by:
                assistant["served_by"] = served_by

            # Track usage if available
            usage = response_data.get("usage", {})
            logger.debug(f"Extracted usage from response_data: {usage}")

            result = {"assistant": assistant}

            if usage:
                # Pass through complete usage data (OpenAI may include additional details like cached_tokens, reasoning_tokens etc.)
                result["usage"] = usage
                logger.debug(f"Added usage to result: {result['usage']}")
            else:
                logger.debug("No usage data in response_data")

            # Same contract as the streaming paths: the caller needs to tell a
            # truncated answer ("length") from an empty one.
            if finish_reason:
                result["finish_reason"] = finish_reason

            return result

        except Exception as e:
            logger.error(f"Failed to format response: {e}, raw data: {response_data}")
            # An empty content with no error marker is indistinguishable from
            # "the model said nothing": the agent loop then treats a CLIENT
            # bug (an unexpected response shape from a new gateway backend) as
            # a content problem — retrying the same model, writing an issue,
            # never reaching the fallback chain. Same shape the content_filter
            # branch above uses, and the SDK client's error path.
            return {
                "assistant": {
                    "role": "assistant",
                    "content": "",
                    "error": {
                        "message": (f"Could not parse the provider response "
                                    f"({type(e).__name__}: {e}, "
                                    f"model={self.model})"),
                        "type": "response_format_error",
                    },
                }
            }


# Factory function for easy integration
def create_httpx_openai_client(
    model: str,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    verify: Optional[bool] = None,
    **kwargs
) -> HTTPXOpenAIClient:
    """Create HTTPX-based OpenAI client with sensible defaults."""
    return HTTPXOpenAIClient(
        model=model,
        api_key=api_key,
        base_url=base_url,
        verify=verify,
        **kwargs
    )