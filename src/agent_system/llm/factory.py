"""
LLM factory helpers.

Provides a factory that creates LLM clients from AgentSystemConfig and AgentConfig.
This keeps LLM creation explicit and testable (dependency injection).
Uses the new profile-based configuration system.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..config.models import AgentSystemConfig, AgentConfig
from .clients import make_llm, LLMClient

logger = logging.getLogger(__name__)


def resolve_llm_config_for_agent(config: AgentSystemConfig, agent_config: AgentConfig) -> dict:
    """
    Resolve LLM configuration for a specific agent using the profile system.

    Args:
        config: The main system configuration containing LLM system
        agent_config: The agent-specific configuration

    Returns:
        dict: LLM configuration parameters for make_llm()
    """
    if not config.llm_system:
        raise ValueError("LLM system configuration is missing from AgentSystemConfig")

    # Get the default profile name from agent config
    profile_name = agent_config.default_llm_profile

    # Resolve profile to model config
    if profile_name not in config.llm_system.profiles:
        raise ValueError(f"Profile '{profile_name}' not found in LLM system profiles")

    profile = config.llm_system.profiles[profile_name]
    model_ref = profile.model_ref

    if model_ref not in config.llm_system.models:
        raise ValueError(f"Model reference '{model_ref}' not found in LLM system models")

    model_config = config.llm_system.models[model_ref]

    # Build LLM kwargs from model config
    llm_kwargs = {
        "provider": model_config.provider,
        "model": model_config.model,
        "openai_api_key": model_config.openai_api_key,
        "ollama_url": model_config.ollama_url,
        "context_window": model_config.context_window,
        "ollama_mode": model_config.ollama_mode,
        "request_timeout": model_config.request_timeout,
        "capabilities": model_config.capabilities,  # Pass Pydantic model directly
    }

    # Add HTTPX timeouts if available (model-specific overrides or system defaults)
    httpx_timeouts = None
    if model_config.httpx_timeouts:
        # Model-specific HTTPX timeouts
        httpx_timeouts = model_config.httpx_timeouts.model_dump()
    elif config.llm_system.httpx_timeouts:
        # System default HTTPX timeouts
        httpx_timeouts = config.llm_system.httpx_timeouts.model_dump()

    if httpx_timeouts:
        llm_kwargs["httpx_timeouts"] = httpx_timeouts

    logger.debug("Resolved LLM config: profile=%s, model_ref=%s, provider=%s, model=%s",
                 profile_name, model_ref, model_config.provider, model_config.model)

    return llm_kwargs


class LLMFactory:
    """Create LLM clients from configuration objects.

    This wrapper centralizes the logic for creating an LLM client so that
    callers (for example bootstrap code) can explicitly create an LLM
    without relying on import-time side effects.
    """

    def __init__(self, config: Optional[AgentSystemConfig] = None, agent_config: Optional[AgentConfig] = None):
        self.config = config
        self.agent_config = agent_config

    def create(self) -> Optional[LLMClient]:
        """Create an LLM client from the provided configurations.

        Returns an LLMClient instance or raises the underlying error from
        `make_llm`. Callers may catch exceptions if they want a fallback
        behavior (for example, running without an LLM in tests).
        """
        if not self.config or not self.agent_config:
            return None

        # Use new profile-based resolution
        llm_kwargs = resolve_llm_config_for_agent(self.config, self.agent_config)

        # Propagate network SSL verification setting into the LLM client creation
        ssl_verify = None
        try:
            ssl_verify = self.config.network.ssl_verify
        except Exception:
            ssl_verify = None

        return make_llm(
            llm_kwargs["provider"],
            llm_kwargs["model"],
            llm_kwargs["openai_api_key"],
            llm_kwargs["ollama_url"],
            llm_kwargs["context_window"],
            llm_kwargs["ollama_mode"],
            llm_kwargs["request_timeout"],
            ssl_verify=ssl_verify,
            httpx_timeouts=llm_kwargs.get("httpx_timeouts"),
            capabilities=llm_kwargs.get("capabilities"),
        )
