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
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Any

from .job_tracker import BatchJobTracker, set_job_tracker

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig
    from .queue_manager import BatchQueueManager

logger = logging.getLogger(__name__)

# Global batch queue manager instance
_batch_queue_manager: Optional["BatchQueueManager"] = None


def _normalize_batch_provider(batch_provider: str) -> str:
    """Normalize batch provider names to canonical form.
    
    Args:
        batch_provider: Batch provider name (e.g., "openai", "gemini")
        
    Returns:
        Normalized provider name
    """
    # Batch providers are already canonical (gemini, openai)
    return batch_provider


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
    
    if not config.llm_system:
        log.debug("No LLM system configured, skipping batch initialization")
        return None
    
    # Check for global batch config
    batch_system_config = config.llm_system.batch
    if not batch_system_config:
        log.debug("No batch system config, skipping batch queue manager")
        return None
    
    # Check if any provider is enabled
    providers_config = batch_system_config.providers
    if not any(p.enabled for p in providers_config.values()):
        log.debug("No batch providers enabled, skipping batch queue manager")
        return None
    
    # Check if any model uses provider='batch'
    batch_models = [
        name for name, m in config.llm_system.models.items()
        if m.provider == "batch"
    ]
    
    if not batch_models:
        log.debug("No models with provider='batch', skipping batch queue manager")
        return None
    
    log.info("Found %d batch models: %s", len(batch_models), batch_models)
    
    try:
        from .queue_manager import BatchQueueManager
        from ..factory import set_batch_queue_manager
        
        # Create batch queue manager with global config
        storage_path = Path(batch_system_config.storage_path)
        
        # Initialize job tracker for tracking our submitted jobs
        job_tracker = BatchJobTracker(storage_path)
        set_job_tracker(job_tracker)
        log.debug("Job tracker initialized at %s", storage_path)
        
        _batch_queue_manager = BatchQueueManager(
            batch_system_config=batch_system_config,
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
    
    batch_system_config = config.llm_system.batch if config.llm_system else None
    if not batch_system_config:
        log.debug("No batch system config available")
        return
    
    try:
        # Initialize job tracker if not already set (needed for cancel_all_pending_batches)
        from .job_tracker import get_job_tracker
        if not get_job_tracker():
            storage_path = Path(batch_system_config.storage_path)
            job_tracker = BatchJobTracker(storage_path)
            set_job_tracker(job_tracker)
            log.debug("Job tracker initialized at %s", storage_path)
        
        # Collect batch providers from models with provider='batch'
        providers_needing_clients: dict = {}  # batch_provider -> model_config
        providers_cancel_on_startup: set = set()
        
        for model_name, model_config in config.llm_system.models.items():
            if model_config.provider == "batch" and model_config.batch_provider:
                batch_provider = model_config.batch_provider
                if batch_provider not in providers_needing_clients:
                    providers_needing_clients[batch_provider] = model_config
                # Check cancel_on_startup from global provider config
                provider_config = batch_system_config.providers.get(batch_provider)
                if provider_config and provider_config.cancel_on_startup:
                    providers_cancel_on_startup.add(batch_provider)
        
        # Register batch clients for each provider
        await _register_batch_clients(
            manager, 
            providers_needing_clients,
            batch_system_config,
            log
        )
        
        # Start the batch queue manager
        await manager.start()
        
        log.info("Batch queue manager started (providers: %s)", 
                 list(providers_needing_clients.keys()))
        
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
    """Initialize the batch queue manager if any model has provider='batch'.
    
    This function:
    1. Scans all LLM models for provider='batch'
    2. Creates a BatchQueueManager with the global batch config
    3. Registers batch clients for each provider (OpenAI, Gemini)
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
            print("Batch processing enabled")
    """
    global _batch_queue_manager
    log = custom_logger or logger
    
    if not config.llm_system:
        log.debug("No LLM system configured, skipping batch initialization")
        return None
    
    # Check for global batch config
    batch_system_config = config.llm_system.batch
    if not batch_system_config:
        log.debug("No batch system config, skipping batch initialization")
        return None
    
    # Check if any provider is enabled
    providers_config = batch_system_config.providers
    if not any(p.enabled for p in providers_config.values()):
        log.debug("No batch providers enabled, skipping batch initialization")
        return None
    
    # Check for models with provider='batch' and collect their batch_provider info
    batch_models = []
    providers_needing_clients: dict = {}  # batch_provider -> model_config
    providers_cancel_on_startup: set = set()  # providers that need cancel_on_startup
    
    for model_name, model_config in config.llm_system.models.items():
        if model_config.provider == "batch":
            if not model_config.batch_provider:
                log.warning("Model %s has provider='batch' but no batch_provider", model_name)
                continue
            batch_models.append(model_name)
            batch_provider = model_config.batch_provider
            if batch_provider not in providers_needing_clients:
                providers_needing_clients[batch_provider] = model_config
            # Check cancel_on_startup from global provider config
            provider_config = providers_config.get(batch_provider)
            if provider_config and provider_config.cancel_on_startup:
                providers_cancel_on_startup.add(batch_provider)
    
    if not batch_models:
        log.debug("No models with provider='batch', skipping batch queue manager")
        return None
    
    log.info("Found %d batch models: %s", len(batch_models), batch_models)
    
    try:
        from .queue_manager import BatchQueueManager
        from ..factory import set_batch_queue_manager
        
        # Create batch queue manager with global config
        storage_path = Path(batch_system_config.storage_path)
        
        # Initialize job tracker for tracking our submitted jobs
        job_tracker = BatchJobTracker(storage_path)
        set_job_tracker(job_tracker)
        log.debug("Job tracker initialized at %s", storage_path)
        
        _batch_queue_manager = BatchQueueManager(
            batch_system_config=batch_system_config,
            storage_path=storage_path
        )
        
        # Register batch clients for each provider
        await _register_batch_clients(
            _batch_queue_manager, 
            providers_needing_clients,
            batch_system_config,
            log
        )
        
        # Register globally so LLMFactory can access it
        set_batch_queue_manager(_batch_queue_manager)
        
        # Start the batch queue manager
        await _batch_queue_manager.start()
        log.info("Batch queue manager started (providers: %s)",
                 list(providers_needing_clients.keys()))
        
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
    batch_system_config: Any,
    log: logging.Logger,
) -> None:
    """Register batch API clients for each batch provider.
    
    Args:
        queue_manager: BatchQueueManager instance
        providers_needing_clients: Dict of batch_provider -> model_config
        batch_system_config: Global BatchSystemConfig
        log: Logger instance
    """
    # Backends live in the LLM provider plugins (declared via provides_batch
    # in their plugin.toml); env-key fallback is provider knowledge and
    # happens inside each plugin's factory, which returns None to skip.
    from agent_system.llm import registry

    for batch_provider, model_config in providers_needing_clients.items():
        try:
            backend_factory = registry.get_batch_backend(batch_provider)
            if backend_factory is None:
                log.warning(f"Unknown batch provider: {batch_provider}")
                continue
            client = backend_factory(model_config)
            if client is None:
                log.info("No API key found, skipping %s batch client", batch_provider)
                continue
            queue_manager.register_batch_client(batch_provider, client)
            log.info("Registered %s batch client", batch_provider)
        except Exception as e:
            log.error(f"Failed to register batch client for {batch_provider}: {e}")


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
    4. Clear job tracker reference
    
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
    
    # Clear job tracker reference (keep file for next startup)
    set_job_tracker(None)
    log.debug("Job tracker cleared")


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
