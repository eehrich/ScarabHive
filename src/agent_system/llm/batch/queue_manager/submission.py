"""Queuing a request, the collection window, submitting a batch -- and recovering a stalled queue.

The path of a request into a batch job: `submit_request` queues it per
``provider:model`` and waits for its future, `_collection_window_task` submits
a queue when its window has passed, `_submit_batch` hands the queue to the
provider as one job, and `_check_stale_queues` -- run by the polling loop --
submits a queue whose collection task is gone.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
import uuid

from agent_system.utils.id import short_id
from ..job_tracker import get_job_tracker
from ..models import BatchJob, BatchRequest, BatchStatus, _utc_now
from ...models import LLMQuotaExhaustedError, LLMRateLimitError
from .core import _BatchQueueCore

logger = logging.getLogger(__name__)


class _QueueSubmission(_BatchQueueCore):
    """Mixin of `BatchQueueManager`: from a caller's request to a submitted batch job."""

    async def submit_request(
        self,
        model: str,
        provider: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
        thinking_budget: Optional[int] = None,
        thinking_level: Optional[str] = None,
        safety_settings: Optional[dict[str, str]] = None,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        custom_id: Optional[str] = None,
        timeout: Optional[float] = None,
        cancellation_token: Optional[Any] = None,
        status_scope: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Submit a request for batch processing.
        
        The request is queued and will be submitted as part of a batch job
        when the collection window expires or max_requests is reached.
        
        Args:
            model: Model name
            provider: Provider name ("openai" or "gemini")
            messages: Chat messages
            tools: Optional tool definitions
            max_tokens: Optional max output tokens limit
            thinking_budget: Token budget for thinking (Gemini 2.5 models)
            thinking_level: Thinking intensity level (Gemini 3 models)
            safety_settings: Optional Gemini safety settings
            session_id: Optional session ID
            agent_name: Optional agent name
            custom_id: Optional custom ID for correlation
            timeout: Optional timeout in seconds (default: max_wait_hours * 3600)
            cancellation_token: Optional cancellation token
            status_scope: Optional status scope for progress reporting
            
        Returns:
            Response dict when batch completes
            
        Raises:
            asyncio.TimeoutError: If batch doesn't complete within timeout
            RuntimeError: If batch fails
            asyncio.CancelledError: If request is cancelled
        """
        # Ensure polling task is started in this event loop
        # This is critical for CLI which uses multiple asyncio.run() calls
        self._ensure_polling_started()
        
        # Validate provider early (fail fast before queuing)
        if not self.has_batch_client(provider):
            raise ValueError(
                f"No batch client registered for provider: {provider}. "
                f"Available providers: {self.batch_providers()}"
            )
        
        # Create request
        request = BatchRequest(
            request_id=str(uuid.uuid4()),
            custom_id=custom_id or str(uuid.uuid4()),
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            thinking_budget=thinking_budget,
            thinking_level=thinking_level,
            safety_settings=safety_settings,
            session_id=session_id,
            agent_name=agent_name,
            metadata={"provider": provider},
        )
        
        # Create future for this request
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._request_futures[request.request_id] = future
        
        # Store status scope for progress reporting
        if status_scope:
            self._request_status_scopes[request.request_id] = status_scope
        
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
                logger.debug(
                    f"Queue {queue_key} reached max_requests ({self._max_requests}), "
                    f"triggering immediate submission"
                )
                # Submit immediately but also ensure a collection task exists for any
                # new requests that come in during/after this submission
                asyncio.create_task(
                    self._submit_batch(queue_key, provider, model),
                    name=f"batch_immediate_{queue_key}"
                )
            
            # Ensure submission task is running for this queue.
            # ALWAYS start a new task if:
            # 1. No task exists, OR
            # 2. The existing task has completed (done() = True), OR  
            # 3. The existing task has been running for too long (> 2x collection_window)
            #    which suggests something went wrong
            existing_task = self._submission_tasks.get(queue_key)
            should_start_new_task = False
            
            if existing_task is None:
                should_start_new_task = True
                logger.debug(f"Starting collection task for {queue_key}: no existing task")
            elif existing_task.done():
                should_start_new_task = True
                # Check if the task had an exception
                try:
                    exc = existing_task.exception()
                    if exc:
                        logger.warning(f"Previous collection task for {queue_key} failed with: {exc}")
                except (asyncio.CancelledError, asyncio.InvalidStateError):
                    pass
                logger.debug(f"Starting collection task for {queue_key}: previous task done")
            
            if should_start_new_task:
                self._submission_tasks[queue_key] = asyncio.create_task(
                    self._collection_window_task(queue_key, provider, model),
                    name=f"batch_collection_{queue_key}"
                )
                logger.debug(
                    f"Started collection task for {queue_key}, "
                    f"request {request.request_id}"
                )
        
        # Wait for result with cancellation support
        # NOTE: We don't enforce a hard timeout here because the queue manager handles
        # timeouts via _poll_batch_status (local timeout + retries). The agent should
        # wait for the Future to be resolved by the queue manager, which will either:
        # 1. Set the result when batch completes
        # 2. Set an exception when all retries are exhausted
        # The caller can pass a timeout, but the default is very long to allow for retries.
        # Total worst-case time = max_wait_hours * (max_retries + 1)
        timeout = timeout or (self._max_wait_hours * (self._max_retries + 1) * 3600)
        # Use short interval (50ms) to allow status events to flow through the agent's polling loop
        # The agent polls every 100ms, so 50ms ensures we yield control frequently enough
        check_interval = 0.05
        elapsed = 0.0
        
        try:
            while elapsed < timeout:
                # Check cancellation token
                if cancellation_token and cancellation_token.is_cancelled:
                    logger.info(f"Request {request.request_id} cancelled via token")
                    # Cancel the request (and its batch job if already submitted)
                    await self.cancel_request(request.request_id)
                    raise asyncio.CancelledError("Cancelled by user")
                
                # Wait for result with short timeout
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(future), 
                        timeout=check_interval
                    )
                    return result
                except asyncio.TimeoutError:
                    elapsed += check_interval
                    continue
                    
            # Total timeout exceeded - cancel the request and its batch job
            logger.error(f"Request {request.request_id} timed out after {timeout}s")
            await self.cancel_request(request.request_id)
            raise asyncio.TimeoutError(f"Batch request timed out after {timeout}s")
            
        except asyncio.CancelledError:
            # Agent was cancelled - cancel the batch request at provider
            logger.warning(f"Request {request.request_id} was cancelled, cancelling batch job")
            await self.cancel_request(request.request_id)
            raise
    
    async def _collection_window_task(
        self,
        queue_key: str,
        provider: str,
        model: str,
    ) -> None:
        """Background task that waits for collection window then submits batch.
        
        This task will keep running as long as there are requests in the queue,
        ensuring no requests get stuck.
        """
        try:
            while self._running:
                await asyncio.sleep(self._collection_window)
                
                if not self._running:
                    logger.warning(
                        f"Collection window task for {queue_key} stopping: manager not running. "
                        f"Queue has {len(self._queues.get(queue_key, []))} pending requests."
                    )
                    return
                
                # Submit the batch
                await self._submit_batch(queue_key, provider, model)
                
                # Check if more requests came in during submission
                # If queue is empty, we can exit this task
                async with self._queue_locks[queue_key]:
                    queue_size = len(self._queues.get(queue_key, []))
                    if queue_size == 0:
                        logger.debug(f"Collection task for {queue_key} exiting: queue empty")
                        return
                    else:
                        logger.debug(
                            f"Collection task for {queue_key} continuing: "
                            f"{queue_size} requests still in queue"
                        )
                        # Continue the loop to collect more requests
                        
        except asyncio.CancelledError:
            logger.debug(f"Collection window task for {queue_key} cancelled")
            raise
        except Exception as e:
            logger.error(
                f"Collection window task for {queue_key} failed: {e}. "
                f"Queue has {len(self._queues.get(queue_key, []))} pending requests that may need recovery.",
                exc_info=True
            )
            # Don't re-raise - let the stale queue detection handle recovery
    
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
        
        logger.debug(
            f"Submitting batch for {queue_key} with {len(batch_requests)} requests"
        )
        
        job = BatchJob(
            job_id=short_id(),
            provider=provider,
            model=model,
            status=BatchStatus.PENDING,
            requests=batch_requests,
        )
        
        # Store job and map requests to job for cancellation tracking
        async with self._jobs_lock:
            self._active_jobs[job.job_id] = job
            for req in batch_requests:
                self._request_to_job[req.request_id] = job.job_id
        
        self._metrics.total_jobs += 1
        self._metrics.total_requests += len(batch_requests)
        
        # Get batch client
        client = self._client_for(provider)
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
            
            # Report that batch is being submitted to all waiting callers
            for req in batch_requests:
                await self._report_job_status(job, f"Batch submitting: {job.model}")
            
            # Create input file and submit batch
            provider_job_id = await client.submit_batch(job, self.storage_path)
            job.provider_job_id = provider_job_id
            
            # Report successful submission
            for req in batch_requests:
                await self._report_job_status(job, f"Batch submitted: {job.model}")
            
            # Track the job for selective cancellation on restart
            tracker = get_job_tracker()
            if tracker:
                await tracker.add_job(provider, provider_job_id)
            
            logger.debug(
                f"Batch job {job.job_id} submitted to {provider} as {provider_job_id}"
            )
            
        except (LLMQuotaExhaustedError, LLMRateLimitError) as e:
            # Propagate quota/rate limit errors for fallback handling
            # These should trigger fallback to sync client, not fail silently
            logger.warning(f"Batch job {job.job_id} hit rate limit: {e}")
            job.status = BatchStatus.FAILED
            job.error_message = str(e)
            await self._complete_job(job, propagate_error=e)
            raise  # Re-raise for fallback handling
        except Exception as e:
            logger.error(f"Failed to submit batch job {job.job_id}: {e}")
            job.status = BatchStatus.FAILED
            job.error_message = str(e)
            await self._complete_job(job)
        
        return job
    
    async def _check_stale_queues(self) -> None:
        """Check for queues with pending requests but no active submission task.
        
        This is a recovery mechanism for cases where the collection_window_task
        failed or was never started properly.
        """
        now = _utc_now()
        stale_threshold = self._collection_window * 2  # 2x collection window = stale
        
        for queue_key, requests in list(self._queues.items()):
            if not requests:
                continue
            
            # Check if oldest request is stale
            # Queue is FIFO (append-only), so first element is always oldest - O(1)
            oldest_request = requests[0]
            age_seconds = (now - oldest_request.created_at).total_seconds()
            
            if age_seconds < stale_threshold:
                continue  # Not stale yet
            
            # Check if submission task exists and is still running
            task = self._submission_tasks.get(queue_key)
            task_running = task is not None and not task.done()
            
            if task_running:
                # Task is running but requests are old - could be a very slow submission
                logger.debug(
                    f"Queue {queue_key} has {len(requests)} requests (oldest: {age_seconds:.0f}s) "
                    f"but submission task is still running"
                )
                continue
            
            # Stale queue detected! Submit immediately.
            # Log detailed info to help debug why this happened
            task_info = "None"
            if task is not None:
                task_info = f"done={task.done()}, cancelled={task.cancelled()}"
                if task.done():
                    try:
                        exc = task.exception()
                        if exc:
                            task_info += f", exception={type(exc).__name__}: {exc}"
                    except (asyncio.CancelledError, asyncio.InvalidStateError):
                        pass
            
            logger.warning(
                f"STALE QUEUE DETECTED: {queue_key} has {len(requests)} pending requests "
                f"(oldest: {age_seconds:.0f}s old) with no active submission task. "
                f"Task state: {task_info}. Triggering immediate submission."
            )
            
            # Parse provider:model from queue_key
            parts = queue_key.split(":", 1)
            if len(parts) != 2:
                logger.error(f"Invalid queue_key format: {queue_key}")
                continue
            
            provider, model = parts
            
            # Submit immediately - stale requests have already waited too long
            # Any new requests that arrive during/after will either:
            # 1. Start their own collection task (if this one is done)
            # 2. Be caught by next _check_stale_queues cycle (self-healing)
            self._submission_tasks[queue_key] = asyncio.create_task(
                self._submit_batch(queue_key, provider, model),
                name=f"batch_stale_recovery_{queue_key}"
            )
