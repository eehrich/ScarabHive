"""Provider factories for the Anthropic plugin.

The body of each factory is the former ``make_llm`` branch, verbatim in
semantics: env-key fallback, defaults, and parameter wiring are provider
knowledge and live here — the core registry only dispatches on
``cfg.provider``.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, TYPE_CHECKING

from plugins.llm_common.model_dialects import warn_unwired

from .anthropic_client import AnthropicAsyncClient

if TYPE_CHECKING:
    from agent_system.config.models import LLMModelConfig
    from agent_system.llm.batch.base import BatchProviderClient
    from agent_system.llm.models import LLMClient


def build_anthropic(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
    # Whitespace is no key (a stray newline from a secrets file).
    api_key = (cfg.api_key or "").strip() or (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is required when provider=anthropic")

    if cfg.thinking_level:
        # Visible instead of silently dropped: Anthropic thinking is driven by
        # include_thoughts/thinking_budget here — a thinking_level set on a
        # Claude model (e.g. via an llm_params "*" overlay) has no effect at
        # all, and every other provider says so when it drops a field.
        logging.getLogger(__name__).warning(
            "thinking_level is not wired for provider=anthropic and will be "
            "ignored (model=%s) — use include_thoughts/thinking_budget.",
            cfg.model)

    warn_unwired(cfg, provider="anthropic", logger=logging.getLogger(__name__),
                 wired=("reasoning_details_mode", "thinking_request_shape"))

    # Only forward temperature when configured: a None would override
    # provider-side defaults.
    temp_kw = {} if cfg.temperature is None else {"temperature": cfg.temperature}
    return AnthropicAsyncClient(
        ssl_verify=ssl_verify,
        model=cfg.model,
        **temp_kw,
        api_key=api_key,
        base_url=cfg.base_url,
        # Set in the entry, not merely present: both fields have model defaults
        # (32768, 120), and truthiness left this route's own unreachable.
        context_window=(cfg.context_window if "context_window" in cfg.model_fields_set
                        and cfg.context_window else 200000),
        request_timeout=(cfg.request_timeout if "request_timeout" in cfg.model_fields_set
                         and cfg.request_timeout else 180),
        max_retries=3,
        max_tokens=cfg.max_tokens or 8192,
        include_thinking=cfg.include_thoughts or False,
        thinking_budget=cfg.thinking_budget,
        enable_prompt_caching=(cfg.enable_prompt_caching
                               if cfg.enable_prompt_caching is not None else True),
        prompt_cache_mode=cfg.prompt_cache_mode,
        reasoning_details_mode=cfg.reasoning_details_mode,
        thinking_request_shape=cfg.thinking_request_shape,
        capabilities=cfg.capabilities,
    )


def make_batch_backend(cfg: "LLMModelConfig") -> Optional["BatchProviderClient"]:
    """Batch backend for the queue manager; None when no API key is available
    (the caller logs the skip)."""
    api_key = (cfg.api_key or "").strip() or (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None
    from .anthropic_batch import AnthropicBatchClient
    return AnthropicBatchClient(
        api_key=api_key,
        default_model=cfg.model or "claude-sonnet-4-20250514",
    )


PROVIDERS = {"anthropic": build_anthropic}
BATCH_BACKENDS = {"anthropic": make_batch_backend}
