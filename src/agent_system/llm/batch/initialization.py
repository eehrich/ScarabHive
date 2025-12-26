"""Batch Queue Manager Initialization Utilities.

This module provides centralized initialization functions for the batch
queue manager, making it easy to enable batch processing in different
contexts (API server, CLI, agent-run).

Usage:
    from agent_system.llm.batch.initialization import init_batch_system, shutdown_batch_system
    
    # Initialize batch processing
    queue_manager = await init_batch_system(config)
    
    # ... run your application ...
    
    # Shutdown cleanly
    await shutdown_batch_system()
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig
    from .queue_manager import BatchQueueManager

logger = logging.getLogger(__name__)

# Global batch queue manager instance
_batch_queue_manager: Optional["BatchQueueManager"] = None


def _normalize_provider(provider: str) -> str:
    """Normalize provider names to canonical form.
    
    Maps provider variants to their base names for batch client registration.
    
    Args:
        provider: Provider name (e.g., "openai_httpx", "openai", "gemini")
        
    Returns:
        Normalized provider name
    """
    if provider in ("openai", "openai_httpx"):
        return "openai"
    return provider


def setup_batch_queue_manager_sync(
    config: "AgentSystemConfig",
    custom_logger: Optional[logging.Logger] = None,
) -> Optional["BatchQueueManager"]:
    """Synchronously create and register batch queue manager (but don't start it).
    
    This should be called BEFORE agents are created so the factory can wrap
    LLM clients with BatchLLMClient. The actual async startup (connecting to
    providers, recovering jobs) happens later via start_batch_queue_manager().
    
    Args:
        config: AgentSystemConfig containing LLM configurations
        custom_logger: Optional custom logger
        
    Returns:
        BatchQueueManager instance if batch was enabled, None otherwise
    """
    global _batch_queue_manager
    log = custom_logger or logger
    
    if _batch_queue_manager is not None:
        log.debug("Batch queue manager already created, skipping")
        return _batch_queue_manager
    
    if not config.llm_system or not config.llm_system.models:
        log.debug("No LLM models configured, skipping batch initialization")
        return None
    
    # Check if any model has batch enabled
    batch_enabled_models = []
    for model_name, model_config in config.llm_system.models.items():
        if model_config.batch and model_config.batch.enabled:
            batch_enabled_models.append(model_name)
    
    if not batch_enabled_models:
        log.debug("No models with batch enabled, skipping batch queue manager")
        return None
    
    log.info("Found %d models with batch enabled: %s", 
             len(batch_enabled_models), batch_enabled_models)
    
    try:
        from .queue_manager import BatchQueueManager
        from ..factory import set_batch_queue_manager
        
        # Use first batch-enabled model's config for manager settings
        first_model = config.llm_system.models[batch_enabled_models[0]]
        batch_config = first_model.batch
        
        # Create batch queue manager
        storage_path = None
        if batch_config.storage_path:
            storage_path = Path(batch_config.storage_path)
        
        _batch_queue_manager = BatchQueueManager(
            config=batch_config,
            storage_path=storage_path
        )
        
        # Register globally so LLMFactory can access it immediately
        set_batch_queue_manager(_batch_queue_manager)
        log.info("Batch queue manager created and registered (not yet started)")
        
        return _batch_queue_manager
                    
    except Exception as e:
        log.error("Failed to create batch queue manager: %s", e, exc_info=True)
        return None


async def start_batch_queue_manager(
    config: "AgentSystemConfig",
    custom_logger: Optional[logging.Logger] = None,
) -> None:
    """Start the batch queue manager (async operations).
    
    This registers provider clients and starts background tasks.
    Should be called in the async lifespan after the manager was created.
    
    Args:
        config: AgentSystemConfig containing LLM configurations
        custom_logger: Optional custom logger
    """
    from ..factory import get_batch_queue_manager
    
    log = custom_logger or logger
    
    # Get manager from factory (may have been created lazily)
    manager = get_batch_queue_manager()
    
    if manager is None:
        log.debug("No batch queue manager to start")
        return
    
    if manager._running:
        log.debug("Batch queue manager already running")
        return
    
    try:
        # Collect provider info and cancel_on_startup settings
        providers_needing_clients: dict = {}
        providers_cancel_on_startup: set = set()
        
        for model_name, model_config in config.llm_system.models.items():
            if model_config.batch and model_config.batch.enabled:
                normalized = _normalize_provider(model_config.provider)
                if normalized not in providers_needing_clients:
                    providers_needing_clients[normalized] = model_config
                if model_config.batch.cancel_on_startup:
                    providers_cancel_on_startup.add(normalized)
        
        # Register batch clients for each provider
        await _register_batch_clients(
            manager, 
            providers_needing_clients, 
            log
        )
        
        # Start the batch queue manager
        await manager.start()
        
        batch_config = manager.config
        log.info("Batch queue manager started (collection_window=%ss, poll_interval=%ss)",
                 batch_config.collection_window_seconds, 
                 batch_config.poll_interval_seconds)
        
        # Handle existing jobs from providers - per-provider cancel_on_startup
        if providers_cancel_on_startup:
            cancelled = await _cancel_provider_batches(
                manager, 
                providers_cancel_on_startup, 
                log
            )
            if cancelled > 0:
                log.info("Cancelled %d pending batch jobs from providers: %s", 
                        cancelled, list(providers_cancel_on_startup))
        
        # Recover jobs for providers that don't cancel on startup
        providers_to_recover = set(providers_needing_clients.keys()) - providers_cancel_on_startup
        if providers_to_recover:
            recovered = await manager.recover_jobs(providers_to_recover)
            if recovered > 0:
                log.info("Recovered %d active batch jobs from providers: %s", 
                        recovered, list(providers_to_recover))
                
    except Exception as e:
        log.error("Failed to start batch queue manager: %s", e, exc_info=True)


async def init_batch_system(
    config: "AgentSystemConfig",
    custom_logger: Optional[logging.Logger] = None,
) -> Optional["BatchQueueManager"]:
    """Initialize the batch queue manager if any model has batch enabled.
    
    This function:
    1. Scans all LLM models for batch.enabled=true
    2. Creates a BatchQueueManager with the appropriate config
    3. Registers batch clients for each provider (OpenAI, Gemini, etc.)
    4. Starts the background polling/submission tasks
    5. Registers the manager globally so LLMFactory can access it
    
    Args:
        config: AgentSystemConfig containing LLM configurations
        custom_logger: Optional custom logger (defaults to module logger)
        
    Returns:
        BatchQueueManager instance if batch was enabled, None otherwise
        
    Example:
        config = load_config()
        queue_manager = await init_batch_system(config)
        
        if queue_manager:
            print(f"Batch processing enabled with {queue_manager._collection_window}s window")
    """
    global _batch_queue_manager
    log = custom_logger or logger
    
    if not config.llm_system or not config.llm_system.models:
        log.debug("No LLM models configured, skipping batch initialization")
        return None
    
    # Check if any model has batch enabled and collect provider info
    batch_enabled_models = []
    providers_needing_clients: dict = {}  # normalized_provider -> model_config
    providers_cancel_on_startup: set = set()  # providers that need cancel_on_startup
    
    for model_name, model_config in config.llm_system.models.items():
        if model_config.batch and model_config.batch.enabled:
            batch_enabled_models.append(model_name)
            normalized = _normalize_provider(model_config.provider)
            if normalized not in providers_needing_clients:
                providers_needing_clients[normalized] = model_config
            # Track which providers need cancel_on_startup
            if model_config.batch.cancel_on_startup:
                providers_cancel_on_startup.add(normalized)
    
    if not batch_enabled_models:
        log.debug("No models with batch enabled, skipping batch queue manager")
        return None
    
    log.info("Found %d models with batch enabled: %s", 
             len(batch_enabled_models), batch_enabled_models)
    
    try:
        from .queue_manager import BatchQueueManager
        from ..factory import set_batch_queue_manager
        
        # Use first batch-enabled model's config for manager settings
        first_model = config.llm_system.models[batch_enabled_models[0]]
        batch_config = first_model.batch
        
        # Create batch queue manager
        storage_path = None
        if batch_config.storage_path:
            storage_path = Path(batch_config.storage_path)
        
        _batch_queue_manager = BatchQueueManager(
            config=batch_config,
            storage_path=storage_path
        )
        
        # Register batch clients for each provider
        await _register_batch_clients(
            _batch_queue_manager, 
            providers_needing_clients, 
            log
        )
        
        # Register globally so LLMFactory can access it
        set_batch_queue_manager(_batch_queue_manager)
        
        # Start the batch queue manager
        await _batch_queue_manager.start()
        log.info("Batch queue manager started (collection_window=%ss, poll_interval=%ss)",
                 batch_config.collection_window_seconds, 
                 batch_config.poll_interval_seconds)
        
        # Handle existing jobs from providers - per-provider cancel_on_startup
        if providers_cancel_on_startup:
            # Cancel pending jobs only for providers with cancel_on_startup=true
            cancelled = await _cancel_provider_batches(
                _batch_queue_manager, 
                providers_cancel_on_startup, 
                log
            )
            if cancelled > 0:
                log.info("Cancelled %d pending batch jobs from providers: %s", 
                        cancelled, list(providers_cancel_on_startup))
        
        # Recover jobs for providers that don't cancel on startup
        providers_to_recover = set(providers_needing_clients.keys()) - providers_cancel_on_startup
        if providers_to_recover:
            recovered = await _batch_queue_manager.recover_jobs(providers_to_recover)
            if recovered > 0:
                log.info("Recovered %d active batch jobs from providers: %s", 
                        recovered, list(providers_to_recover))
        
        return _batch_queue_manager
                    
    except Exception as e:
        log.error("Failed to initialize batch queue manager: %s", e, exc_info=True)
        # Don't fail startup - just log and continue without batch
        return None


async def _register_batch_clients(
    queue_manager: "BatchQueueManager",
    providers_needing_clients: dict,
    log: logging.Logger,
) -> None:
    """Register batch API clients for each provider.
    
    Args:
        queue_manager: BatchQueueManager instance
        providers_needing_clients: Dict of normalized_provider -> model_config
        log: Logger instance
    """
    for provider, model_config in providers_needing_clients.items():
        try:
            if provider == "openai":
                from .openai_batch import OpenAIBatchClient
                
                # Get API key from model config or environment
                api_key = model_config.api_key
                if not api_key:
                    api_key = os.environ.get("OPENAI_API_KEY", "")
                
                if not api_key:
                    log.warning("No OpenAI API key found, skipping OpenAI batch client")
                    continue
                    
                client = OpenAIBatchClient(api_key=api_key)
                queue_manager.register_batch_client("openai", client)
                # Also register for openai_httpx variant
                queue_manager.register_batch_client("openai_httpx", client)
                log.info("Registered OpenAI batch client")
                
            elif provider == "gemini":
                from .gemini_batch import GeminiBatchClient
                
                # Get API key from model config or environment
                api_key = model_config.api_key
                if not api_key:
                    api_key = os.environ.get("GOOGLE_API_KEY", "")
                
                if not api_key:
                    log.warning("No Gemini API key found, skipping Gemini batch client")
                    continue
                    
                client = GeminiBatchClient(api_key=api_key)
                queue_manager.register_batch_client("gemini", client)
                log.info("Registered Gemini batch client")
                
            elif provider == "anthropic":
                # Anthropic batch support can be added here when needed
                log.debug("Anthropic batch not yet implemented")
                
            else:
                log.warning(f"Unknown provider for batch: {provider}")
                
        except Exception as e:
            log.error(f"Failed to register batch client for {provider}: {e}")


async def _cancel_provider_batches(
    queue_manager: "BatchQueueManager",
    providers_to_cancel: set,
    log: logging.Logger,
) -> int:
    """Cancel pending batches from specified providers.
    
    Args:
        queue_manager: BatchQueueManager instance
        providers_to_cancel: Set of provider names to cancel batches for
        log: Logger instance
        
    Returns:
        Total number of batches cancelled
    """
    total_cancelled = 0
    
    for provider, client in queue_manager._batch_clients.items():
        # Only cancel for providers in the cancel set
        if provider not in providers_to_cancel:
            log.debug(f"Skipping cancel for provider {provider} (cancel_on_startup=false)")
            continue
            
        try:
            if hasattr(client, 'cancel_all_pending_batches'):
                cancelled = await client.cancel_all_pending_batches()
                total_cancelled += cancelled
                if cancelled > 0:
                    log.info(f"Cancelled {cancelled} pending batches from {provider}")
                else:
                    log.debug(f"No pending batches to cancel from {provider}")
        except Exception as e:
            log.warning(f"Failed to cancel batches from {provider}: {e}")
    
    return total_cancelled


async def shutdown_batch_system(
    custom_logger: Optional[logging.Logger] = None,
) -> None:
    """Shutdown the batch queue manager cleanly.
    
    This should be called during application shutdown to:
    1. Stop background polling tasks
    2. Wait for pending batches to complete (optional)
    3. Clean up resources
    
    Args:
        custom_logger: Optional custom logger (defaults to module logger)
    """
    global _batch_queue_manager
    log = custom_logger or logger
    
    if _batch_queue_manager:
        try:
            from ..factory import set_batch_queue_manager
            await _batch_queue_manager.stop()
            set_batch_queue_manager(None)
            _batch_queue_manager = None
            log.info("Batch queue manager stopped")
        except Exception as e:
            log.error(f"Error stopping batch queue manager: {e}")


def get_batch_queue_manager() -> Optional["BatchQueueManager"]:
    """Get the current batch queue manager instance.
    
    Checks both the local module global (for explicit init) and the 
    factory module (for lazy initialization).
    
    Returns:
        BatchQueueManager instance if initialized, None otherwise
    """
    # First check local module global (explicit initialization)
    if _batch_queue_manager is not None:
        return _batch_queue_manager
    
    # Also check factory module (lazy initialization)
    try:
        from ..factory import get_batch_queue_manager as factory_get_manager
        return factory_get_manager()
    except ImportError:
        return None


def is_batch_enabled() -> bool:
    """Check if batch processing is currently enabled.
    
    Returns:
        True if batch queue manager is running, False otherwise
    """
    return _batch_queue_manager is not None and _batch_queue_manager._running
