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

import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Any

from .job_tracker import BatchJobTracker, get_job_tracker, set_job_tracker

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig
    from .queue_manager import BatchQueueManager

logger = logging.getLogger(__name__)

# The ONE batch queue manager of the process. llm.factory reads and sets it
# through the functions below; it used to keep a second global of its own, and
# init_batch_system then built a new manager next to the one the factory had
# already created lazily -- clients wrapped before init stayed bound to that
# one, which nobody ever started.
_batch_queue_manager: Optional["BatchQueueManager"] = None
_batch_manager_config: Optional["AgentSystemConfig"] = None  # config for lazy creation


def set_batch_queue_manager(manager: Optional["BatchQueueManager"]) -> None:
    """Set (or clear, with None) the global batch queue manager."""
    global _batch_queue_manager
    _batch_queue_manager = manager
    if manager:
        logger.info("Batch queue manager registered globally")


def set_batch_config(config: "AgentSystemConfig") -> None:
    """Store the config the manager is created from lazily, on first use."""
    global _batch_manager_config
    _batch_manager_config = config


def get_batch_queue_manager() -> Optional["BatchQueueManager"]:
    """The global batch queue manager -- created (not started) on first use
    when set_batch_config gave a config that enables batch."""
    global _batch_queue_manager
    if _batch_queue_manager is None and _batch_manager_config is not None:
        _batch_queue_manager = _create_batch_queue_manager(_batch_manager_config, logger, "batch queue manager")
        if _batch_queue_manager is not None:
            logger.info("Batch queue manager created (lazy init)")
    return _batch_queue_manager


def _collect_batch_providers(
    config: "AgentSystemConfig",
    batch_system_config: Any,
    log: logging.Logger,
) -> tuple:
    """Models with provider='batch', and what they need: (batch_models,
    providers_needing_clients (batch_provider -> its models' configs, in
    config order), providers with cancel_on_startup)."""
    batch_models = []
    providers_needing_clients: dict = {}  # batch_provider -> [model_config, ...]
    providers_cancel_on_startup: set = set()  # providers that need cancel_on_startup
    
    for model_name, model_config in config.llm_system.models.items():
        if model_config.provider == "batch":
            if not model_config.batch_provider:
                log.warning("Model %s has provider='batch' but no batch_provider", model_name)
                continue
            batch_models.append(model_name)
            batch_provider = model_config.batch_provider
            providers_needing_clients.setdefault(batch_provider, []).append(model_config)
            # Check cancel_on_startup from global provider config
            provider_config = batch_system_config.providers.get(batch_provider)
            if provider_config and provider_config.cancel_on_startup:
                providers_cancel_on_startup.add(batch_provider)
    return batch_models, providers_needing_clients, providers_cancel_on_startup


def _create_batch_queue_manager(
    config: "AgentSystemConfig",
    log: logging.Logger,
    skipping: str,
) -> Optional["BatchQueueManager"]:
    """A new, unstarted manager when the config enables batch, else None.

    Installs the job tracker for its storage path. Does not register it
    globally; the callers do.
    """
    batch_system_config = _enabled_batch_config(config, log, skipping)
    if batch_system_config is None:
        return None
    
    batch_models = [
        name for name, m in config.llm_system.models.items()
        if m.provider == "batch"
    ]
    if not batch_models:
        log.debug("No models with provider='batch', skipping %s", skipping)
        return None
    
    log.info("Found %d batch models: %s", len(batch_models), batch_models)
    
    from .queue_manager import BatchQueueManager
    
    # Initialize job tracker for tracking our submitted jobs
    storage_path = _install_job_tracker(batch_system_config, log)
    
    # Create batch queue manager with global config
    return BatchQueueManager(
        batch_system_config=batch_system_config,
        storage_path=storage_path
    )


