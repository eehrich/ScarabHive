"""Abstract base class for batch provider clients.

Defines the common interface that all batch provider implementations
(OpenAI, Gemini, etc.) must implement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import BatchJob


class BatchProviderClient(ABC):
    """Abstract base class for batch provider clients.

    All batch provider implementations must implement this interface
    to ensure consistent behavior across providers.
    """

    #: The batch provider name this client was REGISTERED under, set by
    #: BatchQueueManager.register_batch_client. The job tracker is keyed by
    #: that name, so a client that hardcodes its own name instead loses every
    #: tracked job the moment the two differ — which is what happened when
    #: `openai` was renamed to `openai_httpx`: startup cancellation looked
    #: under the old key, found nothing, and left paid jobs running.
    provider_name: str = ""

    def _tracker_key(self) -> Optional[str]:
        """Key to look the tracked jobs up under, or None if unregistered."""
        return self.provider_name or None

    @abstractmethod
    async def submit_batch(
        self,
        job: "BatchJob",
        storage_path: Path,
    ) -> str:
        """Submit a batch job to the provider.
        
        Args:
            job: BatchJob containing requests to submit
            storage_path: Path to store temporary files
            
        Returns:
            Provider-specific job ID
        """
        ...
    
    @abstractmethod
    async def get_batch_status(
        self,
        provider_job_id: str,
    ) -> Dict[str, Any]:
        """Get the status of a batch job.
        
        Args:
            provider_job_id: Provider-specific job ID
            
        Returns:
            Dict with at least 'status' key containing BatchStatus value
        """
        ...
    
    @abstractmethod
    async def get_batch_results(
        self,
        job: "BatchJob",
    ) -> List[Dict[str, Any]]:
        """Get results for a completed batch job.
        
        Args:
            job: BatchJob with provider_job_id set
            
        Returns:
            List of result dicts with 'custom_id', 'response', and optionally 'error'
        """
        ...
    
    @abstractmethod
    async def cancel_batch(
        self,
        provider_job_id: str,
    ) -> bool:
        """Cancel a batch job.
        
        Args:
            provider_job_id: Provider-specific job ID
            
        Returns:
            True if cancellation was initiated
        """
        ...
    
    async def list_batches(
        self,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List recent batch jobs from the provider.

        Optional method - not all providers may support this.
        Used for job recovery after server restart.

        Args:
            limit: Maximum number of jobs to return

        Returns:
            List of batch job info dicts, in the PROVIDER's own shape —
            ``describe_listed_batch`` turns one into a common shape.
        """
        return []

    def describe_listed_batch(
        self,
        batch_info: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Normalize ONE entry of ``list_batches`` for job recovery.

        Every provider names these fields differently (``name``/``state`` vs.
        ``id``/``status`` vs. ``id``/``processing_status``). The queue manager
        used to switch on the provider NAME and read the fields itself, with
        an ``else`` branch that assumed the OpenAI shape — so Anthropic
        listings were parsed with the wrong keys and recovered as jobs with an
        empty status and a made-up model name.

        Returns:
            ``{"job_id": str, "status": BatchStatus, "model": str}`` for a job
            still worth recovering, or None when it is finished (or the shape
            is unrecognized). The base implementation returns None: a provider
            that lists batches without describing them skips recovery instead
            of being parsed by guesswork.
        """
        return None
    
    async def close(self) -> None:
        """Close the client and release resources.
        
        Optional cleanup method.
        """
        pass
