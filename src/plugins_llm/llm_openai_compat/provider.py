"""Provider factories for OpenAI-compatible httpx clients.

Bodies are the former ``make_llm`` branches, verbatim in semantics —
including the key-follows-endpoint rule of the Responses provider.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, TYPE_CHECKING

from .httpx_client import HTTPXOpenAIClient, HTTPXTimeoutConfig

if TYPE_CHECKING:
    from agent_system.config.models import LLMModelConfig
    from agent_system.llm.models import LLMClient

logger = logging.getLogger(__name__)


def _timeout_config(cfg: "LLMModelConfig", default_read: float,
                    default_write: float) -> HTTPXTimeoutConfig:
    ht = cfg.httpx_timeouts.model_dump() if cfg.httpx_timeouts else {}
    read_default = float(cfg.request_timeout) if cfg.request_timeout else default_read
    return HTTPXTimeoutConfig(
        connect=ht.get("connect", 10.0),
        read=ht.get("read", read_default),
        write=ht.get("write", default_write),
        pool=ht.get("pool", 5.0),
    )


def build_openai_httpx(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
    api_key = cfg.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required when provider=openai_httpx")

    if cfg.include_thoughts:
        # Visible instead of silently dropped: the httpx client only wires
        # thinking_level/thinking_budget into the reasoning param;
        # include_thoughts has no effect here.
        logger.warning(
            "include_thoughts is not wired for provider=openai_httpx "
            "and will be ignored (model=%s).",
            cfg.model,
        )

    temp_kw = {} if cfg.temperature is None else {"temperature": cfg.temperature}
    return HTTPXOpenAIClient(
        model=cfg.model,
        **temp_kw,
        api_key=api_key,
        base_url=cfg.base_url or "https://api.openai.com/v1",
        timeout_config=_timeout_config(cfg, default_read=180.0, default_write=10.0),
        max_retries=1,  # 2 attempts total — faster fallback on 5xx (e.g. DeepSeek 504)
        retry_backoff=1.0,
        verify=ssl_verify,
        context_window=cfg.context_window,
        capabilities=cfg.capabilities,
        parallel_tool_calls=cfg.parallel_tool_calls,
        max_tokens=cfg.max_tokens,
        thinking_level=cfg.thinking_level,
        thinking_budget=cfg.thinking_budget,
        safety_settings=cfg.safety_settings,
        service_tier=cfg.service_tier,
        provider_routing=cfg.provider_routing,
        reasoning_details_mode=cfg.reasoning_details_mode,
        prompt_cache_key=cfg.prompt_cache_key,
        prompt_cache_mode=cfg.prompt_cache_mode,
        prompt_cache_marker_style=cfg.prompt_cache_marker_style,
    )


def build_openai_responses(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
    # OpenAI Responses API via OpenRouter (/responses beta): native item
    # round-trip, no Chat-Completions bridging — removes the
    # encrypted-reasoning 400s of that translation layer entirely.
    # See openai_responses_client.py for the full rationale.
    # The EFFECTIVE endpoint decides which key may be used. This provider
    # defaults to OpenRouter, so a missing OPENROUTER_API_KEY must not fall
    # through to OPENAI_API_KEY: the client would come up healthy and put
    # the OpenAI secret into an Authorization header addressed to
    # openrouter.ai, and only the first turn would fail — after the key
    # had already been sent to a third party.
    effective_url = cfg.base_url or "https://openrouter.ai/api/v1"
    via_openrouter = "openrouter.ai" in effective_url.lower()
    api_key = cfg.api_key
    if not api_key:
        api_key = (os.getenv("OPENROUTER_API_KEY") if via_openrouter
                   else os.getenv("OPENAI_API_KEY"))
    if not api_key:
        wanted = "OPENROUTER_API_KEY" if via_openrouter else "OPENAI_API_KEY"
        raise ValueError(
            f"{wanted} is required when provider=openai_responses "
            f"targets {effective_url}")

    if cfg.thinking_budget:
        # Visible instead of silently dropped: the Responses client only
        # knows thinking_level (reasoning.effort) — a budget has no wire
        # field on this route.
        logger.warning(
            "thinking_budget is not wired for provider=openai_responses "
            "and will be ignored (model=%s) — use thinking_level instead.",
            cfg.model,
        )
    if cfg.include_thoughts:
        # debug only: config sets this on every OpenRouter model; the
        # gateway returns reasoning summaries on its own, so the intent
        # roughly holds without a request field.
        logger.debug(
            "include_thoughts has no request field on provider="
            "openai_responses (model=%s) — reasoning summaries arrive "
            "at the provider's discretion.",
            cfg.model,
        )

    from .openai_responses_client import OpenAIResponsesClient
    return OpenAIResponsesClient(
        model=cfg.model,
        api_key=api_key,
        base_url=effective_url,
        context_window=cfg.context_window or 200000,
        request_timeout=cfg.request_timeout or 600,
        ssl_verify=ssl_verify if ssl_verify is not None else True,
        timeout_config=_timeout_config(cfg, default_read=600.0, default_write=30.0),
        capabilities=cfg.capabilities,
        parallel_tool_calls=cfg.parallel_tool_calls,
        thinking_level=cfg.thinking_level,
        max_tokens=cfg.max_tokens,
        service_tier=cfg.service_tier,
        provider_routing=cfg.provider_routing,
        safety_settings=cfg.safety_settings,
        prompt_cache_key=cfg.prompt_cache_key,
        prompt_cache_mode=cfg.prompt_cache_mode,
        prompt_cache_marker_style=cfg.prompt_cache_marker_style,
        temperature=cfg.temperature,
    )


def build_openai_speech_tts(cfg):
    """TTS factory (manifest key provides_tts); lazy import keeps the
    speech client out of LLM-only processes."""
    from .openai_speech_client import build_openai_speech
    return build_openai_speech(cfg)


PROVIDERS = {
    "openai_httpx": build_openai_httpx,
    "openai_responses": build_openai_responses,
}
TTS_PROVIDERS = {"openai_speech": build_openai_speech_tts}
