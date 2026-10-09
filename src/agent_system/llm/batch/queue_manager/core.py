"""The state every part of `BatchQueueManager` works on, and what they all use.

`BatchQueueManager` is assembled from mixins (submission, polling, outcome)
rather than collaborators because its concerns do not own separate state: the
queues, the active and completed jobs, the callers' futures and the metrics
are read and written by nearly every step, the steps call each other in both
directions (a poll completes a job, a submit cancels one, the polling loop
recovers a stalled queue), and the batch monitor and the tests read that state
off the manager itself. This base holds it -- `__init__` -- together with the
status reports to the waiting callers and the access to the batch clients,
which every part needs.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from agent_system.paths import data_path, resolve_data_path
from ..models import BatchJob, BatchRequest, BatchMetrics
from .clients import BatchClientRegistry

if TYPE_CHECKING:
    from agent_system.config.models import BatchSystemConfig, BatchProviderConfig

logger = logging.getLogger(__name__)


class _BatchQueueCore:
    """Shared state and services of `BatchQueueManager`; see the module docstring."""

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
        
        # Batch clients by provider (set by register_batch_client and
        # register_batch_client_factory)
        self._client_registry = BatchClientRegistry()
        
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
        self._client_registry.register(provider, client)

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
        self._client_registry.register_factory(provider, factory)

    def has_batch_client(self, provider: str) -> bool:
        """Whether a request for ``provider`` can be served. Resolves the
        client: a submit IS first use, and a provider whose factory declines
        (no key) must be refused here, before the request is queued and sits
        out the collection window."""
        return self._client_for(provider) is not None

    def batch_providers(self) -> list[str]:
        """Every provider name that has a client or can still build one."""
        return self._client_registry.providers()

    def _client_for(self, provider: str) -> Any:
        """The provider's client, built on first use; None if there is none
        (`BatchClientRegistry.client_for`). Never raises."""
        return self._client_registry.client_for(provider)

    if TYPE_CHECKING:
        # The parts call each other through self. Declared for the type
        # checker only; _QueueSubmission, _JobPolling and _JobOutcome implement them.
        def _ensure_polling_started(self) -> None: ...

        async def _check_stale_queues(self) -> None: ...

        async def _process_results(self, job: BatchJob, results: List[Dict[str, Any]]) -> None: ...

        async def _complete_job(
            self, job: BatchJob, propagate_error: Optional[Exception] = None) -> None: ...

        async def cancel_request(self, request_id: str) -> bool: ...