def _enabled_batch_config(
    config: "AgentSystemConfig",
    log: logging.Logger,
    skipping: str,
) -> Optional[Any]:
    """The global BatchSystemConfig when batch can run at all, else None.

    The first checks of setup_batch_queue_manager_sync and init_batch_system:
    an LLM system, a batch section, at least one enabled provider. Each
    "no" is logged at debug level; ``skipping`` names what the caller skips
    ("batch queue manager", "batch initialization").
    """
    if not config.llm_system:
        log.debug("No LLM system configured, skipping batch initialization")
        return None

    # Check for global batch config
    batch_system_config = config.llm_system.batch
    if not batch_system_config:
        log.debug("No batch system config, skipping %s", skipping)
        return None

    # Check if any provider is enabled
    providers_config = batch_system_config.providers
    if not any(p.enabled for p in providers_config.values()):
        log.debug("No batch providers enabled, skipping %s", skipping)
        return None
    return batch_system_config


def _install_job_tracker(batch_system_config: Any, log: logging.Logger) -> Path:
    """Install the job tracker for the batch storage path; returns that path."""
    storage_path = Path(batch_system_config.storage_path)
    job_tracker = BatchJobTracker(storage_path)
    set_job_tracker(job_tracker)
    log.debug("Job tracker initialized at %s", storage_path)
    return storage_path


async def _start_and_settle_jobs(
    manager: "BatchQueueManager",
    providers_needing_clients: dict,
    providers_cancel_on_startup: set,
    log: logging.Logger,
) -> None:
    """Start the manager, then cancel or recover each provider's existing jobs.

    The last step of start_batch_queue_manager and init_batch_system:
    providers with cancel_on_startup lose their pending jobs, every other
    provider's active jobs are recovered.
    """
    # Start the batch queue manager
    await manager.start()
    log.info("Batch queue manager started (providers: %s)",
             list(providers_needing_clients.keys()))

    # Handle existing jobs from providers - per-provider cancel_on_startup
    if providers_cancel_on_startup:
        # Cancel pending jobs only for providers with cancel_on_startup=true
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
    log = custom_logger or logger
    
    if _batch_queue_manager is not None:
        log.debug("Batch queue manager already created, skipping")
        return _batch_queue_manager
    
    try:
        manager = _create_batch_queue_manager(config, log, "batch queue manager")
        if manager is None:
            return None
        
        # Register globally so the factory can access it immediately
        set_batch_queue_manager(manager)
        log.info("Batch queue manager created and registered (not yet started)")
        
        return manager
                    
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
    log = custom_logger or logger
    
    # The global manager (may have been created lazily)
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
            _install_job_tracker(batch_system_config, log)
        
        # Collect batch providers from models with provider='batch'
        _, providers_needing_clients, providers_cancel_on_startup = _collect_batch_providers(
            config, batch_system_config, log)
        
        # Register batch clients for each provider
        await _register_batch_clients(
            manager, 
            providers_needing_clients,
            batch_system_config,
            log
        )
        
        await _start_and_settle_jobs(
            manager, providers_needing_clients, providers_cancel_on_startup, log)
                
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
       -- or takes the global one, when it was already created (lazily or
       by setup_batch_queue_manager_sync)
    3. Registers batch clients for each provider (OpenAI, Gemini)
    4. Starts the background polling/submission tasks
    5. Registers the manager globally so the factory can access it
    
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
    log = custom_logger or logger
    
    batch_system_config = _enabled_batch_config(config, log, "batch initialization")
    if batch_system_config is None:
        return None
    
    # Check for models with provider='batch' and collect their batch_provider info
    batch_models, providers_needing_clients, providers_cancel_on_startup = _collect_batch_providers(
        config, batch_system_config, log)
    
    if not batch_models:
        log.debug("No models with provider='batch', skipping batch queue manager")
        return None
    
    try:
        # The manager the factory may already have created (and wrapped
        # clients around) -- a second one would leave those bound to a
        # manager nobody starts
        manager = get_batch_queue_manager()
        if manager is not None and manager._running:
            log.debug("Batch queue manager already running")
            return manager
        if manager is None:
            manager = _create_batch_queue_manager(config, log, "batch initialization")
            if manager is None:
                return None
        elif get_job_tracker() is None:
            _install_job_tracker(batch_system_config, log)
        
        # Register batch clients for each provider
        await _register_batch_clients(
            manager, 
            providers_needing_clients,
            batch_system_config,
            log
        )
        
        # Register globally so the factory can access it
        set_batch_queue_manager(manager)
        
        await _start_and_settle_jobs(
            manager, providers_needing_clients, providers_cancel_on_startup, log)
        
        return manager
                    
    except Exception as e:
        log.error("Failed to initialize batch queue manager: %s", e, exc_info=True)
        # Don't fail startup - just log and continue without batch
        return None


