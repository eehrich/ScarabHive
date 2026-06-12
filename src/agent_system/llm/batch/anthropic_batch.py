"""Anthropic Message Batches API Client.

Implements batch processing using Anthropic's Message Batches API:
1. Create batch with list of requests
2. Poll for completion
3. Retrieve results

Key features:
- 50% cost reduction compared to sync API
- Asynchronous processing (results within 24 hours typically)
- Support for all Claude models
- Prompt caching compatible

Reference: https://docs.anthropic.com/en/docs/build-with-claude/message-batches
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ...utils.json_utils import repair_json

from .base import BatchProviderClient
from .models import BatchJob, BatchStatus
from .job_tracker import get_job_tracker
from ..models import LLMRateLimitError, LLMQuotaExhaustedError
from .. import anthropic_utils

logger = logging.getLogger(__name__)


class _DateTimeEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime objects."""
    
    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


class AnthropicBatchClient(BatchProviderClient):
    """Client for Anthropic Message Batches API.
    
    The Message Batches API provides:
    - 50% cost reduction compared to sync API
    - Asynchronous processing
    - Results typically within 24 hours
    - Support for all Claude models
    
    Usage:
        client = AnthropicBatchClient(api_key="sk-ant-...")
        job = BatchJob(requests=[...])
        
        # Submit batch
        provider_job_id = await client.submit_batch(job, storage_path)
        
        # Poll status
        status = await client.get_batch_status(provider_job_id)
        
        # Get results when complete
        results = await client.get_batch_results(job)
    """
    
    BETA_HEADER = "message-batches-2024-09-24"
    
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        timeout: float = 60.0,
        default_model: str = "claude-sonnet-4-20250514",
        default_max_tokens: int = 8192,
    ):
        """Initialize the Anthropic batch client.
        
        Args:
            api_key: Anthropic API key
            base_url: Base URL for Anthropic API
            timeout: Request timeout in seconds
            default_model: Default model for batch requests
            default_max_tokens: Default max tokens for responses
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.default_model = default_model
        self.default_max_tokens = default_max_tokens
        
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": self.BETA_HEADER,
                "content-type": "application/json",
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
                    provider="anthropic_batch",
                    model=model,
                    message=f"{operation}: {response.text}"
                )
            # Extract retry-after if available
            retry_after = response.headers.get("retry-after")
            retry_seconds = float(retry_after) if retry_after else None
            raise LLMRateLimitError(
                provider="anthropic_batch",
                model=model,
                retry_after=retry_seconds,
                message=f"{operation}: {response.text}"
            )
        
        raise RuntimeError(f"{operation}: {response.status_code} {response.text}")
    
    def _convert_openai_messages_to_anthropic(
        self, messages: List[Dict[str, Any]]
    ) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """Convert OpenAI message format to Anthropic format.
        
        Returns:
            (system_prompt, messages_list)
        """
        system_prompt: Optional[str] = None
        anthropic_messages: List[Dict[str, Any]] = []
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            # Extract system messages
            if role == "system":
                text = content if isinstance(content, str) else str(content)
                if system_prompt:
                    system_prompt += "\n" + text
                else:
                    system_prompt = text
                continue
            
            # Map roles
            if role == "assistant":
                anthropic_role = "assistant"
            else:
                anthropic_role = "user"
            
            # Handle tool results
            if role == "tool":
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else str(content)
                }
                anthropic_messages.append({
                    "role": "user",
                    "content": [tool_result]
                })
                
                # Check for multimodal content in tool response
                if msg.get("multimodal_content"):
                    from ...utils.multimodal_tool_content import create_anthropic_multimodal_injection_from_dict
                    # Most Anthropic models support vision
                    injection = create_anthropic_multimodal_injection_from_dict(
                        msg, supports_vision=True, model_name="anthropic-batch"
                    )
                    if injection:
                        anthropic_messages.append(injection)
                
                continue
            
            # Handle assistant messages with tool_calls
            tool_calls = msg.get("tool_calls", [])
            if role == "assistant" and tool_calls:
                content_blocks: List[Dict[str, Any]] = []
                
                # Add text content if present
                if content:
                    content_blocks.append({"type": "text", "text": str(content)})
                
                # Add tool_use blocks
                for tc in tool_calls:
                    func = tc.get("function", {})
                    args_str = func.get("arguments", "{}")
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except json.JSONDecodeError:
                        repaired = repair_json(args_str)
                        args = repaired if repaired is not None and isinstance(repaired, dict) else {}
                    
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "input": args
                    })
                
                anthropic_messages.append({
                    "role": "assistant",
                    "content": content_blocks
                })
                continue
            
            # Handle multimodal content
            if isinstance(content, list):
                content_blocks = anthropic_utils.normalize_content_list(content)
                
                anthropic_messages.append({
                    "role": anthropic_role,
                    "content": content_blocks
                })
            else:
                # Simple text
                anthropic_messages.append({
                    "role": anthropic_role,
                    "content": str(content) if content else ""
                })
        
        return system_prompt, anthropic_messages

    def _convert_openai_tools_to_anthropic(
        self, tools: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Convert OpenAI tool schema to Anthropic format."""
        anthropic_tools = []
        
        for tool in tools:
            if tool.get("type") != "function":
                continue
            
            func = tool.get("function", {})
            params = func.get("parameters", {})
            
            # Clean schema
            input_schema = self._clean_schema(params)
            
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": input_schema
            })
        
        return anthropic_tools

    def _clean_schema(self, schema: Dict) -> Dict:
        """Clean JSON schema for Anthropic compatibility."""
        if not schema:
            return {"type": "object", "properties": {}}
        
        cleaned = {}
        
        for key, value in schema.items():
            # Skip unsupported fields
            if key in ("additionalProperties", "examples", "$schema", "$id"):
                continue
            
            if key == "properties" and isinstance(value, dict):
                cleaned["properties"] = {
                    k: self._clean_schema(v) for k, v in value.items()
                }
            elif key == "items" and isinstance(value, dict):
                cleaned["items"] = self._clean_schema(value)
            elif isinstance(value, dict):
                cleaned[key] = self._clean_schema(value)
            else:
                cleaned[key] = value
        
        return cleaned

    async def submit_batch(
        self,
        job: BatchJob,
        storage_path: Path,
    ) -> str:
        """Submit a batch job to Anthropic.
        
        Args:
            job: BatchJob containing requests to submit
            storage_path: Path to store temporary files (unused, kept for API compat)
            
        Returns:
            Anthropic batch ID
        """
        # Build batch requests
        batch_requests = []
        
        for request in job.requests:
            # Convert messages to Anthropic format
            system_prompt, messages = self._convert_openai_messages_to_anthropic(
                request.messages
            )
            
            # Convert tools if present
            tools = None
            if request.tools:
                tools = self._convert_openai_tools_to_anthropic(request.tools)
            
            # Build request params
            # Use max_tokens from request if specified, otherwise fall back to default
            params: Dict[str, Any] = {
                "model": request.model or self.default_model,
                "max_tokens": request.max_tokens if request.max_tokens is not None else self.default_max_tokens,
                "messages": messages,
            }
            
            if system_prompt:
                params["system"] = system_prompt
            
            if tools:
                params["tools"] = tools
            
            batch_requests.append({
                "custom_id": request.custom_id,
                "params": params
            })
        
        # Submit batch
        url = f"{self.base_url}/messages/batches"
        payload = {"requests": batch_requests}
        
        response = await self._client.post(url, json=payload)
        
        self._raise_for_status(response, "Batch creation failed")
        
        data = response.json()
        batch_id = data.get("id")
        
        if not batch_id:
            raise RuntimeError(f"No batch ID in response: {data}")
        
        logger.info(f"Created Anthropic batch: {batch_id} with {len(batch_requests)} requests")

        # NOTE: job tracking is done by BatchQueueManager._submit_batch, which
        # calls tracker.add_job(provider, provider_job_id) right after this
        # method returns. The previous in-client call passed 3 args to a 2-arg
        # add_job(provider, job_id) signature, raising TypeError on every
        # Anthropic batch submission (after the batch was already created at the
        # provider). Removed - it was both buggy and redundant double-tracking.

        return batch_id
    
    async def get_batch_status(self, batch_id: str) -> Dict[str, Any]:
        """Get the status of a batch job.
        
        Args:
            batch_id: Anthropic batch ID
            
        Returns:
            Status info dict with 'status' key mapping to BatchStatus
        """
        url = f"{self.base_url}/messages/batches/{batch_id}"
        
        response = await self._client.get(url)
        
        self._raise_for_status(response, "Failed to get batch status")
        
        data = response.json()
        
        # Map Anthropic status to our BatchStatus
        anthropic_status = data.get("processing_status", "unknown")
        status_mapping = {
            "in_progress": BatchStatus.IN_PROGRESS.value,
            "ended": BatchStatus.COMPLETED.value,
            "canceling": BatchStatus.CANCELLING.value,
        }
        
        # Ended can mean completed, failed, or cancelled based on end status
        if anthropic_status == "ended":
            end_status = data.get("end_status", "")
            if end_status == "succeeded":
                status = BatchStatus.COMPLETED.value
            elif end_status == "failed":
                status = BatchStatus.FAILED.value
            elif end_status == "canceled":
                status = BatchStatus.CANCELLED.value
            elif end_status == "expired":
                status = BatchStatus.EXPIRED.value
            else:
                status = BatchStatus.COMPLETED.value  # Default to completed
        else:
            status = status_mapping.get(anthropic_status, anthropic_status)
        
        # Extract request counts
        request_counts = data.get("request_counts", {})
        
        return {
            "status": status,
            "request_counts": {
                "total": request_counts.get("total", 0),
                "succeeded": request_counts.get("succeeded", 0),
                "errored": request_counts.get("errored", 0),
                "canceled": request_counts.get("canceled", 0),
                "expired": request_counts.get("expired", 0),
                "processing": request_counts.get("processing", 0),
            },
            "created_at": data.get("created_at"),
            "ended_at": data.get("ended_at"),
            "expires_at": data.get("expires_at"),
            "results_url": data.get("results_url"),
        }
    
    async def get_batch_results(self, job: BatchJob) -> List[Dict[str, Any]]:
        """Get results for a completed batch job.
        
        Args:
            job: BatchJob to get results for
            
        Returns:
            List of result dicts with custom_id, response/error, and usage
        """
        if not job.provider_job_id:
            raise RuntimeError("Job has no provider_job_id")
        
        # Get status to check results URL
        status_info = await self.get_batch_status(job.provider_job_id)
        results_url = status_info.get("results_url")
        
        if not results_url:
            # Stream results from the API
            return await self._stream_results(job.provider_job_id)
        
        # Download from results URL
        response = await self._client.get(results_url)
        self._raise_for_status(response, "Failed to download results")
        
        return self._parse_results(response.text)
    
    async def _stream_results(self, batch_id: str) -> List[Dict[str, Any]]:
        """Stream results from the batch API.
        
        Args:
            batch_id: Batch ID to get results for
            
        Returns:
            List of parsed results
        """
        url = f"{self.base_url}/messages/batches/{batch_id}/results"
        
        response = await self._client.get(url)
        self._raise_for_status(response, "Failed to get batch results")
        
        return self._parse_results(response.text)
    
    def _parse_results(self, content: str) -> List[Dict[str, Any]]:
        """Parse JSONL batch results.
        
        Args:
            content: JSONL content string
            
        Returns:
            List of result dicts
        """
        results = []
        
        for line in content.strip().split("\n"):
            if not line:
                continue
            
            try:
                data = json.loads(line)
                custom_id = data.get("custom_id")
                result_data = data.get("result", {})
                result_type = result_data.get("type", "")
                
                if result_type == "succeeded":
                    message = result_data.get("message", {})
                    
                    # Extract content and tool use
                    content_parts = message.get("content", [])
                    text_content = ""
                    tool_calls = []
                    
                    for part in content_parts:
                        part_type = part.get("type", "")
                        if part_type == "text":
                            text_content += part.get("text", "")
                        elif part_type == "tool_use":
                            tool_calls.append({
                                "id": part.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": part.get("name", ""),
                                    "arguments": json.dumps(part.get("input", {}))
                                }
                            })
                    
                    # Build OpenAI-compatible response
                    response_body = {
                        "choices": [{
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": text_content,
                            },
                            "finish_reason": message.get("stop_reason", "stop")
                        }],
                        "usage": {
                            "prompt_tokens": message.get("usage", {}).get("input_tokens", 0),
                            "completion_tokens": message.get("usage", {}).get("output_tokens", 0),
                            "total_tokens": (
                                message.get("usage", {}).get("input_tokens", 0) +
                                message.get("usage", {}).get("output_tokens", 0)
                            )
                        }
                    }
                    
                    if tool_calls:
                        response_body["choices"][0]["message"]["tool_calls"] = tool_calls
                    
                    results.append({
                        "custom_id": custom_id,
                        "response": response_body,
                        "error": None,
                        "usage": response_body["usage"]
                    })
                    
                elif result_type == "errored":
                    error = result_data.get("error", {})
                    results.append({
                        "custom_id": custom_id,
                        "response": None,
                        "error": error.get("message", str(error)),
                        "usage": {}
                    })
                    
                elif result_type in ("canceled", "expired"):
                    results.append({
                        "custom_id": custom_id,
                        "response": None,
                        "error": f"Request {result_type}",
                        "usage": {}
                    })
                    
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse result line: {e}")
        
        logger.info(f"Retrieved {len(results)} results from batch")
        
        return results
    
    async def cancel_batch(self, batch_id: str) -> bool:
        """Cancel a batch job.
        
        Args:
            batch_id: Anthropic batch ID
            
        Returns:
            True if cancellation was initiated
        """
        url = f"{self.base_url}/messages/batches/{batch_id}/cancel"
        
        response = await self._client.post(url)
        
        self._raise_for_status(response, "Batch cancellation failed")
        
        logger.info(f"Initiated cancellation for batch {batch_id}")
        return True
    
    async def list_batches(
        self,
        limit: int = 20,
        before_id: Optional[str] = None,
        after_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List batch jobs.
        
        Args:
            limit: Max number of batches to return
            before_id: Return batches before this ID
            after_id: Return batches after this ID
            
        Returns:
            List of batch info dicts
        """
        url = f"{self.base_url}/messages/batches"
        params: Dict[str, Any] = {"limit": limit}
        if before_id:
            params["before_id"] = before_id
        if after_id:
            params["after_id"] = after_id
        
        response = await self._client.get(url, params=params)
        
        self._raise_for_status(response, "List batches failed")
        
        data = response.json()
        return data.get("data", [])
    
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
        
        tracked_jobs = await tracker.get_tracked_jobs("anthropic")
        if not tracked_jobs:
            logger.debug("No tracked Anthropic batch jobs to cancel")
            return 0
        
        logger.info(f"Found {len(tracked_jobs)} tracked Anthropic batch jobs to check")
        cancelled = 0
        
        for job_id in tracked_jobs:
            try:
                # Get batch status
                status_info = await self.get_batch_status(job_id)
                status = status_info.get("status", "")
                
                if status in (BatchStatus.IN_PROGRESS.value,):
                    await self.cancel_batch(job_id)
                    cancelled += 1
                    logger.info(f"Cancelled tracked batch {job_id}")
                
                # Remove from tracker (job is done or cancelled)
                await tracker.remove_job("anthropic", job_id)
                
            except Exception as e:
                logger.warning(f"Failed to cancel tracked batch {job_id}: {e}")
                # Still try to remove from tracker (job may not exist anymore)
                await tracker.remove_job("anthropic", job_id)
        
        return cancelled
