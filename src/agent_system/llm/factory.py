"""
LLM factory helpers.

Provides a small factory that creates an LLM client from an AgentConfig.
This keeps LLM creation explicit and testable (dependency injection).
"""
from __future__ import annotations

from typing import Optional

from ..config.models import AgentConfig
from .clients import make_llm, LLMClient


class LLMFactory:
    """Create LLM clients from configuration objects.

    This wrapper centralizes the logic for creating an LLM client so that
    callers (for example bootstrap code) can explicitly create an LLM
    without relying on import-time side effects.
    """

    def __init__(self, agent_config: Optional[AgentConfig] = None):
        self.agent_config = agent_config

    def create(self) -> Optional[LLMClient]:
        """Create an LLM client from the provided `AgentConfig`.

        Returns an LLMClient instance or raises the underlying error from
        `make_llm`. Callers may catch exceptions if they want a fallback
        behavior (for example, running without an LLM in tests).
        """
        if not self.agent_config or not getattr(self.agent_config, "llm", None):
            return None

        llm_cfg = self.agent_config.llm
        # Pass explicit values from config to make_llm so creation is explicit
        # Propagate network SSL verification setting into the LLM client creation
        ssl_verify = None
        try:
            ssl_verify = getattr(self.agent_config, "network").ssl_verify
        except Exception:
            ssl_verify = None
        return make_llm(
            llm_cfg.provider,
            llm_cfg.model,
            getattr(llm_cfg, "openai_api_key", None),
            getattr(llm_cfg, "ollama_url", None),
            getattr(llm_cfg, "context_window", None),
            getattr(llm_cfg, "ollama_mode", None),
            getattr(llm_cfg, "request_timeout", None),
            ssl_verify=ssl_verify,
        )