def _build_batch_client(backend_factory: Any, model_configs: list) -> Any:
    """The backend, built from the first of its models' entries, knowing all of them.

    One backend serves every model with its batch_provider, and the line it
    writes for a request must take THAT model's settings (whether it takes
    images, its reasoning setup): with the first entry only, the first model's
    spoke for all of them.
    """
    client = backend_factory(model_configs[0])
    if client is not None:
        # By the API model id a request carries; the first entry naming one wins.
        client.model_configs = {}
        for cfg in model_configs:
            client.model_configs.setdefault(cfg.model, cfg)
    return client


async def _register_batch_clients(
    queue_manager: "BatchQueueManager",
    providers_needing_clients: dict,
    batch_system_config: Any,
    log: logging.Logger,
) -> None:
    """Register batch API clients for each batch provider.
    
    Args:
        queue_manager: BatchQueueManager instance
        providers_needing_clients: Dict of batch_provider -> its models' configs
        batch_system_config: Global BatchSystemConfig
        log: Logger instance
    """
    # Backends live in the LLM provider plugins (declared via provides_batch
    # in their plugin.toml); env-key fallback is provider knowledge and
    # happens inside each plugin's factory, which returns None to skip.
    from agent_system.llm import registry

    for batch_provider, model_configs in providers_needing_clients.items():
        try:
            backend_factory = registry.get_batch_backend(batch_provider)
            if backend_factory is None:
                log.warning(f"Unknown batch provider: {batch_provider}")
                continue
            # The plugin is resolved (manifest scan, import) HERE, so an
            # unknown or broken provider still shows up at startup; only the
            # client -- SDK import, HTTP client, API key -- waits for first use.
            queue_manager.register_batch_client_factory(
                batch_provider, functools.partial(_build_batch_client, backend_factory, model_configs))
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
    tracker = get_job_tracker()

    for provider in queue_manager.batch_providers():
        # Only cancel for providers in the cancel set
        if provider not in providers_to_cancel:
            log.debug(f"Skipping cancel for provider {provider} (cancel_on_startup=false)")
            continue

        # Ask the tracker BEFORE building the client: cancel_all_pending_batches
        # only ever cancels tracked jobs, so with nothing tracked there is
        # nothing to do -- and no reason to import an SDK for it.
        tracked = await tracker.get_tracked_jobs(provider) if tracker else set()
        if not tracked:
            log.debug(f"No tracked {provider} batch jobs to cancel")
            continue

        try:
            client = queue_manager._client_for(provider)
            if client is not None and hasattr(client, 'cancel_all_pending_batches'):
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
            await _batch_queue_manager.stop()
            _batch_queue_manager = None
            log.info("Batch queue manager stopped")
        except Exception as e:
            log.error(f"Error stopping batch queue manager: {e}")
    
    # Clear job tracker reference (keep file for next startup)
    set_job_tracker(None)
    log.debug("Job tracker cleared")


def is_batch_enabled() -> bool:
    """Check if batch processing is currently enabled.
    
    Returns:
        True if batch queue manager is running, False otherwise
    """
    return _batch_queue_manager is not None and _batch_queue_manager._running
