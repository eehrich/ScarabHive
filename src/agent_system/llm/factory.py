"""
LLM factory helpers.

Provides a factory that creates LLM clients from AgentSystemConfig and AgentConfig.
This keeps LLM creation explicit and testable (dependency injection).
Uses the new profile-based configuration system.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from ..config.models import AgentSystemConfig, AgentConfig
from .clients import make_llm, LLMClient

if TYPE_CHECKING:
    from .batch.queue_manager import BatchQueueManager

logger = logging.getLogger(__name__)


# Global batch queue manager - lazy initialized on first use
_batch_queue_manager: Optional["BatchQueueManager"] = None
_batch_manager_config: Optional[AgentSystemConfig] = None  # Config for lazy init


def set_batch_queue_manager(manager: Optional["BatchQueueManager"]) -> None:
    """Set the global batch queue manager."""
    global _batch_queue_manager
    _batch_queue_manager = manager
    if manager:
        logger.info("Batch queue manager registered globally")


def set_batch_config(config: AgentSystemConfig) -> None:
    """Store config for lazy batch manager initialization."""
    global _batch_manager_config
    _batch_manager_config = config


def get_batch_queue_manager() -> Optional["BatchQueueManager"]:
    """Get or create the global batch queue manager (lazy init)."""
    global _batch_queue_manager
    
    if _batch_queue_manager is not None:
        return _batch_queue_manager
    
    # Lazy init if we have config
    if _batch_manager_config is not None:
        _batch_queue_manager = _create_batch_queue_manager_sync(_batch_manager_config)
        
    return _batch_queue_manager


def _create_batch_queue_manager_sync(config: AgentSystemConfig) -> Optional["BatchQueueManager"]:
    """Create batch queue manager synchronously (without starting async tasks)."""
    if not config.llm_system or not config.llm_system.models:
        return None
    
    # Check for global batch config
    batch_system_config = config.llm_system.batch
    if not batch_system_config:
        return None
    
    # Check if any provider is enabled
    providers_config = batch_system_config.providers
    gemini_enabled = providers_config.gemini.enabled if providers_config.gemini else False
    openai_enabled = providers_config.openai.enabled if providers_config.openai else False
    
    if not gemini_enabled and not openai_enabled:
        return None
    
    # Check if any model uses batch provider
    has_batch_model = any(
        m.provider == "batch" for m in config.llm_system.models.values()
    )
    if not has_batch_model:
        return None
    
    from .batch.queue_manager import BatchQueueManager
    
    storage_path = Path(batch_system_config.storage_path)
    manager = BatchQueueManager(
        batch_system_config=batch_system_config,
        storage_path=storage_path
    )
    logger.info("Batch queue manager created (lazy init)")
    return manager


def create_llm_from_profile(
    config: AgentSystemConfig,
    llm_profile: str,
    ssl_verify: Optional[bool] = None,
) -> LLMClient:
    """Create an LLM client from a profile name.
    
    This is the recommended way to create LLM clients when you need to
    override the default profile. It properly handles batch mode wrapping.
    
    Args:
        config: The system configuration
        llm_profile: Name of the LLM profile to use
        ssl_verify: Optional SSL verification override
        
    Returns:
        LLMClient (possibly wrapped with BatchLLMClient if batch mode enabled)
    """
    from .clients import make_llm
    
    # Create temporary agent config with override profile
    temp_agent_config = AgentConfig(llm_profile=llm_profile)
    llm_kwargs = resolve_llm_config_for_agent(config, temp_agent_config)
    
    # Extract batch info before passing to make_llm
    is_batch_model: bool = llm_kwargs.pop("is_batch_model", False)
    batch_provider: Optional[str] = llm_kwargs.pop("batch_provider", None)
    model_ref: Optional[str] = llm_kwargs.pop("model_ref", None)
    
    # Use provided ssl_verify or get from config
    if ssl_verify is None:
        ssl_verify = getattr(config.network, "ssl_verify", None) if config.network else None
    
    # Build make_kwargs
    make_kwargs = {
        "ssl_verify": ssl_verify,
        "httpx_timeouts": llm_kwargs.get("httpx_timeouts"),
        "capabilities": llm_kwargs.get("capabilities"),
    }
    
    if llm_kwargs.get("include_thoughts") is not None:
        make_kwargs["include_thoughts"] = llm_kwargs.get("include_thoughts")
    
    if llm_kwargs.get("thinking_budget") is not None:
        make_kwargs["thinking_budget"] = llm_kwargs.get("thinking_budget")
    
    # Create the underlying LLM client
    underlying_client = make_llm(
        llm_kwargs["provider"],
        llm_kwargs["model"],
        llm_kwargs["api_key"],
        llm_kwargs["base_url"],
        llm_kwargs["context_window"],
        llm_kwargs["ollama_mode"],
        llm_kwargs["request_timeout"],
        **make_kwargs,
    )
    
    # Wrap with batch client if this is a batch model
    if is_batch_model and batch_provider:
        queue_manager = get_batch_queue_manager()
        if queue_manager:
            # Get provider config from global batch settings
            batch_system_config = config.llm_system.batch if config.llm_system else None
            if batch_system_config:
                provider_config = getattr(batch_system_config.providers, batch_provider, None)
                if provider_config and provider_config.enabled:
                    from .batch.batch_client import BatchLLMClient
                    logger.info("Wrapping LLM client with batch support: model=%s, provider=%s", 
                               model_ref, batch_provider)
                    return BatchLLMClient(
                        underlying_client=underlying_client,
                        queue_manager=queue_manager,
                        batch_provider_config=provider_config,
                        model_name=llm_kwargs["model"],
                        batch_provider=batch_provider,
                    )
        logger.warning(
            "Batch mode requested for model %s but batch system not available. "
            "Falling back to sync mode.",
            model_ref
        )
    
    return underlying_client
    
    return underlying_client


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

    # Determine the actual provider for make_llm()
    # If provider is "batch", we use batch_provider to determine the underlying provider
    provider = model_config.provider
    batch_provider = model_config.batch_provider
    is_batch_model = provider == "batch"
    
    if is_batch_model:
        if not batch_provider:
            raise ValueError(f"Model '{model_ref}' has provider='batch' but no batch_provider specified")
        # Map batch_provider to actual LLM provider
        if batch_provider == "gemini":
            provider = "gemini"
        elif batch_provider == "openai":
            provider = "openai_httpx"  # Use httpx variant for OpenAI
        else:
            raise ValueError(f"Unknown batch_provider: {batch_provider}")

    # Build LLM kwargs from model config
    llm_kwargs = {
        "provider": provider,
        "model": model_config.model,
        "api_key": model_config.api_key,
        "base_url": model_config.base_url,
        "context_window": model_config.context_window,
        "ollama_mode": model_config.ollama_mode,
        "request_timeout": model_config.request_timeout,
        "parallel_tool_calls": model_config.parallel_tool_calls,
        "capabilities": model_config.capabilities,  # Pass Pydantic model directly
    }

    if model_config.include_thoughts is not None:
        llm_kwargs["include_thoughts"] = model_config.include_thoughts

    if model_config.thinking_budget is not None:
        llm_kwargs["thinking_budget"] = model_config.thinking_budget

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
    
    # Add batch info if this is a batch model
    if is_batch_model:
        llm_kwargs["is_batch_model"] = True
        llm_kwargs["batch_provider"] = batch_provider
        llm_kwargs["model_ref"] = model_ref
        logger.info("Batch model detected: %s (batch_provider=%s)", model_ref, batch_provider)
    else:
        llm_kwargs["is_batch_model"] = False

    logger.debug("Resolved LLM config: profile=%s, model_ref=%s, provider=%s, model=%s",
                 profile_name, model_ref, provider, model_config.model)

    return llm_kwargs


class LLMFactory:
    """Create LLM clients from configuration objects.

    This wrapper centralizes the logic for creating an LLM client so that
    callers (for example bootstrap code) can explicitly create an LLM
    without relying on import-time side effects.
    
    When provider='batch' is set in the model config, the factory wraps
    the underlying LLM client with a BatchLLMClient that routes requests
    through the global BatchQueueManager for 50% cost reduction.
    """

    def __init__(self, config: Optional[AgentSystemConfig] = None, agent_config: Optional[AgentConfig] = None):
        self.config = config
        self.agent_config = agent_config

    def create(self) -> Optional[LLMClient]:
        """Create an LLM client from the provided configurations.

        Returns an LLMClient instance or raises the underlying error from
        `make_llm`. Callers may catch exceptions if they want a fallback
        behavior (for example, running without an LLM in tests).
        
        If provider='batch' is set in the model config AND a global batch
        queue manager is registered, the client will be wrapped with
        BatchLLMClient for automatic request batching.
        """
        if not self.config or not self.agent_config:
            return None

        # Use new profile-based resolution
        llm_kwargs = resolve_llm_config_for_agent(self.config, self.agent_config)
        
        # Extract batch info before passing to make_llm
        is_batch_model: bool = llm_kwargs.pop("is_batch_model", False)
        batch_provider: Optional[str] = llm_kwargs.pop("batch_provider", None)
        model_ref: Optional[str] = llm_kwargs.pop("model_ref", None)

        # Propagate network SSL verification setting into the LLM client creation
        ssl_verify = None
        try:
            ssl_verify = self.config.network.ssl_verify
        except Exception:
            ssl_verify = None

        make_kwargs = {
            "ssl_verify": ssl_verify,
            "httpx_timeouts": llm_kwargs.get("httpx_timeouts"),
            "capabilities": llm_kwargs.get("capabilities"),
        }

        if llm_kwargs.get("include_thoughts") is not None:
            make_kwargs["include_thoughts"] = llm_kwargs.get("include_thoughts")

        # Create the underlying LLM client
        underlying_client = make_llm(
            llm_kwargs["provider"],
            llm_kwargs["model"],
            llm_kwargs["api_key"],
            llm_kwargs["base_url"],
            llm_kwargs["context_window"],
            llm_kwargs["ollama_mode"],
            llm_kwargs["request_timeout"],
            **make_kwargs,
        )
        
        # Wrap with batch client if this is a batch model
        if is_batch_model and batch_provider:
            queue_manager = get_batch_queue_manager()
            if queue_manager:
                # Get provider config from global batch settings
                batch_system_config = self.config.llm_system.batch if self.config.llm_system else None
                if batch_system_config:
                    provider_config = getattr(batch_system_config.providers, batch_provider, None)
                    if provider_config and provider_config.enabled:
                        from .batch.client_wrapper import BatchLLMClient
                        logger.info("Wrapping LLM client with batch support: model=%s, provider=%s",
                                   model_ref, batch_provider)
                        return BatchLLMClient(
                            underlying_client=underlying_client,
                            queue_manager=queue_manager,
                            batch_provider_config=provider_config,
                            model_name=llm_kwargs["model"],
                            batch_provider=batch_provider,
                        )
            logger.warning(
                "Batch mode requested for model %s but batch system not available. "
                "Falling back to sync mode.",
                model_ref
            )
        
        return underlying_client
