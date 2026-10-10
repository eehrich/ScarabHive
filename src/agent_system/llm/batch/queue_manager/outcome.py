"""How a batch job ends: its results, its completion, its cancellation.

Every path that ends a job goes through `_complete_job`, which records the
metrics, moves the job to the completed ones and resolves every future still
waiting on it; `_process_results` hands each caller its own result first, and
the cancellation methods end a job -- or take a request out of its queue --
on a caller's or the server's behalf.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..job_tracker import get_job_tracker
from ..models import BatchJob, BatchStatus, _utc_now
from .core import _BatchQueueCore

logger = logging.getLogger(__name__)


class _JobOutcome(_BatchQueueCore):
    """Mixin of `BatchQueueManager`: results, completion and cancellation of jobs."""

    async def _process_results(
        self,
        job: BatchJob,
        results: List[Dict[str, Any]],
    ) -> None:
        """Process batch results and distribute to waiting callers."""
        # Recovered jobs have no original requests - skip result processing
        is_recovered = job.job_id.startswith("rec_")
        
        for result in results:
            custom_id = result.get("custom_id")
            if not custom_id:
                continue
            
            # Find the original request
            request = job.get_request_by_custom_id(custom_id)
            if not request:
                # Only warn for non-recovered jobs (recovered jobs have no requests by design)
                if not is_recovered:
                    logger.warning(f"No request found for custom_id: {custom_id}")
                continue
            
            # Store result on request
            if result.get("error"):
                request.error = result.get("error", {}).get("message", "Unknown error")
                job.failed_count += 1
            else:
                request.response = result.get("response", {})
                job.completed_count += 1
            
            # Notify waiting caller
            future = self._request_futures.pop(request.request_id, None)
            if future and not future.done() and not future.cancelled():
                if request.error:
                    future.set_exception(RuntimeError(request.error))
                else:
                    future.set_result(request.response)
    
    async def _complete_job(self, job: BatchJob, propagate_error: Optional[Exception] = None) -> None:
        """Mark a job as complete and update metrics.
        
        Args:
            job: The batch job to complete
            propagate_error: If provided, set this exception on all waiting futures
                            instead of a generic RuntimeError. Used for quota/rate limit
                            errors that should trigger fallback.
        """
        job.completed_at = _utc_now()
        
        # Calculate and record processing time
        if job.submitted_at and job.completed_at:
            processing_time = (job.completed_at - job.submitted_at).total_seconds()
            self._metrics.add_processing_time(processing_time)
        
        # Update metrics
        if job.status == BatchStatus.COMPLETED:
            self._metrics.completed_jobs += 1
            self._metrics.completed_requests += job.completed_count
            self._metrics.failed_requests += job.failed_count
        elif job.status == BatchStatus.FAILED:
            self._metrics.failed_jobs += 1
            self._metrics.failed_requests += job.request_count
        elif job.status == BatchStatus.CANCELLED:
            self._metrics.cancelled_jobs += 1
        
        # Remove from job tracker (job is finished)
        if job.provider_job_id:
            tracker = get_job_tracker()
            if tracker:
                await tracker.remove_job(job.provider, job.provider_job_id)
        
        # Move to completed jobs and cleanup request-to-job mapping
        async with self._jobs_lock:
            self._active_jobs.pop(job.job_id, None)
            
            # Add to completed jobs with LRU eviction to prevent memory leak
            self._completed_jobs[job.job_id] = job
            
            # Evict oldest entries if over limit
            if len(self._completed_jobs) > self._max_completed_jobs:
                # Dict maintains insertion order in Python 3.7+, evict oldest
                keys_to_remove = list(self._completed_jobs.keys())[:-self._max_completed_jobs]
                for key in keys_to_remove:
                    del self._completed_jobs[key]
                logger.debug(f"Evicted {len(keys_to_remove)} old completed jobs from cache")
            
            # Clean up request-to-job mapping and status scopes for this job's requests
            for request in (job.requests or []):
                self._request_to_job.pop(request.request_id, None)
                self._request_status_scopes.pop(request.request_id, None)
        
        # Notify any remaining waiting callers of failure
        if job.status != BatchStatus.COMPLETED:
            # For cancelled jobs, cancel the futures (don't set exception)
            # This prevents "Future exception was never retrieved" warnings
            # when the caller has already stopped waiting due to CancellationToken
            if job.status == BatchStatus.CANCELLED:
                for request in (job.requests or []):
                    future = self._request_futures.pop(request.request_id, None)
                    if future and not future.done():
                        future.cancel()
            else:
                # For other failures, set exception so callers get proper error
                # If propagate_error is set, use it (for quota/rate limit errors)
                # Otherwise use generic RuntimeError
                error_to_set = propagate_error or RuntimeError(f"Batch job {job.status.value}: {job.error_message}")
                
                for request in (job.requests or []):
                    future = self._request_futures.pop(request.request_id, None)
                    if future and not future.done():
                        # Only set exception if not already cancelled
                        # (cancelled futures should not have exceptions set on them)
                        if not future.cancelled():
                            try:
                                future.set_exception(error_to_set)
                            except Exception as e:
                                # Future might be in invalid state, log and continue
                                logger.debug(f"Could not set exception on future for {request.request_id}: {e}")
        
        logger.debug(
            f"Batch job {job.job_id} completed with status {job.status.value}, "
            f"completed={job.completed_count}, failed={job.failed_count}"
        )
        
        # Clear request data to free memory (messages, tools can be large)
        # Keep only metadata for debugging -- and the input estimate, which is
        # counted from the messages
        job.metadata["estimated_input_tokens"] = job.estimated_input_tokens
        for request in (job.requests or []):
            request.messages = []  # Clear large message payloads
            request.tools = None   # Clear tool definitions
    
    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a batch job.
        
        Args:
            job_id: Job ID to cancel
            
        Returns:
            True if cancellation was initiated
        """
        async with self._jobs_lock:
            job = self._active_jobs.get(job_id)
        
        if not job:
            logger.warning(f"Job {job_id} not found for cancellation")
            return False
        
        if job.is_terminal:
            logger.warning(f"Job {job_id} is already in terminal state: {job.status}")
            return False
        
        client = self._client_for(job.provider)
        if not client:
            return False
        
        try:
            job.status = BatchStatus.CANCELLING
            await client.cancel_batch(job.provider_job_id)
            job.status = BatchStatus.CANCELLED
            await self._complete_job(job)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel batch job {job_id}: {e}")
            return False
    
    async def cancel_request(self, request_id: str) -> bool:
        """Cancel a specific request and its associated batch job at the provider.
        
        This is called when a user cancels an agent task. It will:
        1. Remove the request from its queue (if not yet submitted)
        2. Cancel the batch job at the provider API (if already submitted)
        3. Notify waiting callers with CancelledError
        
        Args:
            request_id: The request ID to cancel
            
        Returns:
            True if cancellation was successful
        """
        # First, try to remove from queue (not yet submitted)
        removed_from_queue = False
        for queue_key in list(self._queues.keys()):
            async with self._queue_locks[queue_key]:
                original_len = len(self._queues[queue_key])
                self._queues[queue_key] = [
                    r for r in self._queues[queue_key] 
                    if r.request_id != request_id
                ]
                if len(self._queues[queue_key]) < original_len:
                    removed_from_queue = True
                    logger.info(f"Request {request_id} removed from queue before submission")
                    break
        
        if removed_from_queue:
            # Notify waiting caller
            future = self._request_futures.pop(request_id, None)
            if future and not future.done():
                future.cancel()
            return True
        
        # Request was already submitted - find and cancel the job
        job_id = self._request_to_job.get(request_id)
        if not job_id:
            logger.debug(f"Request {request_id} not found in any job")
            return False
        
        async with self._jobs_lock:
            job = self._active_jobs.get(job_id)
        
        if not job:
            logger.debug(f"Job {job_id} for request {request_id} not found")
            return False
        
        if job.is_terminal:
            logger.debug(f"Job {job_id} already in terminal state: {job.status}")
            return False
        
        # Cancel the job at the provider
        if job.provider_job_id:
            client = self._client_for(job.provider)
            if client:
                try:
                    logger.info(
                        f"Cancelling batch job {job.provider_job_id} at {job.provider} "
                        f"(triggered by request {request_id})"
                    )
                    job.status = BatchStatus.CANCELLING
                    await client.cancel_batch(job.provider_job_id)
                    job.status = BatchStatus.CANCELLED
                    job.error_message = f"Cancelled by user (request {request_id})"
                    await self._complete_job(job)
                    logger.info(f"Successfully cancelled batch job {job.provider_job_id}")
                    return True
                except Exception as e:
                    logger.error(f"Failed to cancel batch job at provider: {e}")
                    # Still mark as cancelled locally
                    job.status = BatchStatus.CANCELLED
                    job.error_message = f"Cancel request sent (provider error: {e})"
                    await self._complete_job(job)
                    return True
        
        return False
    
    async def cancel_all_jobs(self) -> int:
        """Cancel all active batch jobs.
        
        Returns:
            Number of jobs cancelled
        """
        async with self._jobs_lock:
            jobs = list(self._active_jobs.values())
        
        cancelled = 0
        for job in jobs:
            if await self.cancel_job(job.job_id):
                cancelled += 1
        
        return cancelled
