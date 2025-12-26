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
    
    for model_name, model_config in config.llm_system.models.items():
        if model_config.batch and model_config.batch.enabled:
            batch_enabled_models.append(model_name)
            normalized = _normalize_provider(model_config.provider)
            if normalized not in providers_needing_clients:
                providers_needing_clients[normalized] = model_config
    
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
        
        # Handle existing jobs from providers
        if batch_config.cancel_on_startup:
            # Cancel any pending jobs from previous runs
            cancelled = await _cancel_all_provider_batches(_batch_queue_manager, log)
            if cancelled > 0:
                log.info("Cancelled %d pending batch jobs from previous runs", cancelled)
        else:
            # Recover active jobs for monitoring
            recovered = await _batch_queue_manager.recover_jobs()
            if recovered > 0:
                log.info("Recovered %d active batch jobs from providers", recovered)
        
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


async def _cancel_all_provider_batches(
    queue_manager: "BatchQueueManager",
    log: logging.Logger,
) -> int:
    """Cancel all pending batches from all registered providers.
    
    Args:
        queue_manager: BatchQueueManager instance
        log: Logger instance
        
    Returns:
        Total number of batches cancelled
    """
    total_cancelled = 0
    
    for provider, client in queue_manager._batch_clients.items():
        try:
            if hasattr(client, 'cancel_all_pending_batches'):
                cancelled = await client.cancel_all_pending_batches()
                total_cancelled += cancelled
                if cancelled > 0:
                    log.info(f"Cancelled {cancelled} pending batches from {provider}")
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
    
    Returns:
        BatchQueueManager instance if initialized, None otherwise
    """
    return _batch_queue_manager


def is_batch_enabled() -> bool:
    """Check if batch processing is currently enabled.
    
    Returns:
        True if batch queue manager is running, False otherwise
    """
    return _batch_queue_manager is not None and _batch_queue_manager._running
