"""Batch API data models.

Defines the data structures for batch processing:
- BatchRequest: Individual request waiting to be batched
- BatchJob: Submitted batch job with status tracking
- BatchResult: Result for a single request within a batch
- BatchStatus: Enum for job statuses
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def _utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class BatchStatus(str, Enum):
    """Status of a batch job."""
    PENDING = "pending"          # Collecting requests
    SUBMITTED = "submitted"      # Submitted to API, waiting for processing
    VALIDATING = "validating"    # OpenAI: validating input file
    IN_PROGRESS = "in_progress"  # Processing
    FINALIZING = "finalizing"    # OpenAI: finalizing results
    COMPLETED = "completed"      # Successfully completed
    FAILED = "failed"            # Failed with error
    EXPIRED = "expired"          # Exceeded max wait time
    CANCELLED = "cancelled"      # Manually cancelled
    CANCELLING = "cancelling"    # Cancellation in progress


@dataclass
class BatchRequest:
    """Individual request waiting to be batched.
    
    Attributes:
        request_id: Unique identifier for this request
        custom_id: User-provided ID for correlation (required by OpenAI)
        model: Model name (e.g., "gpt-4o", "gemini-2.5-flash")
        messages: Chat messages in OpenAI format
        tools: Optional tool definitions
        session_id: Optional session ID for tracking
        agent_name: Optional agent name for tracking
        created_at: Timestamp when request was created
        metadata: Additional metadata
    """
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    custom_id: str = ""  # Will be set if not provided
    model: str = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tools: Optional[List[Dict[str, Any]]] = None
    session_id: Optional[str] = None
    agent_name: Optional[str] = None
    created_at: datetime = field(default_factory=_utc_now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Response placeholder (filled when result arrives)
    response: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    
    def __post_init__(self):
        if not self.custom_id:
            self.custom_id = self.request_id


@dataclass
class BatchJob:
    """Represents a submitted batch job.
    
    Attributes:
        job_id: Internal job ID
        provider_job_id: ID returned by the provider (OpenAI batch_id, Gemini job name)
        provider: "openai" or "gemini"
        model: Model used for this batch
        status: Current job status
        requests: List of requests in this batch
        input_file_id: ID of uploaded input file (OpenAI)
        output_file_id: ID of output file when complete (OpenAI)
        error_file_id: ID of error file if any (OpenAI)
        created_at: When the job was created
        submitted_at: When the job was submitted to the API
        completed_at: When the job completed (success or failure)
        error_message: Error message if failed
        metadata: Additional metadata
    """
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    provider_job_id: Optional[str] = None
    provider: str = ""  # "openai" or "gemini"
    model: str = ""
    status: BatchStatus = BatchStatus.PENDING
    requests: List[BatchRequest] = field(default_factory=list)
    
    # OpenAI-specific
    input_file_id: Optional[str] = None
    output_file_id: Optional[str] = None
    error_file_id: Optional[str] = None
    
    # Timestamps
    created_at: datetime = field(default_factory=_utc_now)
    submitted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    # Error tracking
    error_message: Optional[str] = None
    failed_count: int = 0
    completed_count: int = 0
    
    # Retry tracking
    retry_count: int = 0
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def request_count(self) -> int:
        return len(self.requests)
    
    @property
    def is_terminal(self) -> bool:
        """Check if job is in a terminal state."""
        return self.status in (
            BatchStatus.COMPLETED,
            BatchStatus.FAILED,
            BatchStatus.EXPIRED,
            BatchStatus.CANCELLED,
        )
    
    def get_request_by_custom_id(self, custom_id: str) -> Optional[BatchRequest]:
        """Find a request by its custom_id."""
        for req in self.requests:
            if req.custom_id == custom_id:
                return req
        return None


@dataclass
class BatchResult:
    """Result for a single request within a batch.
    
    Attributes:
        custom_id: The custom_id from the original request
        request_id: Our internal request ID
        success: Whether this request succeeded
        response: The response data if successful
        error: Error information if failed
    """
    custom_id: str
    request_id: str
    success: bool = True
    response: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    
    # Usage tracking
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class BatchMetrics:
    """Metrics for batch operations.
    
    Attributes:
        total_jobs: Total batch jobs created
        completed_jobs: Successfully completed jobs
        failed_jobs: Failed jobs
        total_requests: Total requests processed
        total_tokens: Total tokens consumed
        estimated_cost_savings: Estimated savings vs sync API (50%)
    """
    total_jobs: int = 0
    completed_jobs: int = 0
    failed_jobs: int = 0
    cancelled_jobs: int = 0
    total_requests: int = 0
    completed_requests: int = 0
    failed_requests: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    
    # Processing time tracking (in seconds)
    _processing_times: List[float] = field(default_factory=list)
    
    def add_processing_time(self, seconds: float) -> None:
        """Record a job's processing time."""
        self._processing_times.append(seconds)
    
    @property
    def min_processing_time(self) -> Optional[float]:
        """Minimum processing time in seconds."""
        return min(self._processing_times) if self._processing_times else None
    
    @property
    def max_processing_time(self) -> Optional[float]:
        """Maximum processing time in seconds."""
        return max(self._processing_times) if self._processing_times else None
    
    @property
    def mean_processing_time(self) -> Optional[float]:
        """Mean processing time in seconds."""
        if not self._processing_times:
            return None
        return sum(self._processing_times) / len(self._processing_times)
    
    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens
    
    @property
    def success_rate(self) -> float:
        if self.total_jobs == 0:
            return 0.0
        return self.completed_jobs / self.total_jobs
