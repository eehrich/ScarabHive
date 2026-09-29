"""Batch Job Tracker.

Tracks batch job IDs submitted by this AgentSystem instance.
Used to ensure cancel_on_startup only cancels our own jobs, not jobs
from other systems using the same API keys.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Set
import asyncio
import aiofiles

from agent_system.paths import resolve_data_path

logger = logging.getLogger(__name__)


class BatchJobTracker:
    """Tracks batch job IDs per provider for selective cancellation.
    
    This class maintains a persistent record of batch jobs submitted by
    this AgentSystem instance. On startup, only these tracked jobs will
    be cancelled (if cancel_on_startup is enabled), leaving jobs from
    other systems untouched.
    
    The tracker persists to a JSON file to survive restarts.
    
    Usage:
        tracker = BatchJobTracker(storage_path)
        
        # When submitting a job
        await tracker.add_job("gemini", "batches/abc123")
        
        # When job completes or is cancelled
        await tracker.remove_job("gemini", "batches/abc123")
        
        # On startup, get jobs to cancel
        jobs = await tracker.get_tracked_jobs("gemini")
    """
    
    def __init__(self, storage_path: Path):
        """Initialize the job tracker.
        
        Args:
            storage_path: Directory to store the tracking file (a data/...
                path lands in the data directory, agent_system/paths.py)
        """
        self.storage_path = resolve_data_path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.tracker_file = self.storage_path / "tracked_batch_jobs.json"
        self._lock = asyncio.Lock()
        
        # In-memory cache: provider -> set of job_ids
        self._tracked_jobs: Dict[str, Set[str]] = {}
        self._loaded = False
    
    async def _load(self) -> None:
        """Load tracked jobs from disk."""
        if self._loaded:
            return
            
        async with self._lock:
            if self._loaded:
                return
                
            if self.tracker_file.exists():
                try:
                    async with aiofiles.open(self.tracker_file, 'r') as f:
                        content = await f.read()
                        data = json.loads(content)
                        # Convert lists to sets
                        self._tracked_jobs = {
                            provider: set(job_ids) 
                            for provider, job_ids in data.items()
                        }
                        total = sum(len(jobs) for jobs in self._tracked_jobs.values())
                        if total > 0:
                            logger.debug(f"Loaded {total} tracked batch jobs from {self.tracker_file}")
                except Exception as e:
                    logger.warning(f"Failed to load tracked jobs: {e}")
                    self._tracked_jobs = {}
            
            self._loaded = True
    
    async def _save(self) -> None:
        """Save tracked jobs to disk."""
        try:
            # Convert sets to lists for JSON serialization
            data = {
                provider: list(job_ids) 
                for provider, job_ids in self._tracked_jobs.items()
                if job_ids  # Only save non-empty providers
            }
            async with aiofiles.open(self.tracker_file, 'w') as f:
                await f.write(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save tracked jobs: {e}")
    
    async def add_job(self, provider: str, job_id: str) -> None:
        """Add a job to the tracker.
        
        Args:
            provider: Provider name (e.g., "gemini", "openai")
            job_id: Provider-specific job ID (e.g., "batches/abc123")
        """
        await self._load()
        
        async with self._lock:
            if provider not in self._tracked_jobs:
                self._tracked_jobs[provider] = set()
            self._tracked_jobs[provider].add(job_id)
            await self._save()
            logger.debug(f"Tracked batch job: {provider}/{job_id}")
    
    async def remove_job(self, provider: str, job_id: str) -> None:
        """Remove a job from the tracker.
        
        Args:
            provider: Provider name
            job_id: Provider-specific job ID
        """
        await self._load()
        
        async with self._lock:
            if provider in self._tracked_jobs:
                self._tracked_jobs[provider].discard(job_id)
                # Clean up empty provider entry
                if not self._tracked_jobs[provider]:
                    del self._tracked_jobs[provider]
                await self._save()
                logger.debug(f"Untracked batch job: {provider}/{job_id}")
    
    async def get_tracked_jobs(self, provider: str) -> Set[str]:
        """Get all tracked jobs for a provider.
        
        Args:
            provider: Provider name
            
        Returns:
            Set of job IDs for this provider
        """
        await self._load()
        
        return self._tracked_jobs.get(provider, set()).copy()
    
    async def clear_provider(self, provider: str) -> int:
        """Clear all tracked jobs for a provider.
        
        Args:
            provider: Provider name
            
        Returns:
            Number of jobs cleared
        """
        await self._load()
        
        async with self._lock:
            count = len(self._tracked_jobs.get(provider, set()))
            if provider in self._tracked_jobs:
                del self._tracked_jobs[provider]
                await self._save()
            return count
    
    async def get_all_tracked_jobs(self) -> Dict[str, Set[str]]:
        """Get all tracked jobs for all providers.
        
        Returns:
            Dict mapping provider -> set of job IDs
        """
        await self._load()
        
        return {
            provider: job_ids.copy() 
            for provider, job_ids in self._tracked_jobs.items()
        }


# Global tracker instance (set during initialization)
_job_tracker: BatchJobTracker | None = None


def get_job_tracker() -> BatchJobTracker | None:
    """Get the global job tracker instance."""
    return _job_tracker


def set_job_tracker(tracker: BatchJobTracker | None) -> None:
    """Set the global job tracker instance."""
    global _job_tracker
    _job_tracker = tracker
