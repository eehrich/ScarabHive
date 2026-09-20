"""Provider factory for Ollama.

Body is the former ``make_llm`` branch, verbatim in semantics. The
openai_compat mode used to construct ``OpenAIAsyncClient`` directly; that
class now lives in the llm_openai plugin, so this factory delegates
through the registry instead of importing across plugins.
"""
from __future__ import annotations

import logging

from plugins.llm_common.model_dialects import warn_unwired
from typing import Any, Dict, Optional, TYPE_CHECKING

from .ollama_client import OllamaNativeAsyncClient

if TYPE_CHECKING:
    from agent_system.config.models import LLMModelConfig
    from agent_system.llm.models import LLMClient

logger = logging.getLogger(__name__)


def build_ollama(cfg: "LLMModelConfig", ssl_verify: Optional[bool] = None) -> "LLMClient":
    warn_unwired(cfg, provider="ollama", wired=(), logger=logger)
    if cfg.thinking_level or cfg.thinking_budget or cfg.include_thoughts:
        logger.debug(
            "thinking_level/thinking_budget/include_thoughts are ignored "
            "for provider=ollama (model=%s).",
            cfg.model,
        )

    mode = (cfg.ollama_mode or "openai_compat").lower()
    if mode == "native":
        base_native = cfg.base_url.rstrip("/") if cfg.base_url else "http://127.0.0.1:11434"
        options: Dict[str, Any] = {}
        if cfg.context_window:
            options["num_ctx"] = cfg.context_window
        if cfg.temperature is not None:
            options["temperature"] = cfg.temperature
        return OllamaNativeAsyncClient(
            model=cfg.model,
            base_url=base_native,
            options=options or None,
            timeout=float(cfg.request_timeout) if cfg.request_timeout else None,
            verify=ssl_verify,
            context_window=cfg.context_window,
            capabilities=cfg.capabilities,
        )

    # openai_compat mode: an OpenAI SDK client pointed at the Ollama
    # OpenAI-compatible endpoint. Fields the ollama branch never forwarded
    # (thinking, prompt cache, modalities) are cleared so the openai
    # factory neither wires nor warns about them.
    if cfg.prompt_cache_key:
        logger.warning(
            "prompt_cache_key is ignored for provider=ollama (model=%s).",
            cfg.model,
        )
    base = cfg.base_url.rstrip("/") if cfg.base_url else "http://127.0.0.1:11434/v1"
    delegated = cfg.model_copy(update={
        "provider": "openai",
        "api_key": cfg.api_key or "ollama",
        "base_url": base,
        "thinking_level": None,
        "thinking_budget": None,
        "include_thoughts": None,
        "modalities": None,
        "prompt_cache_key": None,
        "prompt_cache_mode": None,
    })
    # get_provider, not build_client: the delegation must reach the openai
    # FACTORY even when the public build_client seam is replaced by a test
    # fake — otherwise an ollama client built through the real path would
    # come back with a fake inner client.
    from agent_system.llm.registry import get_provider
    return get_provider("openai")(delegated, ssl_verify=ssl_verify)


PROVIDERS = {"ollama": build_ollama}
