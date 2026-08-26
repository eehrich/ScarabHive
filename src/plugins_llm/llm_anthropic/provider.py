"""Provider factories for the Anthropic plugin.

The body of each factory is the former ``make_llm`` branch, verbatim in
semantics: env-key fallback, defaults, and parameter wiring are provider
knowledge and live here — the core registry only dispatches on
``cfg.provider``.
"""
from __future__ import annotations

import os
from typing import Optional, TYPE_CHECKING

from .anthropic_client import AnthropicAsyncClient

if TYPE_CHECKING:
    from agent_system.config.models import LLMModelConfig
    from agent_system.llm.batch.base import BatchProviderClient
    from agent_system.llm.models import LLMClient


def build_anthropic(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
    api_key = cfg.api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is required when provider=anthropic")

    # Only forward temperature when configured: a None would override
    # provider-side defaults.
    temp_kw = {} if cfg.temperature is None else {"temperature": cfg.temperature}
    return AnthropicAsyncClient(
        model=cfg.model,
        **temp_kw,
        api_key=api_key,
        base_url=cfg.base_url,
        context_window=cfg.context_window or 200000,
        request_timeout=cfg.request_timeout or 180,
        max_retries=3,
        max_tokens=cfg.max_tokens or 8192,
        include_thinking=cfg.include_thoughts or False,
        thinking_budget=cfg.thinking_budget,
        enable_prompt_caching=(cfg.enable_prompt_caching
                               if cfg.enable_prompt_caching is not None else True),
        prompt_cache_mode=cfg.prompt_cache_mode,
        reasoning_details_mode=cfg.reasoning_details_mode,
        capabilities=cfg.capabilities,
    )


def make_batch_backend(cfg: "LLMModelConfig") -> Optional["BatchProviderClient"]:
    """Batch backend for the queue manager; None when no API key is available
    (the caller logs the skip)."""
    api_key = cfg.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    from .anthropic_batch import AnthropicBatchClient
    return AnthropicBatchClient(
        api_key=api_key,
        default_model=cfg.model or "claude-sonnet-4-20250514",
    )


PROVIDERS = {"anthropic": build_anthropic}
BATCH_BACKENDS = {"anthropic": make_batch_backend}
