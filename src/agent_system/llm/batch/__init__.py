"""LLM Batch API Support Module.

Provides asynchronous batch processing for OpenAI and Gemini APIs
with 50% cost reduction and separate rate limits.

Components:
- BatchQueueManager: Collects and groups requests by model
- BatchJob: Represents a submitted batch job
- OpenAIBatchClient: OpenAI Files + Batch API integration
- GeminiBatchClient: Gemini Batch API integration
- BatchResultDistributor: Maps batch results to original requests
"""

from .queue_manager import BatchQueueManager
from .models import BatchJob, BatchRequest, BatchResult, BatchStatus
from .openai_batch import OpenAIBatchClient
from .gemini_batch import GeminiBatchClient

__all__ = [
    "BatchQueueManager",
    "BatchJob",
    "BatchRequest",
    "BatchResult",
    "BatchStatus",
    "OpenAIBatchClient",
    "GeminiBatchClient",
]
