"""LLM Batch API Support Module.

Provides asynchronous batch processing with 50% cost reduction and
separate rate limits.

Components:
- BatchQueueManager: Collects and groups requests by model
- BatchLLMClient: Wrapper for transparent batch processing
- BatchProviderClient: Abstract base class for provider implementations
- BatchJob: Represents a submitted batch job

The provider-specific backends (OpenAI, Gemini, Anthropic) live in their
LLM provider plugins under src/plugins/ and are looked up through
agent_system.llm.registry.get_batch_backend().
"""

from .base import BatchProviderClient
from .queue_manager import BatchQueueManager
from .models import BatchJob, BatchRequest, BatchResult, BatchStatus
from .batch_client import BatchLLMClient

__all__ = [
    "BatchProviderClient",
    "BatchQueueManager",
    "BatchLLMClient",
    "BatchJob",
    "BatchRequest",
    "BatchResult",
    "BatchStatus",
]
