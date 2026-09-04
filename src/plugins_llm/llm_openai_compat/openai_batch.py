"""OpenAI Batch API Client.

Implements batch processing using OpenAI's Batch API:
1. Convert requests to JSONL format
2. Upload input file via Files API
3. Create batch job
4. Poll for completion
5. Download and parse results

Reference: https://platform.openai.com/docs/guides/batch
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

import httpx

from agent_system.llm.tls import httpx_verify

from agent_system.llm.batch.base import BatchProviderClient
from agent_system.llm.batch.models import BatchJob, BatchStatus, TERMINAL_STATUSES
from agent_system.llm.batch.job_tracker import get_job_tracker
from agent_system.llm.models import LLMRateLimitError, LLMQuotaExhaustedError
from plugins_llm.llm_common import openai_utils

logger = logging.getLogger(__name__)

# Type variable for retry function
T = TypeVar("T")

# Retry configuration for network operations
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0  # seconds
DEFAULT_MAX_DELAY = 30.0  # seconds


async def _retry_with_backoff(
    operation: Callable[[], Awaitable[T]],
    operation_name: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    retryable_exceptions: tuple = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError),
) -> T:
    """Execute an async operation with exponential backoff retry.
    
    Args:
        operation: Async callable to execute (must return a coroutine)
        operation_name: Name for logging
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries (seconds)
        retryable_exceptions: Tuple of exception types to retry on
        
    Returns:
        Result of the operation
        
    Raises:
        The last exception if all retries fail
    """
    import random
    last_exception: Optional[Exception] = None
    
    for attempt in range(max_retries + 1):
        try:
            return await operation()
        except retryable_exceptions as e:
            last_exception = e
            if attempt < max_retries:
                # Exponential backoff with jitter
                delay = min(base_delay * (2 ** attempt), max_delay)
                # Add small random jitter (±10%)
                delay = delay * (0.9 + random.random() * 0.2)
                
                logger.warning(
                    f"{operation_name} failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"{operation_name} failed after {max_retries + 1} attempts: {e}"
                )
                raise
        except httpx.HTTPStatusError as e:
            # Retry on 5xx server errors, but not on 4xx client errors
            if e.response.status_code >= 500:
                last_exception = e
                if attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    logger.warning(
                        f"{operation_name} got server error {e.response.status_code} "
                        f"(attempt {attempt + 1}/{max_retries + 1}). Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        f"{operation_name} failed after {max_retries + 1} attempts: {e}"
                    )
                    raise
            else:
                # Don't retry 4xx errors
                raise
    
    # Should not reach here, but satisfy type checker
    if last_exception:
        raise last_exception
    raise RuntimeError(f"{operation_name} failed unexpectedly")


class _DateTimeEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime objects."""
    
    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


