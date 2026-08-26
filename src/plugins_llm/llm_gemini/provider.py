"""Provider factories for the Gemini plugin (HTTP client and official SDK).

Bodies are the former ``make_llm`` branches, verbatim in semantics.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent_system.config.models import LLMModelConfig
    from agent_system.llm.batch.base import BatchProviderClient
    from agent_system.llm.models import LLMClient


def _resolve_key(cfg: "LLMModelConfig") -> str:
    api_key = cfg.api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError(
            f"GEMINI_API_KEY or GOOGLE_API_KEY is required when provider={cfg.provider}")
    return api_key


def _common_kwargs(cfg: "LLMModelConfig", ssl_verify: Optional[bool]) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = dict(
        model=cfg.model,
        api_key=_resolve_key(cfg),
        base_url=cfg.base_url or "https://generativelanguage.googleapis.com/v1beta",
        context_window=cfg.context_window or 200000,
        request_timeout=cfg.request_timeout or 180,
        ssl_verify=ssl_verify if ssl_verify is not None else True,
        httpx_timeouts=cfg.httpx_timeouts.model_dump() if cfg.httpx_timeouts else None,
        parallel_tool_calls=cfg.parallel_tool_calls,
        include_thoughts=cfg.include_thoughts,
        thinking_budget=cfg.thinking_budget,
        thinking_level=cfg.thinking_level,
        max_tokens=cfg.max_tokens,
        safety_settings=cfg.safety_settings,
        capabilities=cfg.capabilities,
    )
    if cfg.temperature is not None:
        kwargs["temperature"] = cfg.temperature
    return kwargs


def build_gemini(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
    from .gemini_client import GeminiClient
    return GeminiClient(**_common_kwargs(cfg, ssl_verify))


def build_gemini_sdk(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
    from .gemini_sdk_client import GeminiSDKClient
    return GeminiSDKClient(max_retries=3, **_common_kwargs(cfg, ssl_verify))


def make_batch_backend(cfg: "LLMModelConfig") -> Optional["BatchProviderClient"]:
    # Same two variables the sync path accepts (_resolve_key): a
    # GEMINI_API_KEY-only environment used to lose batch silently — the
    # caller just logs a skip and falls back to sync.
    api_key = (cfg.api_key or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY", ""))
    if not api_key:
        return None
    from .gemini_batch import GeminiBatchClient
    return GeminiBatchClient(api_key=api_key)


def build_gemini_tts(cfg):
    """TTS factory (manifest key provides_tts); lazy so the SDK only loads
    when a gemini_tts model is actually built."""
    from .gemini_tts_client import build_gemini_tts as _build
    return _build(cfg)


PROVIDERS = {"gemini": build_gemini, "gemini_sdk": build_gemini_sdk}
BATCH_BACKENDS = {"gemini": make_batch_backend}
TTS_PROVIDERS = {"gemini_tts": build_gemini_tts}
