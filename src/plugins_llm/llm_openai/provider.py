"""Provider factory for the official OpenAI SDK client.

Body is the former ``make_llm`` branch, verbatim in semantics.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, TYPE_CHECKING

from .openai_client import OpenAIAsyncClient

if TYPE_CHECKING:
    from agent_system.config.models import LLMModelConfig
    from agent_system.llm.batch.base import BatchProviderClient
    from agent_system.llm.models import LLMClient

logger = logging.getLogger(__name__)


def build_openai(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
    api_key = cfg.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required when provider=openai")

    default_extra: Dict[str, Any] = {}
    if cfg.temperature is not None:
        default_extra["temperature"] = cfg.temperature
    if cfg.modalities:
        default_extra["modalities"] = cfg.modalities

    if cfg.thinking_level or cfg.thinking_budget or cfg.include_thoughts:
        # Visible instead of silently dropped: the SDK client has no
        # thinking path at all.
        logger.warning(
            "thinking_level/thinking_budget/include_thoughts are not "
            "wired for provider=openai (SDK client) and will be "
            "ignored (model=%s) — switch the profile to openai_httpx "
            "or openai_responses.",
            cfg.model,
        )

    if cfg.prompt_cache_key or cfg.prompt_cache_mode:
        # Visible instead of silently dropped: the SDK client has no
        # prompt_cache_key path — keyed caching needs openai_httpx or
        # openai_responses.
        logger.warning(
            "prompt_cache_key/prompt_cache_mode are not wired for "
            "provider=openai (SDK client) and will be ignored (model=%s) "
            "— switch the profile to openai_httpx or openai_responses.",
            cfg.model,
        )

    return OpenAIAsyncClient(
        model=cfg.model,
        api_key=api_key,
        base_url=cfg.base_url or "https://api.openai.com/v1",
        timeout=float(cfg.request_timeout) if cfg.request_timeout else None,
        verify=ssl_verify,
        context_window=cfg.context_window,
        capabilities=cfg.capabilities,
        max_tokens=cfg.max_tokens,
        default_extra=default_extra if default_extra else None,
    )


def make_batch_backend(cfg: "LLMModelConfig") -> Optional["BatchProviderClient"]:
    api_key = cfg.api_key or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    from .openai_batch import OpenAIBatchClient
    return OpenAIBatchClient(api_key=api_key)


PROVIDERS = {"openai": build_openai}
BATCH_BACKENDS = {"openai": make_batch_backend}
