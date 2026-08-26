"""Gemini Batch API Client.

Implements batch processing using Google's Gemini Batch API via SDK:
1. Convert requests to Gemini batch format
2. Submit batch job
3. Poll for completion
4. Retrieve results

Reference: https://ai.google.dev/gemini-api/docs/batch
         https://developers.googleblog.com/scale-your-ai-workloads-batch-mode-gemini-api/
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_system.utils.json_utils import repair_json

try:
    from google import genai
    from google.genai import types
    HAS_GENAI_SDK = True
except ImportError:
    HAS_GENAI_SDK = False
    genai = None  # type: ignore
    types = None  # type: ignore

from agent_system.llm.batch.base import BatchProviderClient
from agent_system.llm.batch.models import BatchJob, BatchStatus, TERMINAL_STATUSES
from agent_system.llm.batch.job_tracker import get_job_tracker
from agent_system.llm.models import LLMRateLimitError, LLMQuotaExhaustedError
from agent_system.llm.retry_utils import is_rate_limit_error, parse_retry_delay
from plugins_llm.llm_common.schema_sanitize import sanitize_schema_for_gemini

from .gemini_utils import (
    build_thinking_config,
    extract_available_tool_names,
    filter_unavailable_tool_calls_dict,
)
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

# Type variable for retry function
T = TypeVar("T")

# Retry configuration for network operations
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0  # seconds
DEFAULT_MAX_DELAY = 30.0  # seconds



async def _retry_async_operation(
    operation: Callable[[], T],
    operation_name: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> T:
    """Execute an async operation (typically asyncio.to_thread) with exponential backoff retry.
    
    Retries on network-related exceptions that are typically transient.
    
    Args:
        operation: Async callable to execute
        operation_name: Name for logging
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries (seconds)
        
    Returns:
        Result of the operation
        
    Raises:
        The last exception if all retries fail
    """
    import random
    
    # Exception types that indicate transient network issues
    # These can come from the underlying SDK/HTTP client
    transient_error_indicators = [
        "connection",
        "timeout",
        "timed out",
        "network",
        "temporarily unavailable",
        "service unavailable",
        "503",
        "502",
        "504",
        "reset by peer",
        "broken pipe",
        "ssl",
    ]
    
    def is_transient_error(e: Exception) -> bool:
        """Check if exception looks like a transient network error."""
        error_str = str(e).lower()
        error_type = type(e).__name__.lower()
        return any(
            indicator in error_str or indicator in error_type
            for indicator in transient_error_indicators
        )
    
    last_exception: Optional[Exception] = None
    
    for attempt in range(max_retries + 1):
        try:
            return await operation()
        except Exception as e:
            # Check if this is a rate limit error - don't retry those here,
            # let the caller handle them with proper rate limit logic
            if is_rate_limit_error(e):
                raise
            
            # Check if this looks like a transient error
            if not is_transient_error(e):
                raise
            
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
    
    # Should not reach here, but satisfy type checker
    if last_exception:
        raise last_exception
    raise RuntimeError(f"{operation_name} failed unexpectedly")


class GeminiBatchClient(BatchProviderClient):
    """Client for Gemini Batch API operations using Google GenAI SDK.
    
    The Gemini Batch API provides:
    - 50% cost reduction compared to sync API
    - Higher rate limits
    - Results within 24 hours
    - Support for generateContent endpoint
    
    Usage:
        client = GeminiBatchClient(api_key="...")
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
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: float = 60.0,
    ):
        """Initialize the Gemini batch client.
        
        Args:
            api_key: Gemini API key
            base_url: Base URL for Gemini API (not used in SDK mode)
            timeout: Request timeout in seconds (not used in SDK mode)
            
        Raises:
            ImportError: If google-genai SDK is not installed
        """
        if not HAS_GENAI_SDK:
            raise ImportError(
                "Google GenAI SDK is required for Gemini batch operations. "
                "Install it with: pip install google-genai"
            )
        
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        
        # Initialize SDK client
        self._sdk_client = genai.Client(api_key=api_key)
        logger.info("GeminiBatchClient initialized with SDK")
    
    async def close(self) -> None:
        """Close the client (no-op for SDK)."""
        pass
    
    async def submit_batch(
        self,
        job: BatchJob,
        storage_path: Path,
    ) -> str:
        """Submit a batch job to Gemini.
        
        Args:
            job: BatchJob containing requests to submit
            storage_path: Path to store temporary files (not used with SDK inline requests)
            
        Returns:
            Gemini batch job name/ID
        """
        # Convert requests to SDK inline request format
        # Each request is a dict with 'contents' key and optional 'config'
        inline_requests = []
        for req in job.requests:
            # Filter unavailable tool calls from history before conversion
            # This prevents UNEXPECTED_TOOL_CALL when session has tool calls from other agents
            available_tool_names = extract_available_tool_names(req.tools) if req.tools else set()
            filtered_messages = filter_unavailable_tool_calls_dict(req.messages, available_tool_names)
            
            # Extract system instruction from messages
            system_instruction = self._extract_system_instruction(filtered_messages)
            contents = self._convert_messages_to_contents(filtered_messages)
            
            request_dict: Dict[str, Any] = {
                'contents': contents,
            }
            
            # Build config with system_instruction, tools, and generation parameters
            config: Dict[str, Any] = {}
            
            if system_instruction:
                config['system_instruction'] = system_instruction
            
            # Add max_output_tokens if specified in request
            if req.max_tokens is not None:
                config['max_output_tokens'] = req.max_tokens
            
            # Add tools if present - use SDK types.Tool objects
            if req.tools:
                sdk_tools = self._convert_tools_to_sdk(req.tools)
                if sdk_tools:
                    config['tools'] = sdk_tools
            
            # Add thinking config if specified (Gemini 2.5: thinking_budget, Gemini 3: thinking_level)
            # Note: include_thoughts is always False for batch - thoughts aren't streamed
            # and would just waste tokens in the response
            thinking_config = build_thinking_config(
                include_thoughts=False,  # Always False for batch
                thinking_budget=req.thinking_budget,
                thinking_level=req.thinking_level,
            )
            if thinking_config:
                # Convert HTTP API format to SDK format
                sdk_thinking_kwargs = {}
                if "includeThoughts" in thinking_config:
                    sdk_thinking_kwargs["include_thoughts"] = thinking_config["includeThoughts"]
                if "thinkingBudget" in thinking_config:
                    sdk_thinking_kwargs["thinking_budget"] = thinking_config["thinkingBudget"]
                if "thinkingLevel" in thinking_config:
                    # SDK expects lowercase values ("low", "medium", "high", "minimal")
                    sdk_thinking_kwargs["thinking_level"] = thinking_config["thinkingLevel"]
                config['thinking_config'] = types.ThinkingConfig(**sdk_thinking_kwargs)
            
            # Add safety settings if specified
            if req.safety_settings:
                config['safety_settings'] = [
                    types.SafetySetting(
                        category=category,
                        threshold=threshold,
                    )
                    for category, threshold in req.safety_settings.items()
                ]
            
            if config:
                request_dict['config'] = config
            
            inline_requests.append(request_dict)
        
        # Submit batch using SDK with inline requests
        try:
            # Use full model path for SDK
            model_name = job.model
            
            # DEBUG: Print traceback if model is not a string
            if not isinstance(model_name, str):
                import traceback
                logger.error(
                    "DEBUG: job.model is not a string!\n"
                    "  type(job.model)=%s\n"
                    "  job.model=%r\n"
                    "  job.job_id=%s\n"
                    "  job.requests[0].model=%r (if exists)\n"
                    "  Traceback:\n%s",
                    type(model_name).__name__, model_name, job.job_id,
                    job.requests[0].model if job.requests else "NO REQUESTS",
                    ''.join(traceback.format_stack())
                )
            
            if not model_name.startswith("models/"):
                model_name = f"models/{model_name}"
            
            logger.debug("Submitting batch via SDK: model=%s, requests=%d", 
                        model_name, len(inline_requests))
            
            # Run sync SDK call in thread pool with retry on transient failures
            # Use functools.partial since to_thread doesn't pass kwargs
            create_fn = functools.partial(
                self._sdk_client.batches.create,
                model=model_name,
                src=inline_requests,
                config={'display_name': f"batch_{job.job_id}"},
            )
            
            async def do_create_batch():
                return await asyncio.to_thread(create_fn)
            
            batch_job = await _retry_async_operation(
                do_create_batch,
                f"Gemini submit_batch ({job.job_id})",
            )
            logger.info("Created Gemini batch job: %s", batch_job.name)
            return batch_job.name
        except Exception as e:
            logger.error("SDK batch submission failed: %s: %s", type(e).__name__, e)
            # Check if this is a rate limit error and convert to our exception type
            if is_rate_limit_error(e):
                error_str = str(e).lower()
                retry_delay = parse_retry_delay(str(e))
                # Check for quota exhaustion - includes RESOURCE_EXHAUSTED from Gemini API
                if "quota" in error_str or "insufficient" in error_str or "resource_exhausted" in error_str:
                    raise LLMQuotaExhaustedError(
                        provider="gemini_batch",
                        model=job.model,
                        message=f"Batch submission failed: {e}"
                    ) from e
                raise LLMRateLimitError(
                    provider="gemini_batch",
                    model=job.model,
                    retry_after=retry_delay,
                    message=f"Batch submission failed: {e}"
                ) from e
            raise
    
    def _extract_system_instruction(
        self,
        messages: List[Dict[str, Any]],
    ) -> Optional[str]:
        """Extract and merge system messages into a single system instruction.
        
        Args:
            messages: List of OpenAI-style messages
            
        Returns:
            Merged system instruction string, or None if no system messages
        """
        system_parts: List[str] = []
        
        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    system_parts.append(content.strip())
                elif isinstance(content, list):
                    # Handle multimodal content (extract text parts)
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text = part.get("text", "")
                            if text.strip():
                                system_parts.append(text.strip())
        
        if system_parts:
            merged = "\n\n".join(system_parts)
            logger.debug(f"Extracted system instruction: {len(merged)} chars from {len(system_parts)} parts")
            return merged
        
        return None

    def _convert_messages_to_contents(
        self,
        messages: List[Dict[str, Any]],
    ) -> List[types.Content]:
        """Convert OpenAI-style messages to Gemini SDK Content format.
        
        Uses SDK types.Content and types.Part to properly support thought_signature
        for Gemini 3 Pro function calls.
        
        Handles:
        - user messages → role: "user"
        - assistant messages → role: "model"
        - assistant with tool_calls → role: "model" with functionCall parts (with thought_signature)
        - tool responses → role: "tool" with functionResponse parts (Gemini 2.0+ format)
        - system messages → skipped (handled separately via system_instruction)
        """
        contents: List[types.Content] = []
        
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            
            if role == "system":
                # Gemini handles system messages differently
                # Should be passed as system_instruction, not in contents
                continue
            
            # Handle tool responses (function results)
            if role == "tool":
                tool_name = msg.get("name") or msg.get("tool_call_id") or "unknown"
                
                # Try to parse content as JSON
                if isinstance(content, str):
                    try:
                        result_data = json.loads(content)
                    except json.JSONDecodeError:
                        result_data = {"result": content}
                else:
                    result_data = content if content else {}
                
                # Build parts starting with function response
                tool_parts: List[types.Part] = [
                    types.Part.from_function_response(
                        name=tool_name,
                        response=result_data
                    )
                ]
                
                # Add multimodal content as inline parts (Gemini native support)
                if msg.get("multimodal_content"):
                    from agent_system.utils.multimodal_tool_content import create_gemini_multimodal_parts_from_dict
                    encoded_items = create_gemini_multimodal_parts_from_dict(msg)
                    if encoded_items:
                        for item in encoded_items:
                            # Decode base64 to bytes for Blob
                            data_bytes = base64.b64decode(item.data)
                            tool_parts.append(types.Part(
                                inline_data=types.Blob(
                                    mime_type=item.mime_type,
                                    data=data_bytes
                                )
                            ))
                
                contents.append(types.Content(
                    role="tool",  # Gemini 2.0+ uses "tool" role
                    parts=tool_parts
                ))
                continue
            
            # Handle assistant with tool_calls (function call requests)
            if role == "assistant" and msg.get("tool_calls"):
                parts: List[types.Part] = []
                
                # Add text content if present
                if content:
                    parts.append(types.Part(text=content))
                
                # Add function calls with thought_signature preservation
                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    func_name = func.get("name", "")
                    func_args = func.get("arguments", "{}")
                    
                    # Parse args if string
                    if isinstance(func_args, str):
                        try:
                            func_args = json.loads(func_args)
                        except json.JSONDecodeError:
                            repaired = repair_json(func_args)
                            func_args = repaired if repaired is not None and isinstance(repaired, dict) else {}
                    
                    # Get thought_signature - required for Gemini 3 Pro
                    # The signature is stored in extra_content.google.thought_signature
                    thought_sig = None
                    extra_content = tc.get("extra_content")
                    if extra_content:
                        google_extra = extra_content.get("google")
                        if google_extra:
                            thought_sig = google_extra.get("thought_signature")
                    
                    # Also check direct thought_signature field
                    if not thought_sig:
                        thought_sig = tc.get("thought_signature")
                    
                    # Convert thought_signature to bytes if it's a base64 string
                    # (happens when messages were JSON-serialized via gemini_sdk_client)
                    if thought_sig and isinstance(thought_sig, str):
                        try:
                            thought_sig = base64.b64decode(thought_sig)
                        except Exception:
                            # If decoding fails, try encoding as UTF-8 bytes
                            thought_sig = thought_sig.encode('utf-8')
                    
                    # CRITICAL: For Gemini 3 Pro, all function calls in current turn MUST have
                    # a thought_signature. If we don't have one (e.g., from a different model,
                    # or from older sessions), use Google's documented bypass token to skip validation.
                    # See: https://ai.google.dev/gemini-api/docs/thought-signatures#faqs
                    if not thought_sig:
                        thought_sig = b"skip_thought_signature_validator"
                    
                    # Create SDK Part with function_call and thought_signature
                    parts.append(types.Part(
                        function_call=types.FunctionCall(
                            name=func_name,
                            args=func_args
                        ),
                        thought_signature=thought_sig
                    ))
                
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                continue
            
            # Standard user/assistant messages
            gemini_role = "user" if role == "user" else "model"
            
            if isinstance(content, str):
                contents.append(types.Content(
                    role=gemini_role,
                    parts=[types.Part(text=content)]
                ))
            elif isinstance(content, list):
                # Multimodal content
                parts = []
                for item in content:
                    if item.get("type") == "text":
                        parts.append(types.Part(text=item.get("text", "")))
                    elif item.get("type") in ("image", "image_url"):
                        # Handle image content
                        image_url = item.get("image_url", {})
                        if isinstance(image_url, dict):
                            url = image_url.get("url", "")
                        else:
                            url = image_url
                        
                        # DEBUG: Print full traceback if url is not a string
                        if not isinstance(url, str):
                            import traceback
                            logger.error(
                                "DEBUG: url is not a string!\n"
                                "  type(url)=%s\n"
                                "  url=%r\n"
                                "  item=%r\n"
                                "  image_url=%r\n"
                                "  Traceback:\n%s",
                                type(url).__name__, url, item, image_url,
                                ''.join(traceback.format_stack())
                            )
                        
                        if url.startswith("data:"):
                            # Parse data URL
                            try:
                                header, data = url.split(",", 1)
                                mime_type = header.split(":")[1].split(";")[0]
                                data_bytes = base64.b64decode(data)
                                parts.append(types.Part(
                                    inline_data=types.Blob(
                                        mime_type=mime_type,
                                        data=data_bytes
                                    )
                                ))
                            except (ValueError, IndexError):
                                pass
                
                if parts:
                    contents.append(types.Content(role=gemini_role, parts=parts))
        
        return contents
    
    def _convert_tools_to_sdk(
        self,
        tools: List[Dict[str, Any]],
    ) -> List[types.Tool]:
        """Convert OpenAI tools to SDK format.
        
        Creates proper types.Tool objects with FunctionDeclaration.
        Uses shared sanitize_schema_for_gemini() to clean schemas.
        """
        function_declarations = []
        
        for tool in tools:
            if tool.get("type") != "function":
                continue
            
            func = tool.get("function", {})
            params = func.get("parameters", {})
            
            # Sanitize schema using shared function
            sanitized_params = sanitize_schema_for_gemini(params) if params else None
            
            # Build function declaration with all parameters at once
            declaration = types.FunctionDeclaration(
                name=func.get("name", ""),
                description=func.get("description", ""),
                parameters=sanitized_params,  # type: ignore[arg-type]
            )
            
            function_declarations.append(declaration)
        
        if function_declarations:
            return [types.Tool(function_declarations=function_declarations)]
        return []
    
    async def get_batch_status(self, job_name: str) -> Dict[str, Any]:
        """Get the status of a batch job.
        
        Args:
            job_name: Gemini batch job name
            
        Returns:
            Status info dict with 'status' key mapping to BatchStatus
        """
        try:
            # Run sync SDK call in thread pool to avoid blocking event loop
            get_fn = functools.partial(self._sdk_client.batches.get, name=job_name)
            job = await asyncio.to_thread(get_fn)
            
            # Map Gemini status to our BatchStatus
            # The state is an enum, access .name for string comparison
            state_name = job.state.name if hasattr(job.state, 'name') else str(job.state)
            logger.debug(f"Gemini batch status for {job_name}: {state_name}")
            
            status_mapping = {
                "STATE_UNSPECIFIED": BatchStatus.SUBMITTED.value,
                "JOB_STATE_PENDING": BatchStatus.SUBMITTED.value,  # Waiting at provider
                "JOB_STATE_RUNNING": BatchStatus.IN_PROGRESS.value,
                "JOB_STATE_SUCCEEDED": BatchStatus.COMPLETED.value,
                "JOB_STATE_FAILED": BatchStatus.FAILED.value,
                "JOB_STATE_CANCELLED": BatchStatus.CANCELLED.value,
                "JOB_STATE_EXPIRED": BatchStatus.FAILED.value,
            }
            
            mapped_status = status_mapping.get(state_name, BatchStatus.IN_PROGRESS.value)
            logger.debug(f"Gemini batch {job_name}: state={state_name} -> status={mapped_status}")
            
            return {
                "status": mapped_status,
                "error": str(job.error) if hasattr(job, "error") and job.error else None,
            }
        except Exception as e:
            # Don't mark as failed for transient errors - keep polling
            error_msg = str(e)
            logger.error(f"Failed to get batch status (will retry): {e}")
            
            # Check if this is a transient network error
            transient_errors = ["getaddrinfo", "timeout", "connection", "ConnectionError"]
            is_transient = any(err.lower() in error_msg.lower() for err in transient_errors)
            
            if is_transient:
                # Keep status as in_progress for transient errors
                return {"status": BatchStatus.IN_PROGRESS.value, "error": None}
            else:
                # For other errors, mark as failed
                return {"status": BatchStatus.FAILED.value, "error": error_msg}
    
    async def get_batch_results(self, job: BatchJob) -> List[Dict[str, Any]]:
        """Get batch results with retry on transient failures.
        
        Returns individual results with per-request token usage in OpenAI-compatible format.
        
        Args:
            job: BatchJob to get results for
            
        Returns:
            List of result dicts with custom_id, response/error, and usage per request.
            Usage fields are normalized to OpenAI format:
            - prompt_tokens (Gemini: prompt_token_count)
            - completion_tokens (Gemini: candidates_token_count)
            - total_tokens (Gemini: total_token_count)
        """
        try:
            # Run sync SDK call in thread pool with retry on transient failures
            get_fn = functools.partial(
                self._sdk_client.batches.get, name=job.provider_job_id
            )
            
            async def do_get_results():
                return await asyncio.to_thread(get_fn)
            
            batch_job = await _retry_async_operation(
                do_get_results,
                f"Gemini get_batch_results ({job.provider_job_id[:20]}...)",
            )
            
            results = []
            
            # Check for inline responses
            if hasattr(batch_job, 'dest') and batch_job.dest:
                # Try inline responses first
                responses = None
                if hasattr(batch_job.dest, 'inlined_responses') and batch_job.dest.inlined_responses:
                    responses = batch_job.dest.inlined_responses
                
                # Only process if responses exist and is iterable
                if responses is not None and len(responses) > 0:
                    # Safely access job.requests
                    job_requests = job.requests or []
                    
                    for i, inline_response in enumerate(responses):
                        if i < len(job_requests):
                            custom_id = job_requests[i].custom_id
                        else:
                            custom_id = f"request_{i}"
                        
                        # Check for error in response
                        if hasattr(inline_response, "error") and inline_response.error:
                            results.append({
                                "custom_id": custom_id,
                                "response": None,
                                "error": {"message": str(inline_response.error)},
                            })
                        elif hasattr(inline_response, "response") and inline_response.response:
                            # Extract content from response - check parts directly to avoid
                            # warning when function_call parts are present
                            response_obj = inline_response.response
                            content = ""
                            tool_calls = []
                            
                            if hasattr(response_obj, "candidates") and response_obj.candidates:
                                candidate = response_obj.candidates[0]
                                if hasattr(candidate, "content") and candidate.content and hasattr(candidate.content, "parts") and candidate.content.parts:
                                    for part in candidate.content.parts:
                                        if hasattr(part, "function_call") and part.function_call:
                                            # Extract function call
                                            fc = part.function_call
                                            tool_calls.append({
                                                "id": f"call_{fc.name}_{len(tool_calls)}",
                                                "type": "function",
                                                "function": {
                                                    "name": fc.name,
                                                    "arguments": json.dumps(dict(fc.args)) if fc.args else "{}",
                                                }
                                            })
                                        elif hasattr(part, "text") and part.text:
                                            content += part.text
                            
                            # Build message
                            message: Dict[str, Any] = {
                                "role": "assistant",
                                "content": content if content else None,
                            }
                            if tool_calls:
                                message["tool_calls"] = tool_calls
                            
                            # Extract usage metadata in OpenAI-compatible format
                            usage = {}
                            if hasattr(response_obj, "usage_metadata"):
                                um = response_obj.usage_metadata
                                
                                # Debug: Log the full usage_metadata structure
                                logger.debug(f"SDK usage_metadata: {um}")
                                
                                prompt_tokens = getattr(um, "prompt_token_count", 0) or 0
                                
                                # SDK has both response_token_count and candidates_token_count
                                # Try response_token_count first (newer API), fallback to candidates_token_count
                                completion_tokens = getattr(um, "response_token_count", None) or getattr(um, "candidates_token_count", 0) or 0
                                
                                # cached_content_token_count can be None when no caching is used
                                cached_tokens = getattr(um, "cached_content_token_count", None)
                                cached_tokens = cached_tokens if cached_tokens is not None else 0
                                
                                logger.debug(f"Extracted tokens - prompt: {prompt_tokens}, completion: {completion_tokens}, cached: {cached_tokens}")
                                
                                usage = {
                                    "prompt_tokens": prompt_tokens,
                                    "completion_tokens": completion_tokens,
                                    "total_tokens": getattr(um, "total_token_count", 0) or 0,
                                }
                                
                                # Add cached tokens if present
                                if cached_tokens and cached_tokens > 0:
                                    usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
                                    logger.debug(f"Added cached tokens to usage: {cached_tokens}")
                            
                            results.append({
                                "custom_id": custom_id,
                                "response": {
                                    "choices": [{
                                        "message": message,
                                    }],
                                    "usage": usage,  # Per-request token usage
                                },
                                "error": None,
                            })
                        else:
                            results.append({
                                "custom_id": custom_id,
                                "response": None,
                                "error": {"message": "No response or error in batch result"},
                            })
            
            return results if results else [{
                "custom_id": req.custom_id,
                "response": None,
                "error": {"message": "No results found in batch job"},
            } for req in (job.requests or [])]
            
        except Exception as e:
            logger.error(f"Failed to get batch results: {e}")
            # Return error for all requests
            requests = job.requests or []
            if not requests:
                # No requests to report errors for - return single generic error
                return [{
                    "custom_id": "unknown",
                    "response": None,
                    "error": {"message": str(e)},
                }]
            return [{
                "custom_id": req.custom_id,
                "response": None,
                "error": {"message": str(e)},
            } for req in requests]
    
    async def cancel_batch(self, job_name: str) -> Dict[str, Any]:
        """Cancel a batch job.
        
        Args:
            job_name: Gemini batch job name
            
        Returns:
            Cancellation response
        """
        try:
            # Run sync SDK call in thread pool to avoid blocking event loop
            cancel_fn = functools.partial(self._sdk_client.batches.cancel, name=job_name)
            await asyncio.to_thread(cancel_fn)
            return {"status": "cancelled"}
        except Exception as e:
            if is_rate_limit_error(e):
                error_str = str(e).lower()
                retry_delay = parse_retry_delay(str(e))
                # Check for quota exhaustion - includes RESOURCE_EXHAUSTED from Gemini API
                if "quota" in error_str or "insufficient" in error_str or "resource_exhausted" in error_str:
                    raise LLMQuotaExhaustedError(
                        provider="gemini_batch",
                        model="unknown",
                        message=f"Batch cancellation failed: {e}"
                    ) from e
                raise LLMRateLimitError(
                    provider="gemini_batch",
                    model="unknown",
                    retry_after=retry_delay,
                    message=f"Batch cancellation failed: {e}"
                ) from e
            raise RuntimeError(f"Cancellation failed: {e}")
    
    async def list_batches(self, limit: int = 20) -> List[Dict[str, Any]]:
        """List batch jobs.
        
        Args:
            limit: Max number of batches to return
            
        Returns:
            List of batch info dicts
        """
        try:
            # SDK's list() doesn't take page_size, just iterate
            # Run sync SDK call in thread pool to avoid blocking event loop
            batches = await asyncio.to_thread(self._sdk_client.batches.list)
            result = []
            for i, b in enumerate(batches):
                if i >= limit:
                    break
                # b.state is an enum, get its name (e.g., "JOB_STATE_SUCCEEDED")
                state_str = b.state.name if hasattr(b.state, 'name') else str(b.state)
                logger.debug(f"Batch {b.name}: state={b.state}, state_str={state_str}")
                result.append({"name": b.name, "state": state_str})
            logger.debug(f"Listed {len(result)} batches from Gemini")
            return result
        except Exception as e:
            logger.error(f"Failed to list batches: {e}")
            return []
    
    def describe_listed_batch(
        self, batch_info: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Gemini listing shape: `name` + `state` (JOB_STATE_*)."""
        state = batch_info.get("state", "")
        if state in ("JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED",
                     "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"):
            return None  # finished — nothing to recover
        job_id = batch_info.get("name", "")
        if not job_id:
            return None
        mapping = {
            "JOB_STATE_PENDING": BatchStatus.SUBMITTED,
            "JOB_STATE_RUNNING": BatchStatus.IN_PROGRESS,
        }
        return {
            "job_id": job_id,
            "status": mapping.get(state, BatchStatus.SUBMITTED),
            # The listing carries no model name; recovery only polls and
            # collects results, so a placeholder is honest here.
            "model": batch_info.get("model") or "unknown",
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
            logger.debug("No tracked Gemini batch jobs to cancel")
            return 0
        
        logger.info(f"Found {len(tracked_jobs)} tracked Gemini batch jobs to check")
        cancelled = 0
        
        for job_id in tracked_jobs:
            try:
                # get_batch_status returns the MAPPED status under "status" —
                # there is no "state" key. Reading one always yielded "",
                # which is in no terminal set, so every tracked job was
                # cancelled: finished ones got a pointless cancel call that
                # failed and only showed up as a warning.
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
