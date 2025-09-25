"""
LLM factory helpers.

Provides a small factory that creates an LL            else:
                logger.error("Model reference '%s' not found in models", model_ref)
                raise ValueError(f"Model reference '{model_ref}' not found in LLM system models")
        else:
            logger.error("Profile '%s' not found in profiles", profile_name)
            raise ValueError(f"Profile '{profile_name}' not found in LLM system profiles")
    
    # If we reach here, the LLM system is not properly configured
    logger.error("LLM system not properly configured for agent %s", agent_name or "default")
    raise ValueError("LLM system configuration missing or incomplete. Please ensure llm_system with models and profiles is configured.")AgentConfig.
This keeps LLM creation explicit and testable (dependency injection).
Supports both legacy direct LLM config and new profile-based configuration.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..config.models import AgentConfig
from .clients import make_llm, LLMClient

logger = logging.getLogger(__name__)


def resolve_llm_config_for_agent(agent_config: AgentConfig, agent_name: str = None) -> dict:
    """
    Resolve LLM configuration for a specific agent using the new profile system.
    
    Args:
        agent_config: The main agent configuration
        agent_name: The name of the agent to resolve config for
    
    Returns:
        dict: LLM configuration parameters for make_llm()
    """
    # Check if we have the new LLM system configuration
    if agent_config.llm_system and agent_config.llm_system.models and agent_config.llm_system.profiles:
        # Determine which profile to use
        profile_name = None
        
        # 1. Check agent-specific assignment
        if agent_name and agent_config.agent_llm_profiles:
            profile_name = agent_config.agent_llm_profiles.get(agent_name)
        
        # 2. Check if main LLM config specifies a profile
        if not profile_name and agent_config.llm.profile:
            profile_name = agent_config.llm.profile
        
        # 3. Fall back to default profile
        if not profile_name:
            profile_name = agent_config.llm_system.default_profile
        
        # Resolve profile to model config
        if profile_name in agent_config.llm_system.profiles:
            profile = agent_config.llm_system.profiles[profile_name]
            model_ref = profile.model_ref
            
            if model_ref in agent_config.llm_system.models:
                model_config = agent_config.llm_system.models[model_ref]
                
                # Build LLM kwargs from model config
                llm_kwargs = {
                    "provider": model_config.provider,
                    "model": model_config.model,
                    "openai_api_key": model_config.openai_api_key,
                    "ollama_url": model_config.ollama_url,
                    "context_window": model_config.context_window,
                    "ollama_mode": model_config.ollama_mode,
                    "request_timeout": model_config.request_timeout,
                }
                
                logger.debug("Resolved LLM config for agent %s: profile=%s, model_ref=%s, provider=%s, model=%s",
                           agent_name or "default", profile_name, model_ref, model_config.provider, model_config.model)
                
                return llm_kwargs
            else:
                logger.warning("Model reference '%s' not found in models, falling back to legacy config", model_ref)
        else:
            logger.warning("Profile '%s' not found in profiles, falling back to legacy config", profile_name)
    
    # Fall back to legacy LLM config
    logger.debug("Using legacy LLM config for agent %s", agent_name or "default")
    llm_cfg = agent_config.llm
    return {
        "provider": llm_cfg.provider,
        "model": llm_cfg.model,
        "openai_api_key": getattr(llm_cfg, "openai_api_key", None),
        "ollama_url": getattr(llm_cfg, "ollama_url", None),
        "context_window": getattr(llm_cfg, "context_window", None),
        "ollama_mode": getattr(llm_cfg, "ollama_mode", None),
        "request_timeout": getattr(llm_cfg, "request_timeout", None),
    }


class LLMFactory:
    """Create LLM clients from configuration objects.

    This wrapper centralizes the logic for creating an LLM client so that
    callers (for example bootstrap code) can explicitly create an LLM
    without relying on import-time side effects.
    """

    def __init__(self, agent_config: Optional[AgentConfig] = None, agent_name: str = None):
        self.agent_config = agent_config
        self.agent_name = agent_name

    def create(self) -> Optional[LLMClient]:
        """Create an LLM client from the provided `AgentConfig`.

        Returns an LLMClient instance or raises the underlying error from
        `make_llm`. Callers may catch exceptions if they want a fallback
        behavior (for example, running without an LLM in tests).
        """
        if not self.agent_config:
            return None

        # Use new profile-based resolution
        llm_kwargs = resolve_llm_config_for_agent(self.agent_config, self.agent_name)
        
        # Propagate network SSL verification setting into the LLM client creation
        ssl_verify = None
        try:
            ssl_verify = getattr(self.agent_config, "network").ssl_verify
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
        )
