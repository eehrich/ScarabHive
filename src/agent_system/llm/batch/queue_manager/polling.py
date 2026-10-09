"""The polling loop: each submitted job's status at its provider, and what follows from it.

`_poll_active_jobs` runs as one task per event loop (`_ensure_polling_started`
starts it lazily, and again if it died) and asks the provider about every
active job in turn; `_poll_job` acts on the answer -- results to fetch, a retry
after a cancellation or an expiry, a local timeout -- and `_retry_job`
resubmits a job.
"""

from __future__ import annotations

import asyncio
import logging

from ..job_tracker import get_job_tracker
from ..models import BatchJob, BatchStatus, _utc_now
from .core import _BatchQueueCore

logger = logging.getLogger(__name__)


class _JobPolling(_BatchQueueCore):
    """Mixin of `BatchQueueManager`: polling the submitted jobs and retrying them."""

    def _ensure_polling_started(self) -> None:
        """Ensure polling task is started and running in the current event loop.
        
        This is called lazily on first request to ensure the polling task
        runs in the same event loop as the agent. This is necessary because
        the CLI uses multiple asyncio.run() calls which create separate
        event loops.
        
        Also handles task restart if the previous task died unexpectedly.
        """
        # Check if task exists and is still running
        if self._polling_task is not None and not self._polling_task.done():
            return  # Task is running, nothing to do
        
        # Task doesn't exist or has finished - check if it died unexpectedly
        if self._polling_task is not None and self._polling_task.done():
            # Task finished - check if it was an error
            try:
                # This will raise if the task had an exception
                exc = self._polling_task.exception()
                if exc is not None:
                    logger.error(
                        f"Polling task died with exception: {exc}. Restarting..."
                    )
            except asyncio.CancelledError:
                logger.debug("Previous polling task was cancelled, restarting...")
            except asyncio.InvalidStateError:
                pass  # Task not done yet (shouldn't happen given the check above)
        
        self._polling_task = asyncio.create_task(
            self._poll_active_jobs(),
            name="batch_polling"
        )
        logger.debug("Polling task started lazily in current event loop")
    
    async def _poll_active_jobs(self) -> None:
        """Background task that polls active batch jobs for status updates."""
        logger.debug("Batch polling task started, interval=%ss", self._poll_interval)
        poll_cycle = 0
        while self._running:
            try:
                poll_cycle += 1
                logger.debug("Batch polling: sleeping for %ss (cycle %d)", self._poll_interval, poll_cycle)
                await asyncio.sleep(self._poll_interval)
                
                if not self._running:
                    logger.debug("Batch polling task stopping (not running)")
                    break
                
                # Check for stale pending requests (stuck in queue without submission task)
                await self._check_stale_queues()
                
                # Get list of active jobs
                async with self._jobs_lock:
                    jobs_to_poll = [
                        job for job in self._active_jobs.values()
                        if not job.is_terminal and job.provider_job_id
                    ]
                
                if jobs_to_poll:
                    logger.debug("Batch polling: found %d active jobs to poll", len(jobs_to_poll))
                
                for job in jobs_to_poll:
                    await self._poll_job(job)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in batch polling loop: {e}")

    async def _poll_job(self, job: BatchJob) -> None:
        """Poll a single batch job for status updates."""
        logger.debug("Polling batch job %s (provider: %s)", job.job_id[:8], job.provider)
        client = self._client_for(job.provider)
        if not client:
            logger.warning("No batch client for provider: %s", job.provider)
            return
        
        try:
            # Get status from provider
            status_info = await client.get_batch_status(job.provider_job_id)
            
            # Update job status
            new_status = status_info.get("status")
            if new_status:
                old_status = job.status
                job.status = BatchStatus(new_status)
                if old_status != job.status:
                    logger.debug(
                        f"Batch job {job.job_id} status changed: "
                        f"{old_status.value} -> {job.status.value}"
                    )
                    # Report status change to waiting callers
                    await self._report_job_status(
                        job, f"Batch {job.status.value}: {job.model}"
                    )
            
            # Check if completed
            if job.status == BatchStatus.COMPLETED:
                # Download and process results
                try:
                    await self._report_job_status(job, f"Batch downloading: {job.model}")
                    results = await client.get_batch_results(job)
                    await self._process_results(job, results)
                    await self._complete_job(job)
                except Exception as e:
                    # CRITICAL: Ensure job is removed from _active_jobs even on failure
                    # Otherwise the job stays stuck with status=COMPLETED forever
                    logger.error(
                        f"Failed to download/process results for completed batch job {job.job_id}: {e}"
                    )
                    job.status = BatchStatus.FAILED
                    job.error_message = f"Failed to retrieve results: {e}"
                    await self._complete_job(job)
                
            elif job.status == BatchStatus.CANCELLED:
                # Server-side cancellation - retry if under limit
                if job.retry_count < self._max_retries:
                    job.retry_count += 1
                    logger.warning(
                        f"Batch job {job.job_id} cancelled server-side, "
                        f"retrying ({job.retry_count}/{self._max_retries})"
                    )
                    await self._report_job_status(
                        job, f"Batch retry {job.retry_count}/{self._max_retries}: {job.model}"
                    )
                    await self._retry_job(job)
                else:
                    logger.error(
                        f"Batch job {job.job_id} cancelled after {job.retry_count} retries, giving up"
                    )
                    job.error_message = f"Cancelled after {job.retry_count} retries"
                    await self._complete_job(job)
                    
            elif job.status == BatchStatus.FAILED:
                job.error_message = status_info.get("error", "Unknown error")
                await self._report_job_status(job, f"Batch failed: {job.model}")
                await self._complete_job(job)
                
            elif job.status == BatchStatus.EXPIRED:
                # Provider-side expiration - retry if under limit
                if job.retry_count < self._max_retries:
                    job.retry_count += 1
                    logger.warning(
                        f"Batch job {job.job_id} expired on provider, "
                        f"retrying ({job.retry_count}/{self._max_retries})"
                    )
                    await self._report_job_status(
                        job, f"Batch retry {job.retry_count}/{self._max_retries}: {job.model}"
                    )
                    await self._retry_job(job)
                else:
                    await self._report_job_status(job, f"Batch expired: {job.model}")
                    job.error_message = status_info.get("error", "Batch expired on provider")
                    await self._complete_job(job)
                
            # Check for local timeout
            elif job.submitted_at:
                elapsed = (_utc_now() - job.submitted_at).total_seconds()
                if elapsed > self._max_wait_hours * 3600:
                    # Local timeout - retry if under limit
                    if job.retry_count < self._max_retries:
                        job.retry_count += 1
                        logger.warning(
                            f"Batch job {job.job_id} expired locally after {elapsed:.0f}s, "
                            f"retrying ({job.retry_count}/{self._max_retries})"
                        )
                        await self._report_job_status(
                            job, f"Batch retry {job.retry_count}/{self._max_retries}: {job.model}"
                        )
                        await self._retry_job(job)
                    else:
                        logger.warning(
                            f"Batch job {job.job_id} expired after {elapsed:.0f}s "
                            f"and {job.retry_count} retries, giving up"
                        )
                        # Cancel at provider to prevent resource leak
                        if job.provider_job_id:
                            try:
                                logger.info(f"Cancelling expired job {job.provider_job_id} at provider")
                                await client.cancel_batch(job.provider_job_id)
                            except Exception as cancel_error:
                                logger.warning(f"Failed to cancel expired job at provider: {cancel_error}")
                        
                        job.status = BatchStatus.EXPIRED
                        job.error_message = f"Exceeded max wait time of {self._max_wait_hours} hours after {job.retry_count} retries"
                        await self._report_job_status(job, f"Batch expired: {job.model}")
                        await self._complete_job(job)
                    
        except Exception as e:
            logger.error(f"Error polling batch job {job.job_id}: {e}")
    
    async def _retry_job(self, job: BatchJob) -> None:
        """Retry a cancelled batch job by resubmitting it.
        
        Args:
            job: The BatchJob to retry
        """
        client = self._client_for(job.provider)
        if not client:
            logger.error(f"No batch client for provider {job.provider}, cannot retry")
            job.error_message = f"No batch client for provider: {job.provider}"
            await self._complete_job(job)
            return
        
        try:
            # Reset job state for resubmission
            job.status = BatchStatus.SUBMITTED
            job.submitted_at = _utc_now()
            job.provider_job_id = None
            job.error_message = None
            
            # Resubmit to provider
            provider_job_id = await client.submit_batch(job, self.storage_path)
            job.provider_job_id = provider_job_id
            
            # Track the new job ID for selective cancellation on restart
            tracker = get_job_tracker()
            if tracker:
                await tracker.add_job(job.provider, provider_job_id)
            
            logger.info(
                f"Batch job {job.job_id} resubmitted as {provider_job_id} "
                f"(retry {job.retry_count}/{self._max_retries})"
            )
            
        except Exception as e:
            logger.error(f"Failed to retry batch job {job.job_id}: {e}")
            job.status = BatchStatus.FAILED
            job.error_message = f"Retry failed: {e}"
            await self._complete_job(job)
