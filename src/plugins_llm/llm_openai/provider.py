"""Provider factory for the official OpenAI SDK client.

Body is the former ``make_llm`` branch, verbatim in semantics.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, TYPE_CHECKING

from plugins_llm.llm_common.api_keys import resolve_api_key

from .openai_client import OpenAIAsyncClient

if TYPE_CHECKING:
    from agent_system.config.models import LLMModelConfig
    from agent_system.llm.batch.base import BatchProviderClient
    from agent_system.llm.models import LLMClient

logger = logging.getLogger(__name__)


def build_openai(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
    # The key follows the endpoint (llm_common.api_keys): this SDK client
    # accepts any OpenAI-compatible base_url, so the env fallback must not
    # send the OpenAI secret to a gateway named in the model entry.
    api_key, base_url = resolve_api_key(
        cfg.api_key, cfg.base_url,
        default_base_url="https://api.openai.com/v1",
        provider="openai")

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
        base_url=base_url,
        timeout=float(cfg.request_timeout) if cfg.request_timeout else None,
        verify=ssl_verify,
        context_window=cfg.context_window,
        capabilities=cfg.capabilities,
        max_tokens=cfg.max_tokens,
        default_extra=default_extra if default_extra else None,
    )


def make_batch_backend(cfg: "LLMModelConfig") -> Optional["BatchProviderClient"]:
    """Batch backend for the SDK client.

    The OpenAI Batch API is plain HTTP, so the implementation lives with the
    httpx clients (llm_openai_compat) — this plugin owns the SDK, not a
    second copy of /v1/batches. Delegating through the registry is the same
    move llm_ollama makes for its openai_compat mode: `get_batch_backend`,
    not the concrete class, so the plugin boundary stays intact.

    Each client keeps its OWN batch backend name: `batch_provider: openai`
    pairs this SDK client with this backend, `batch_provider: openai_httpx`
    pairs the httpx client with its own. The core no longer maps one name
    onto another.
    """
    from agent_system.llm import registry

    delegate = registry.get_batch_backend("openai_httpx")
    if delegate is None:
        # Only reachable if llm_openai_compat is missing from the deployment.
        # Say which plugin, or the caller's generic handler logs a bare
        # "'NoneType' object is not callable".
        raise ImportError(
            "batch_provider=openai needs the llm_openai_compat plugin, which "
            "owns the /v1/batches implementation — it declares no "
            "openai_httpx batch backend here")
    return delegate(cfg)


PROVIDERS = {"openai": build_openai}
BATCH_BACKENDS = {"openai": make_batch_backend}