class OpenAIBatchClient(BatchProviderClient):
    """Client for OpenAI Batch API operations.
    
    The Batch API provides:
    - 50% cost reduction compared to sync API
    - Separate, higher rate limits
    - Results within 24 hours
    - Support for /v1/chat/completions endpoint
    
    Usage:
        client = OpenAIBatchClient(api_key="sk-...")
        job = BatchJob(requests=[...])
        
        # Submit batch
        provider_job_id = await client.submit_batch(job, storage_path)
        
        # Poll status
        status = await client.get_batch_status(provider_job_id)
        
        # Get results when complete
        results = await client.get_batch_results(job)
    """
    
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
    ):
        """Initialize the OpenAI batch client.
        
        Args:
            api_key: OpenAI API key
            base_url: Base URL for OpenAI API
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        
        # Don't set Content-Type in default headers - let httpx set it per request
        # (multipart/form-data for file uploads, application/json for JSON requests)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            verify=httpx_verify(),
            headers={
                "Authorization": f"Bearer {api_key}",
            },
        )
    
    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    def _raise_for_status(
        self, response: httpx.Response, operation: str, model: str = "unknown"
    ) -> None:
        """Raise appropriate error for non-200 responses.
        
        Args:
            response: HTTP response
            operation: Description of the operation for error message
            model: Model name for rate limit errors
            
        Raises:
            LLMRateLimitError: On 429 status
            LLMQuotaExhaustedError: On 429 with quota-related message
            RuntimeError: On other errors
        """
        if response.status_code == 200:
            return
            
        if response.status_code == 429:
            error_text = response.text.lower()
            # Check for quota exhaustion vs temporary rate limit
            if "quota" in error_text or "insufficient" in error_text:
                raise LLMQuotaExhaustedError(
                    provider="openai_batch",
                    model=model,
                    message=f"{operation}: {response.text}"
                )
            # Extract retry-after if available
            retry_after = response.headers.get("retry-after")
            retry_seconds = float(retry_after) if retry_after else None
            raise LLMRateLimitError(
                provider="openai_batch",
                model=model,
                retry_after=retry_seconds,
                message=f"{operation}: {response.text}"
            )
        
        raise RuntimeError(f"{operation}: {response.status_code} {response.text}")
    
    async def submit_batch(
        self,
        job: BatchJob,
        storage_path: Path,
    ) -> str:
        """Submit a batch job to OpenAI.
        
        Args:
            job: BatchJob containing requests to submit
            storage_path: Path to store temporary JSONL files
            
        Returns:
            OpenAI batch ID
        """
        # Create JSONL input file
        input_file_path = storage_path / f"batch_input_{job.job_id}.jsonl"
        self._create_input_file(job, input_file_path)
        
        # Upload input file
        file_id = await self._upload_file(input_file_path)
        job.input_file_id = file_id
        
        logger.info(f"Uploaded batch input file: {file_id}")
        
        # Create batch
        batch_response = await self._create_batch(file_id)
        batch_id = batch_response.get("id")
        
        if not batch_id:
            raise RuntimeError(f"No batch ID in response: {batch_response}")
        
        logger.info(f"Created OpenAI batch: {batch_id}")
        
        return batch_id
    
    def _create_input_file(self, job: BatchJob, file_path: Path) -> None:
        """Create JSONL input file for batch submission.
        
        OpenAI batch format (per line):
        {
            "custom_id": "request-1",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": "gpt-4o",
                "messages": [...],
                "tools": [...],  # optional
                "max_tokens": 4096
            }
        }
        """
        requests = job.requests or []
        if not requests:
            logger.warning(f"Batch job {job.job_id} has no requests to write")
            return
            
        with open(file_path, "w", encoding="utf-8") as f:
            for request in requests:
                # Normalize messages for OpenAI API compatibility
                normalized_messages = openai_utils.normalize_messages(request.messages)
                
                body: Dict[str, Any] = {
                    "model": request.model,
                    "messages": normalized_messages,
                }
                
                # Add max_tokens if specified in request
                if request.max_tokens is not None:
                    body["max_tokens"] = request.max_tokens
                
                if request.tools:
                    body["tools"] = request.tools
                
                line = {
                    "custom_id": request.custom_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": body,
                }
                
                # Use custom encoder to handle datetime objects
                f.write(json.dumps(line, cls=_DateTimeEncoder) + "\n")
        
        logger.debug(f"Created input file with {len(requests)} requests: {file_path}")
    
    async def _upload_file(self, file_path: Path) -> str:
        """Upload a file to OpenAI Files API with retry on transient failures.
        
        Returns:
            File ID
        """
        url = f"{self.base_url}/files"
        
        async def do_upload() -> str:
            # Use multipart form upload
            # Re-open file on each attempt in case of retry
            with open(file_path, "rb") as f:
                files = {
                    "file": (file_path.name, f, "application/jsonl"),
                }
                data = {
                    "purpose": "batch",
                }
                
                # Don't override headers - let httpx set multipart/form-data automatically
                response = await self._client.post(
                    url,
                    files=files,
                    data=data,
                )
            
            self._raise_for_status(response, "File upload failed")
            
            result = response.json()
            return result.get("id")
        
        return await _retry_with_backoff(
            do_upload,
            f"File upload ({file_path.name})",
        )
    
    async def _create_batch(self, input_file_id: str) -> Dict[str, Any]:
        """Create a batch job.
        
        Args:
            input_file_id: ID of uploaded input file
            
        Returns:
            Batch creation response
        """
        url = f"{self.base_url}/batches"
        
        payload = {
            "input_file_id": input_file_id,
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        }
        
        response = await self._client.post(url, json=payload)
        
        self._raise_for_status(response, "Batch creation failed")
        
        return response.json()
    
    async def get_batch_status(self, batch_id: str) -> Dict[str, Any]:
        """Get the status of a batch job.
        
        Args:
            batch_id: OpenAI batch ID
            
        Returns:
            Status info dict with 'status' key mapping to BatchStatus
        """
        url = f"{self.base_url}/batches/{batch_id}"
        
        response = await self._client.get(url)
        
        self._raise_for_status(response, "Failed to get batch status")
        
        data = response.json()
        
        # Map OpenAI status to our BatchStatus
        openai_status = data.get("status", "unknown")
        status_mapping = {
            "validating": BatchStatus.VALIDATING.value,
            "in_progress": BatchStatus.IN_PROGRESS.value,
            "finalizing": BatchStatus.FINALIZING.value,
            "completed": BatchStatus.COMPLETED.value,
            "failed": BatchStatus.FAILED.value,
            "expired": BatchStatus.EXPIRED.value,
            "cancelled": BatchStatus.CANCELLED.value,
            "cancelling": BatchStatus.CANCELLING.value,
        }
        
        # Extract error message from various possible locations
        error_message = None
        if data.get("errors"):
            errors = data["errors"]
            if isinstance(errors, dict):
                error_message = errors.get("message") or str(errors.get("data", errors))
            elif isinstance(errors, list) and errors:
                error_message = "; ".join(str(e.get("message", e)) for e in errors if e)
            else:
                error_message = str(errors)
        
        # Log full response for debugging on failure
        if openai_status == "failed":
            logger.error(f"OpenAI batch {batch_id} failed. Full response: {data}")
        
        return {
            "status": status_mapping.get(openai_status, openai_status),
            "output_file_id": data.get("output_file_id"),
            "error_file_id": data.get("error_file_id"),
            "request_counts": data.get("request_counts", {}),
            "usage": data.get("usage", {}),  # Token usage for entire batch
            "error": error_message,
        }
    
    async def get_batch_results(self, job: BatchJob) -> List[Dict[str, Any]]:
        """Download and parse batch results.
        
        Returns individual results with per-request token usage.
        OpenAI provides both:
        - Batch-level usage in batch.usage (aggregate totals)
        - Per-request usage in each response.body.usage
        
        We extract per-request usage here so it can be attributed to
        individual agents/sessions. Batch-level totals are available
        via get_batch_status().
        
        Args:
            job: BatchJob to get results for (must have output_file_id set)
            
        Returns:
            List of result dicts with custom_id, response/error, and usage per request
        """
        # Get current status to get output_file_id
        if not job.provider_job_id:
            raise RuntimeError("Job has no provider_job_id")
        
        status_info = await self.get_batch_status(job.provider_job_id)
        output_file_id = status_info.get("output_file_id")
        job.output_file_id = output_file_id
        job.error_file_id = status_info.get("error_file_id")
        
        if not output_file_id:
            raise RuntimeError(f"No output file for batch {job.provider_job_id}")
        
        # Download output file
        content = await self._download_file(output_file_id)
        
        # Parse JSONL results
        results = []
        for line in content.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                response_body = data.get("response", {}).get("body")
                
                # Extract usage from response body if available
                usage = {}
                if response_body and isinstance(response_body, dict):
                    usage = response_body.get("usage", {})
                
                result = {
                    "custom_id": data.get("custom_id"),
                    "response": response_body,
                    "error": data.get("error"),
                    "usage": usage,  # Per-request token usage
                }
                results.append(result)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse result line: {e}")
        
        logger.info(f"Retrieved {len(results)} results from batch {job.provider_job_id}")
        
        return results
    
    async def _download_file(self, file_id: str) -> str:
        """Download a file from OpenAI Files API with retry on transient failures.
        
        Args:
            file_id: File ID to download
            
        Returns:
            File content as string
        """
        url = f"{self.base_url}/files/{file_id}/content"
        
        async def do_download() -> str:
            response = await self._client.get(url)
            self._raise_for_status(response, "File download failed")
            return response.text
        
        return await _retry_with_backoff(
            do_download,
            f"File download ({file_id[:16]}...)",
        )
    
    async def cancel_batch(self, batch_id: str) -> Dict[str, Any]:
        """Cancel a batch job.
        
        Args:
            batch_id: OpenAI batch ID
            
        Returns:
            Cancellation response
        """
        url = f"{self.base_url}/batches/{batch_id}/cancel"
        
        response = await self._client.post(url)
        
        self._raise_for_status(response, "Batch cancellation failed")
        
        return response.json()
    
    async def list_batches(
        self,
        limit: int = 20,
        after: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List batch jobs.
        
        Args:
            limit: Max number of batches to return
            after: Cursor for pagination
            
        Returns:
            List of batch info dicts
        """
        url = f"{self.base_url}/batches"
        params = {"limit": limit}
        if after:
            params["after"] = after
        
        response = await self._client.get(url, params=params)
        
        self._raise_for_status(response, "List batches failed")
        
        data = response.json()
        return data.get("data", [])
    
    def describe_listed_batch(
        self, batch_info: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """OpenAI listing shape: `id` + `status`, model in `metadata`."""
        status_str = batch_info.get("status", "")
        if status_str in ("completed", "failed", "expired", "cancelled"):
            return None  # finished — nothing to recover
        job_id = batch_info.get("id", "")
        if not job_id:
            return None
        mapping = {
            "validating": BatchStatus.VALIDATING,
            "in_progress": BatchStatus.IN_PROGRESS,
            "finalizing": BatchStatus.FINALIZING,
            "cancelling": BatchStatus.CANCELLING,
        }
        metadata = batch_info.get("metadata") or {}
        return {
            "job_id": job_id,
            "status": mapping.get(status_str, BatchStatus.PENDING),
            "model": metadata.get("model") or "unknown",
        }

    async def cancel_all_pending_batches(self) -> int:
        """Cancel only tracked batch jobs from this AgentSystem instance.
        
        Only cancels jobs that were submitted by this AgentSystem (tracked in
        job_tracker), leaving jobs from other systems untouched.
        
        Returns:
            Number of batches cancelled
        """
        tracker = get_job_tracker()
        if not tracker:
            logger.warning("No job tracker available, skipping batch cancellation")
            return 0

        # The name this client was REGISTERED under, not a literal: the
        # tracker is keyed by the configured `batch_provider`, and hardcoding
        # a name here loses every tracked job the moment the two differ
        # (renaming openai -> openai_httpx did exactly that: startup
        # cancellation found nothing and left paid jobs running).
        provider_key = self._tracker_key()
        if not provider_key:
            logger.warning(
                "%s batch client was never registered with a provider name — "
                "skipping startup cancellation instead of guessing the "
                "tracker key", type(self).__name__)
            return 0

        tracked_jobs = await tracker.get_tracked_jobs(provider_key)
        if not tracked_jobs:
            logger.debug("No tracked OpenAI batch jobs to cancel")
            return 0
        
        logger.info(f"Found {len(tracked_jobs)} tracked OpenAI batch jobs to check")
        cancelled = 0
        
        for job_id in tracked_jobs:
            try:
                # Get batch status
                batch_info = await self.get_batch_status(job_id)
                status = batch_info.get("status", "")
                
                if status not in TERMINAL_STATUSES:
                    await self.cancel_batch(job_id)
                    cancelled += 1
                    logger.info(f"Cancelled tracked batch {job_id}")
                
                # Remove from tracker (job is done or cancelled)
                await tracker.remove_job(provider_key, job_id)
                
            except Exception as e:
                logger.warning(f"Failed to cancel tracked batch {job_id}: {e}")
                # Still try to remove from tracker (job may not exist anymore)
                await tracker.remove_job(provider_key, job_id)
        
        return cancelled
