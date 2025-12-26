"""Batch Queue Manager.

Manages the collection and grouping of LLM requests for batch processing.
Requests are accumulated during a configurable time window, then submitted
as batch jobs to the respective provider APIs.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING
import uuid

from .models import BatchJob, BatchRequest, BatchStatus, BatchMetrics
from ...mcp.status import publish_status, StatusPhase, current_request_id

if TYPE_CHECKING:
    from agent_system.config.models import BatchAPIConfig

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class BatchQueueManager:
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
    
    def __init__(
        self,
        config: Optional[BatchAPIConfig] = None,
        storage_path: Optional[Path] = None,
    ):
        """Initialize the batch queue manager.
        
        Args:
            config: Batch API configuration
            storage_path: Path for storing batch files (default: data/batch/)
        """
        self.config = config
        self.storage_path = storage_path or Path("data/batch")
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # Configuration with defaults
        self._collection_window = (
            config.collection_window_seconds if config else 60.0
        )
        self._max_requests = (
            config.max_requests_per_batch if config else 1000
        )
        self._poll_interval = (
            config.poll_interval_seconds if config else 30.0
        )
        self._max_wait_hours = (
            config.max_wait_hours if config else 24.0
        )
        
        # Request queues by model
        self._queues: Dict[str, List[BatchRequest]] = defaultdict(list)
        self._queue_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        
        # Active batch jobs
        self._active_jobs: Dict[str, BatchJob] = {}
        self._jobs_lock = asyncio.Lock()
        
        # Completed job results cache
        self._completed_jobs: Dict[str, BatchJob] = {}
        
        # Request futures (for callers waiting on results)
        self._request_futures: Dict[str, asyncio.Future] = {}
        
        # Background tasks
        self._submission_tasks: Dict[str, asyncio.Task] = {}
        self._polling_task: Optional[asyncio.Task] = None
        self._running = False
        self._polling_started = False  # Track if polling task was started
        
        # Batch client callbacks (set by register_batch_client)
        self._batch_clients: Dict[str, Any] = {}  # provider -> client
        
        # Metrics
        self._metrics = BatchMetrics()
        
        logger.info(
            f"BatchQueueManager initialized: "
            f"window={self._collection_window}s, "
            f"max_requests={self._max_requests}, "
            f"poll_interval={self._poll_interval}s"
        )
    
    def register_batch_client(self, provider: str, client: Any) -> None:
        """Register a batch client for a provider.
        
        Args:
            provider: Provider name ("openai" or "gemini")
            client: Batch client instance (OpenAIBatchClient or GeminiBatchClient)
        """
        self._batch_clients[provider] = client
        logger.info(f"Registered batch client for provider: {provider}")
    
    def _ensure_polling_started(self) -> None:
        """Ensure polling task is started in the current event loop.
        
        This is called lazily on first request to ensure the polling task
        runs in the same event loop as the agent. This is necessary because
        the CLI uses multiple asyncio.run() calls which create separate
        event loops.
        """
        if self._polling_started:
            return
        
        self._polling_started = True
        self._polling_task = asyncio.create_task(
            self._poll_active_jobs(),
            name="batch_polling"
        )
        logger.debug("Polling task started lazily in current event loop")
    
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
    
    async def stop(self) -> None:
        """Stop the batch queue manager and cleanup."""
        self._running = False
        self._polling_started = False  # Reset for potential restart
        
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
    
    async def submit_request(
        self,
        model: str,
        provider: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        custom_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Submit a request for batch processing.
        
        The request is queued and will be submitted as part of a batch job
        when the collection window expires or max_requests is reached.
        
        Args:
            model: Model name
            provider: Provider name ("openai" or "gemini")
            messages: Chat messages
            tools: Optional tool definitions
            session_id: Optional session ID
            agent_name: Optional agent name
            custom_id: Optional custom ID for correlation
            timeout: Optional timeout in seconds (default: max_wait_hours * 3600)
            
        Returns:
            Response dict when batch completes
            
        Raises:
            asyncio.TimeoutError: If batch doesn't complete within timeout
            RuntimeError: If batch fails
        """
        # Ensure polling task is started in this event loop
        # This is critical for CLI which uses multiple asyncio.run() calls
        self._ensure_polling_started()
        
        # Create request
        # Capture current_request_id for status updates during polling
        try:
            agent_request_id = current_request_id.get()
        except Exception:
            agent_request_id = None
            
        request = BatchRequest(
            request_id=str(uuid.uuid4()),
            custom_id=custom_id or str(uuid.uuid4()),
            model=model,
            messages=messages,
            tools=tools,
            session_id=session_id,
            agent_name=agent_name,
            metadata={"provider": provider, "agent_request_id": agent_request_id},
        )
        
        # Create future for this request
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._request_futures[request.request_id] = future
        
        # Add to queue
        queue_key = f"{provider}:{model}"
        async with self._queue_locks[queue_key]:
            self._queues[queue_key].append(request)
            queue_size = len(self._queues[queue_key])
            
            logger.debug(
                f"Queued request {request.request_id} for {queue_key}, "
                f"queue size: {queue_size}"
            )
            
            # Check if we should submit immediately (max_requests reached)
            if queue_size >= self._max_requests:
                logger.info(
                    f"Queue {queue_key} reached max_requests ({self._max_requests}), "
                    f"triggering immediate submission"
                )
                asyncio.create_task(self._submit_batch(queue_key, provider, model))
            
            # Ensure submission task is running for this queue
            if queue_key not in self._submission_tasks or self._submission_tasks[queue_key].done():
                self._submission_tasks[queue_key] = asyncio.create_task(
                    self._collection_window_task(queue_key, provider, model),
                    name=f"batch_collection_{queue_key}"
                )
                # Publish status update about batch collection starting
                await publish_status(
                    server="batch_queue",
                    message=f"Batch: Collecting requests for {self._collection_window}s before submission",
                    phase=StatusPhase.PROGRESS,
                    meta={"queue": queue_key, "collection_window": self._collection_window}
                )
        
        # Wait for result
        timeout = timeout or (self._max_wait_hours * 3600)
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.error(f"Request {request.request_id} timed out after {timeout}s")
            # Clean up
            self._request_futures.pop(request.request_id, None)
            raise
        except asyncio.CancelledError:
            logger.warning(f"Request {request.request_id} was cancelled")
            self._request_futures.pop(request.request_id, None)
            raise
    
    async def _collection_window_task(
        self,
        queue_key: str,
        provider: str,
        model: str,
    ) -> None:
        """Background task that waits for collection window then submits batch."""
        await asyncio.sleep(self._collection_window)
        
        if not self._running:
            return
        
        await self._submit_batch(queue_key, provider, model)
    
    async def _submit_batch(
        self,
        queue_key: str,
        provider: str,
        model: str,
    ) -> Optional[BatchJob]:
        """Submit the current queue as a batch job.
        
        Args:
            queue_key: Queue identifier (provider:model)
            provider: Provider name
            model: Model name
            
        Returns:
            BatchJob if submitted, None if queue was empty
        """
        async with self._queue_locks[queue_key]:
            requests = self._queues[queue_key]
            if not requests:
                logger.debug(f"Queue {queue_key} is empty, skipping submission")
                return None
            
            # Take all requests and clear queue
            batch_requests = list(requests)
            requests.clear()
        
        logger.info(
            f"Submitting batch for {queue_key} with {len(batch_requests)} requests"
        )
        
        # Create batch job
        job = BatchJob(
            job_id=str(uuid.uuid4()),
            provider=provider,
            model=model,
            status=BatchStatus.PENDING,
            requests=batch_requests,
        )
        
        # Store job
        async with self._jobs_lock:
            self._active_jobs[job.job_id] = job
        
        self._metrics.total_jobs += 1
        self._metrics.total_requests += len(batch_requests)
        
        # Get batch client
        client = self._batch_clients.get(provider)
        if not client:
            logger.error(f"No batch client registered for provider: {provider}")
            job.status = BatchStatus.FAILED
            job.error_message = f"No batch client for provider: {provider}"
            await self._complete_job(job)
            return job
        
        # Submit to provider
        try:
            job.status = BatchStatus.SUBMITTED
            job.submitted_at = _utc_now()
            
            # Create input file and submit batch
            provider_job_id = await client.submit_batch(job, self.storage_path)
            job.provider_job_id = provider_job_id
            
            logger.info(
                f"Batch job {job.job_id} submitted to {provider} as {provider_job_id}"
            )
            
            # Publish status update about batch submission
            await publish_status(
                server="batch_queue",
                message=f"Batch: Job submitted to {provider}, waiting for results (poll interval: {self._poll_interval}s)",
                phase=StatusPhase.PROGRESS,
                meta={"provider": provider, "job_id": provider_job_id, "request_count": len(batch_requests)}
            )
            
        except Exception as e:
            logger.error(f"Failed to submit batch job {job.job_id}: {e}")
            job.status = BatchStatus.FAILED
            job.error_message = str(e)
            await self._complete_job(job)
        
        return job
    
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
        client = self._batch_clients.get(job.provider)
        if not client:
            logger.warning("No batch client for provider: %s", job.provider)
            return
        
        # Get agent_request_id from the first request in the job for status updates
        agent_request_id = None
        if job.requests:
            agent_request_id = job.requests[0].metadata.get("agent_request_id")
        
        try:
            # Get status from provider
            status_info = await client.get_batch_status(job.provider_job_id)
            
            # Update job status
            new_status = status_info.get("status")
            if new_status:
                old_status = job.status
                job.status = BatchStatus(new_status)
                if old_status != job.status:
                    logger.info(
                        f"Batch job {job.job_id} status changed: "
                        f"{old_status.value} -> {job.status.value}"
                    )
                    # Publish status update about job progress
                    await publish_status(
                        server="batch_queue",
                        message=f"Batch: Job status changed to {job.status.value}",
                        phase=StatusPhase.PROGRESS,
                        request_id=agent_request_id,
                        meta={"provider": job.provider, "job_id": job.provider_job_id, "status": job.status.value}
                    )
                else:
                    logger.debug(
                        f"Batch job {job.job_id} status: {job.status.value}"
                    )
                    # Publish status update for every poll cycle to show we're still waiting
                    elapsed = 0
                    if job.submitted_at:
                        elapsed = int((_utc_now() - job.submitted_at).total_seconds())
                    await publish_status(
                        server="batch_queue",
                        message=f"Batch: Polling... status={job.status.value}, elapsed={elapsed}s",
                        phase=StatusPhase.PROGRESS,
                        request_id=agent_request_id,
                        meta={"provider": job.provider, "status": job.status.value, "elapsed_seconds": elapsed}
                    )
            
            # Check if completed
            if job.status == BatchStatus.COMPLETED:
                # Download and process results
                results = await client.get_batch_results(job)
                await self._process_results(job, results)
                await self._complete_job(job)
                
                # Publish status update about batch completion
                await publish_status(
                    server="batch_queue",
                    message=f"Batch: Results received ({job.completed_count} completed, {job.failed_count} failed)",
                    phase=StatusPhase.PROGRESS,
                    request_id=agent_request_id,
                    meta={"provider": job.provider, "completed": job.completed_count, "failed": job.failed_count}
                )
                
            elif job.status in (BatchStatus.FAILED, BatchStatus.EXPIRED, BatchStatus.CANCELLED):
                job.error_message = status_info.get("error", "Unknown error")
                await self._complete_job(job)
                
                # Publish error status
                await publish_status(
                    server="batch_queue",
                    message=f"Batch: Job {job.status.value}: {job.error_message}",
                    phase=StatusPhase.ERROR,
                    request_id=agent_request_id,
                    meta={"provider": job.provider, "status": job.status.value}
                )
                
            # Check for timeout
            elif job.submitted_at:
                elapsed = (_utc_now() - job.submitted_at).total_seconds()
                if elapsed > self._max_wait_hours * 3600:
                    logger.warning(f"Batch job {job.job_id} expired after {elapsed}s")
                    job.status = BatchStatus.EXPIRED
                    job.error_message = f"Exceeded max wait time of {self._max_wait_hours} hours"
                    await self._complete_job(job)
                    
        except Exception as e:
            logger.error(f"Error polling batch job {job.job_id}: {e}")
    
    async def _process_results(
        self,
        job: BatchJob,
        results: List[Dict[str, Any]],
    ) -> None:
        """Process batch results and distribute to waiting callers."""
        for result in results:
            custom_id = result.get("custom_id")
            if not custom_id:
                continue
            
            # Find the original request
            request = job.get_request_by_custom_id(custom_id)
            if not request:
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
            if future and not future.done():
                if request.error:
                    future.set_exception(RuntimeError(request.error))
                else:
                    future.set_result(request.response)
    
    async def _complete_job(self, job: BatchJob) -> None:
        """Mark a job as complete and update metrics."""
        job.completed_at = _utc_now()
        
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
        
        # Move to completed jobs
        async with self._jobs_lock:
            self._active_jobs.pop(job.job_id, None)
            self._completed_jobs[job.job_id] = job
        
        # Notify any remaining waiting callers of failure
        if job.status != BatchStatus.COMPLETED:
            for request in job.requests:
                future = self._request_futures.pop(request.request_id, None)
                if future and not future.done():
                    future.set_exception(
                        RuntimeError(f"Batch job {job.status.value}: {job.error_message}")
                    )
        
        logger.info(
            f"Batch job {job.job_id} completed with status {job.status.value}, "
            f"completed={job.completed_count}, failed={job.failed_count}"
        )
    
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
        
        client = self._batch_clients.get(job.provider)
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
    
    def get_metrics(self) -> BatchMetrics:
        """Get current batch metrics."""
        return self._metrics
    
    def get_active_jobs(self) -> List[BatchJob]:
        """Get list of active batch jobs."""
        return list(self._active_jobs.values())
    
    def get_queue_sizes(self) -> Dict[str, int]:
        """Get current queue sizes by model."""
        return {k: len(v) for k, v in self._queues.items()}
