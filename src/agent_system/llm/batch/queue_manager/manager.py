"""`BatchQueueManager`: the parts put together, with its lifecycle and metrics.

The class combines the mixins of this package over the state of
`_BatchQueueCore` (see core.py for why mixins), and adds what concerns the
manager as a whole: starting, recovering the jobs of an earlier process,
stopping, and the read-only views the batch monitor and callers use.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from agent_system.utils.id import short_id
from ..base import BatchProviderClient
from ..models import BatchJob, BatchMetrics
from .outcome import _JobOutcome
from .polling import _JobPolling
from .submission import _QueueSubmission

logger = logging.getLogger(__name__)


class BatchQueueManager(_QueueSubmission, _JobPolling, _JobOutcome):
    """Manages batch request queuing, submission, and result distribution.
    
    This class is responsible for:
    1. Collecting incoming LLM requests during a time window
    2. Grouping requests by model/provider
    3. Submitting batches when window expires or max_requests reached
    4. Tracking batch job status
    5. Distributing results back to waiting callers
    
    Usage:
        manager = BatchQueueManager(config)
        await manager.start()
        
        # Submit a request and wait for result
        result = await manager.submit_request(
            model="gpt-4o",
            messages=[...],
            tools=[...]
        )
        
        await manager.stop()
    """
    
    async def start(self) -> None:
        """Start the batch queue manager background tasks.
        
        Note: The polling task is now started lazily on first request
        to ensure it runs in the correct event loop.
        """
        if self._running:
            return
        
        self._running = True
        
        # Note: Polling task is started lazily in _ensure_polling_started()
        # to ensure it runs in the same event loop as the agent.
        # This is necessary for CLI which uses multiple asyncio.run() calls.
        
        logger.info("BatchQueueManager started")
    
    async def recover_jobs(self, providers: Optional[set] = None) -> int:
        """Recover active jobs from providers after server restart.
        
        This method queries each registered provider for their active batch jobs
        and adds them to the internal tracking. This allows monitoring of jobs
        that were submitted before a server restart.
        
        Note: Recovered jobs won't have their original request futures, so
        results cannot be delivered to waiting callers. They will still be
        polled and tracked for monitoring purposes.
        
        Args:
            providers: Optional set of provider names to recover from.
                      If None, recovers from all registered providers.
        
        Returns:
            Number of jobs recovered
        """
        recovered_count = 0
        
        # Determine which providers to recover from
        provider_names = [p for p in self.batch_providers() if not providers or p in providers]
        logger.info(f"Starting job recovery, checking {len(provider_names)} providers: {provider_names}")

        for provider in provider_names:
            client = self._client_for(provider)
            if client is None:
                continue
            try:
                if not hasattr(client, 'list_batches'):
                    logger.debug(f"Provider {provider} doesn't support list_batches")
                    continue
                
                # The provider's own client knows its listing shape — this used
                # to switch on the provider NAME here, with an `else` that read
                # OpenAI's keys out of whatever came in. The base
                # implementation always returns None, so a provider that never
                # overrode it would silently recover nothing — a different
                # problem from "this batch is finished", and worth saying once
                # per provider rather than per entry.
                #
                # `is` holds here because both sides read a plain `def` off a
                # CLASS, which hands out the same function object every time --
                # a staticmethod does too. Make it a classmethod and each access
                # builds a fresh binding: this would be False for everyone, the
                # warning would stop, and a provider that never overrode it would
                # go back to recovering nothing in silence. Measured on 3.12.5:
                # def True, staticmethod True, classmethod False.
                if getattr(type(client), "describe_listed_batch", None) is (
                        BatchProviderClient.describe_listed_batch):
                    logger.warning(
                        "Batch provider %s lists batches but does not "
                        "implement describe_listed_batch — no jobs can be "
                        "recovered for it", provider)
                    continue

                logger.debug(f"Listing batches from {provider}...")
                batches = await client.list_batches(limit=50)
                logger.info(f"Found {len(batches)} batches from {provider}")

                for batch_info in batches:
                    logger.debug(f"Processing batch: {batch_info}")
                    described = client.describe_listed_batch(batch_info)
                    if not described:
                        logger.debug(
                            "Skipping finished %s batch: %s", provider, batch_info)
                        continue
                    job_id = described["job_id"]
                    status = described["status"]
                    model = described["model"]

                    # Check if we're already tracking this job
                    already_tracked = any(
                        job.provider_job_id == job_id 
                        for job in self._active_jobs.values()
                    )
                    if already_tracked:
                        logger.debug(f"Skipping already tracked job: {job_id}")
                        continue
                    
                    # Create a recovery job (no original requests/futures)
                    from ..models import BatchJob
                    recovery_job = BatchJob(
                        job_id=f"rec_{short_id()}",
                        provider=provider,
                        model=model,
                        status=status,
                        requests=[],  # No original requests available
                    )
                    recovery_job.provider_job_id = job_id
                    
                    async with self._jobs_lock:
                        self._active_jobs[recovery_job.job_id] = recovery_job
                    
                    recovered_count += 1
                    logger.info(f"Recovered batch job from {provider}: {job_id} (status: {status.value})")
                    
            except Exception as e:
                logger.warning(f"Failed to recover jobs from {provider}: {e}", exc_info=True)
        
        if recovered_count > 0:
            logger.info(f"Recovered {recovered_count} active batch jobs from providers")
            # Start polling if we have recovered jobs
            self._ensure_polling_started()
        else:
            logger.info("No active batch jobs found to recover")
        
        return recovered_count
    
    async def stop(self) -> None:
        """Stop the batch queue manager and cleanup."""
        self._running = False
        
        # Cancel submission tasks
        for task in self._submission_tasks.values():
            task.cancel()
        
        # Cancel polling task
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None
        
        # Cancel any waiting futures
        for future in self._request_futures.values():
            if not future.done():
                future.cancel()
        
        logger.info("BatchQueueManager stopped")
    
    def get_metrics(self) -> BatchMetrics:
        """Get current batch metrics."""
        return self._metrics
    
    def get_active_jobs(self) -> List[BatchJob]:
        """Get list of active batch jobs."""
        return list(self._active_jobs.values())
    
    def get_queue_sizes(self) -> Dict[str, int]:
        """Get current queue sizes by model."""
        return {k: len(v) for k, v in self._queues.items()}
