"""Provider factory for the OpenRouter SDK route.

Same endpoint, same model entries, same defaults as ``openai_responses`` —
switching a model over is a one-word change in its YAML. That is the point:
whatever differs in a comparison must be the transport, not the timeouts.
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from plugins_llm.llm_common.api_keys import resolve_api_key
from plugins_llm.llm_common.model_dialects import warn_unwired
# Deliberately the sibling's helper, not a copy: a second timeout default
# would make an A/B measure the config instead of the SDK. The two plugins
# are coupled anyway — the client subclasses the sibling's client.
from plugins_llm.llm_openai_compat.provider import _timeout_config

if TYPE_CHECKING:
    from agent_system.config.models import LLMModelConfig
    from agent_system.llm.models import LLMClient

logger = logging.getLogger(__name__)


def build_openrouter_sdk(cfg: "LLMModelConfig",
                         ssl_verify: Optional[bool] = None) -> "LLMClient":
    # Key follows the endpoint, exactly as on the openai_responses factory:
    # this client speaks to whatever host the model entry names, and an env
    # fallback that ignores base_url would hand the OpenRouter secret to it.
    api_key, effective_url = resolve_api_key(
        cfg.api_key, cfg.base_url,
        default_base_url="https://openrouter.ai/api/v1",
        provider="openrouter_sdk")

    if cfg.thinking_budget:
        # Visible instead of silently dropped — parity with the httpx
        # Responses factory: only thinking_level (reasoning.effort) has a
        # wire field on this route.
        logger.warning(
            "thinking_budget is not wired for provider=openrouter_sdk "
            "and will be ignored (model=%s) — use thinking_level instead.",
            cfg.model,
        )

    warn_unwired(cfg, provider="openrouter_sdk", logger=logger,
                 wired=("tool_schema_dialect", "reasoning_details_mode"))

    from .openrouter_sdk_client import build_openrouter_sdk_client
    return build_openrouter_sdk_client(
        model=cfg.model,
        api_key=api_key,
        base_url=effective_url,
        safety_settings=cfg.safety_settings,
        prompt_cache_marker_style=cfg.prompt_cache_marker_style,
        reasoning_details_mode=cfg.reasoning_details_mode,  # the same round trip as on the other routes
        tool_schema_dialect=cfg.tool_schema_dialect,
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
        prompt_cache_key=cfg.prompt_cache_key,
        prompt_cache_mode=cfg.prompt_cache_mode,
        temperature=cfg.temperature,
        plugins=cfg.plugins,
        prompt_cache_options=cfg.prompt_cache_options,
        safety_identifier=cfg.safety_identifier,
    )


PROVIDERS = {"openrouter_sdk": build_openrouter_sdk}
