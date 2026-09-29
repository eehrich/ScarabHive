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
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING
import uuid

from agent_system.paths import data_path, resolve_data_path
from agent_system.utils.id import short_id
from .base import BatchProviderClient
from .models import BatchJob, BatchRequest, BatchStatus, BatchMetrics
from .job_tracker import get_job_tracker
from ..models import LLMQuotaExhaustedError, LLMRateLimitError

if TYPE_CHECKING:
    from agent_system.config.models import BatchSystemConfig, BatchProviderConfig

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
        batch_system_config: Optional["BatchSystemConfig"] = None,
        storage_path: Optional[Path] = None,
    ):
        """Initialize the batch queue manager.
        
        Args:
            batch_system_config: Global batch system configuration
            storage_path: Path for storing batch files (overrides config.storage_path)
        """
        self.batch_system_config = batch_system_config
        
        # Determine storage path
        # Resolved either way: the callers pass Path(config.storage_path), and
        # the model's own default ("data/batch_jobs") never passed the loader
        # that moves configured paths.
        if storage_path:
            self.storage_path = resolve_data_path(storage_path)
        elif batch_system_config:
            self.storage_path = resolve_data_path(batch_system_config.storage_path)
        else:
            self.storage_path = data_path("batch_jobs")
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # Per-provider configurations — every configured provider, whatever
        # it is called. This used to copy `gemini` and `openai` by name, so
        # `anthropic` (added later) never arrived here at all: the exact cost
        # of naming providers in core code.
        self._provider_configs: Dict[str, "BatchProviderConfig"] = dict(
            (batch_system_config.providers if batch_system_config else None) or {})

        # Default configuration values (can be overridden per-provider)
        # These are used for queue management and polling
        self._collection_window = 10.0  # seconds
        self._max_requests = 100
        self._poll_interval = 10.0  # seconds
        self._max_wait_hours = 24.0
        self._max_retries = 3

        # KNOWN GAP, not an oversight: these knobs are configured PER PROVIDER
        # but applied GLOBALLY — one polling loop serves all providers, so the
        # first entry's values win for everyone (anthropic's poll_interval of
        # 30s never applies while gemini is configured). Closing it means
        # per-provider timing in the polling loop, i.e. a change in production
        # batch behaviour, not a rename. Left as it always was; the config
        # comment says which entry decides.
        for provider_config in self._provider_configs.values():
            self._collection_window = provider_config.collection_window_seconds
            self._max_requests = provider_config.max_requests_per_batch
            self._poll_interval = provider_config.poll_interval_seconds
            self._max_wait_hours = provider_config.max_wait_hours
            self._max_retries = provider_config.max_retries
            break
        
        # Request queues by model
        self._queues: Dict[str, List[BatchRequest]] = defaultdict(list)
        self._queue_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        
        # Active batch jobs
        self._active_jobs: Dict[str, BatchJob] = {}
        self._jobs_lock = asyncio.Lock()
        
        # Completed job results cache (LRU with max size to prevent memory leak)
        self._completed_jobs: Dict[str, BatchJob] = {}
        self._max_completed_jobs = 100  # Keep last 100 completed jobs for debugging
        
        # Request futures (for callers waiting on results)
        self._request_futures: Dict[str, asyncio.Future] = {}
        
        # Request to job mapping (for cancellation)
        self._request_to_job: Dict[str, str] = {}  # request_id -> job_id
        
        # Status scopes for requests (for progress reporting)
        self._request_status_scopes: Dict[str, Any] = {}  # request_id -> status_scope
        
        # Background tasks
        self._submission_tasks: Dict[str, asyncio.Task] = {}
        self._polling_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Batch client callbacks (set by register_batch_client)
        self._batch_clients: Dict[str, Any] = {}  # provider -> client
        self._batch_client_factories: Dict[str, Callable[[], Any]] = {}  # provider -> builder
        
        # Metrics
        self._metrics = BatchMetrics()
        
        # Track last status per job to avoid duplicate messages
        self._last_job_status: Dict[str, str] = {}  # job_id -> last_status_message
        
        logger.info(
            f"BatchQueueManager initialized: "
            f"window={self._collection_window}s, "
            f"max_requests={self._max_requests}, "
            f"poll_interval={self._poll_interval}s"
        )
    
    async def _report_job_status(self, job: "BatchJob", message: str) -> None:
        """Report status update for all requests in a job.
        
        Only sends if message changed since last update for this job.
        
        Args:
            job: The batch job
            message: Status message
        """
        # Skip duplicate messages
        last_status = self._last_job_status.get(job.job_id)
        if last_status == message:
            return
        self._last_job_status[job.job_id] = message
        
        # Send to all requests' status scopes
        for request in (job.requests or []):
            status_scope = self._request_status_scopes.get(request.request_id)
            if status_scope:
                try:
                    await status_scope.progress(message)
                except Exception:
                    pass  # Never let status reporting break batch processing
    
    def register_batch_client(self, provider: str, client: Any) -> None:
        """Register a batch client for a batch provider name.

        Args:
            provider: Batch provider name as configured (`batch_provider:`)
            client: Batch client instance from that provider's plugin
        """
        self._batch_clients[provider] = client
        # The tracker is keyed by THIS name (add_job below), so the client
        # must not carry its own idea of what it is called — startup
        # cancellation looks its jobs up by exactly this key.
        client.provider_name = provider
        logger.info(f"Registered batch client for provider: {provider}")

    def register_batch_client_factory(self, provider: str, factory: Callable[[], Any]) -> None:
        """Register a batch provider whose client is built on first use.

        Building a backend imports its SDK (google.genai: ~1.1 s, 88 MB) and
        opens an HTTP client; done eagerly for every configured provider that
        was paid at EVERY process start -- each ``agent-cli run`` included --
        whether or not a batch request ever followed. ``factory`` returns the
        client, or None when the provider cannot come up (no API key); the
        first ``_client_for`` resolves it and registers it like
        ``register_batch_client`` would.
        """
        self._batch_client_factories[provider] = factory
        logger.info(f"Registered batch backend for provider {provider} (built on first use)")

    def has_batch_client(self, provider: str) -> bool:
        """Whether a request for ``provider`` can be served. Resolves the
        client: a submit IS first use, and a provider whose factory declines
        (no key) must be refused here, before the request is queued and sits
        out the collection window."""
        return self._client_for(provider) is not None

    def batch_providers(self) -> list[str]:
        """Every provider name that has a client or can still build one."""
        return list(dict.fromkeys([*self._batch_clients, *self._batch_client_factories]))

    def _client_for(self, provider: str) -> Any:
        """The provider's client, building it on first use; None if unknown,
        if its factory declined (no API key) or if building it failed. Either
        outcome is logged once and the factory is forgotten, so neither the
        poll loop nor the next submit repeats the attempt every time.

        Never raises: this runs inside _submit_batch, after the job is in
        _active_jobs -- an exception there is swallowed by the collection
        task and leaves the caller waiting for its full timeout."""
        client = self._batch_clients.get(provider)
        if client is not None:
            return client
        factory = self._batch_client_factories.pop(provider, None)
        if factory is None:
            return None
        try:
            client = factory()
        except Exception as e:
            logger.error("Failed to build %s batch client: %s", provider, e, exc_info=True)
            return None
        if client is None:
            logger.info("No API key found, skipping %s batch client", provider)
            return None
        client.provider_name = provider
        self._batch_clients[provider] = client
        return client

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
                    from .models import BatchJob
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
    
    def get_metrics(self) -> BatchMetrics:
        """Get current batch metrics."""
        return self._metrics
    
    def get_active_jobs(self) -> List[BatchJob]:
        """Get list of active batch jobs."""
        return list(self._active_jobs.values())
    
    def get_queue_sizes(self) -> Dict[str, int]:
        """Get current queue sizes by model."""
        return {k: len(v) for k, v in self._queues.items()}
