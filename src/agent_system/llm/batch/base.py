"""Abstract base class for batch provider clients.

Defines the common interface that all batch provider implementations
(OpenAI, Gemini, etc.) must implement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import BatchJob


class BatchProviderClient(ABC):
    """Abstract base class for batch provider clients.
    
    All batch provider implementations must implement this interface
    to ensure consistent behavior across providers.
    """
    
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
            List of batch job info dicts
        """
        return []
    
    async def close(self) -> None:
        """Close the client and release resources.
        
        Optional cleanup method.
        """
        pass
