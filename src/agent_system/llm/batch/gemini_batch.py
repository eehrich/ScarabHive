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

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

try:
    from google import genai
    from google.genai import types
    HAS_GENAI_SDK = True
except ImportError:
    HAS_GENAI_SDK = False
    genai = None  # type: ignore
    types = None  # type: ignore

from .models import BatchJob, BatchStatus

logger = logging.getLogger(__name__)


class GeminiBatchClient:
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
            contents = self._convert_messages_to_contents(req.messages)
            
            request_dict: Dict[str, Any] = {
                'contents': contents,
            }
            
            # Add tools if present - use SDK types.Tool objects
            if req.tools:
                sdk_tools = self._convert_tools_to_sdk(req.tools)
                if sdk_tools:
                    request_dict['config'] = {'tools': sdk_tools}
            
            inline_requests.append(request_dict)
        
        # Submit batch using SDK with inline requests
        try:
            # Use full model path for SDK
            model_name = job.model
            if not model_name.startswith("models/"):
                model_name = f"models/{model_name}"
            
            logger.debug("Submitting batch via SDK: model=%s, requests=%d", 
                        model_name, len(inline_requests))
            
            batch_job = self._sdk_client.batches.create(
                model=model_name,
                src=inline_requests,
                config={
                    'display_name': f"batch_{job.job_id}",
                },
            )
            logger.info("Created Gemini batch job: %s", batch_job.name)
            return batch_job.name
        except Exception as e:
            logger.error("SDK batch submission failed: %s: %s", type(e).__name__, e)
            raise
    
    def _convert_messages_to_contents(
        self,
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Convert OpenAI-style messages to Gemini content format.
        
        Handles:
        - user messages → role: "user"
        - assistant messages → role: "model"
        - assistant with tool_calls → role: "model" with functionCall parts
        - tool responses → role: "tool" with functionResponse parts (Gemini 2.0+ format)
        - system messages → skipped (handled separately via system_instruction)
        """
        import json
        contents = []
        
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
                
                contents.append({
                    "role": "tool",  # Gemini 2.0+ uses "tool" role
                    "parts": [{
                        "functionResponse": {
                            "name": tool_name,
                            "response": result_data
                        }
                    }]
                })
                continue
            
            # Handle assistant with tool_calls (function call requests)
            if role == "assistant" and msg.get("tool_calls"):
                parts = []
                
                # Add text content if present
                if content:
                    parts.append({"text": content})
                
                # Add function calls
                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    func_name = func.get("name", "")
                    func_args = func.get("arguments", "{}")
                    
                    # Parse args if string
                    if isinstance(func_args, str):
                        try:
                            func_args = json.loads(func_args)
                        except json.JSONDecodeError:
                            func_args = {}
                    
                    parts.append({
                        "functionCall": {
                            "name": func_name,
                            "args": func_args
                        }
                    })
                
                if parts:
                    contents.append({
                        "role": "model",
                        "parts": parts,
                    })
                continue
            
            # Standard user/assistant messages
            gemini_role = "user" if role == "user" else "model"
            
            if isinstance(content, str):
                contents.append({
                    "role": gemini_role,
                    "parts": [{"text": content}],
                })
            elif isinstance(content, list):
                # Multimodal content
                parts = []
                for item in content:
                    if item.get("type") == "text":
                        parts.append({"text": item.get("text", "")})
                    elif item.get("type") in ("image", "image_url"):
                        # Handle image content
                        image_url = item.get("image_url", {})
                        if isinstance(image_url, dict):
                            url = image_url.get("url", "")
                        else:
                            url = image_url
                        
                        if url.startswith("data:"):
                            # Parse data URL
                            try:
                                header, data = url.split(",", 1)
                                mime_type = header.split(":")[1].split(";")[0]
                                parts.append({
                                    "inlineData": {
                                        "mimeType": mime_type,
                                        "data": data,
                                    }
                                })
                            except (ValueError, IndexError):
                                pass
                
                if parts:
                    contents.append({
                        "role": gemini_role,
                        "parts": parts,
                    })
        
        return contents
    
    def _sanitize_schema_for_sdk(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        """Remove JSON Schema keywords not supported by Gemini API.
        
        Gemini's Function Declaration schema doesn't support:
        - oneOf, anyOf, allOf (JSON Schema composition)
        - $ref (references)
        - additionalProperties (Gemini uses strict schemas)
        - default (default values)
        - examples
        - $schema, $id (meta keywords)
        
        This recursively processes the schema and removes unsupported
        constructs.
        """
        if not isinstance(schema, dict):
            return schema
        
        # Keywords that Gemini doesn't support at all
        unsupported_keywords = {
            "oneOf", "anyOf", "allOf", "$ref", 
            "additionalProperties", "default", "examples",
            "$schema", "$id", "definitions", "$defs",
            "patternProperties", "unevaluatedProperties",
            "if", "then", "else", "not",
        }
        
        result = {}
        for key, value in schema.items():
            # Skip completely unsupported keywords
            if key in unsupported_keywords:
                # For oneOf/anyOf/allOf, try to merge first option
                if key in ("oneOf", "anyOf", "allOf"):
                    if isinstance(value, list) and len(value) > 0:
                        first_option = value[0]
                        if isinstance(first_option, dict):
                            for opt_key, opt_val in first_option.items():
                                if opt_key not in result and opt_key not in unsupported_keywords:
                                    result[opt_key] = self._sanitize_schema_for_sdk(opt_val)
                continue
            elif key == "properties" and isinstance(value, dict):
                # Recursively sanitize properties
                result[key] = {
                    k: self._sanitize_schema_for_sdk(v) 
                    for k, v in value.items()
                }
            elif key == "items" and isinstance(value, dict):
                # Recursively sanitize array items
                result[key] = self._sanitize_schema_for_sdk(value)
            else:
                result[key] = value
        
        return result
    
    def _convert_tools_to_sdk(
        self,
        tools: List[Dict[str, Any]],
    ) -> List[types.Tool]:
        """Convert OpenAI tools to SDK format.
        
        Creates proper types.Tool objects with FunctionDeclaration.
        Also sanitizes JSON schemas to remove unsupported keywords like 'oneOf'.
        """
        function_declarations = []
        
        for tool in tools:
            if tool.get("type") != "function":
                continue
            
            func = tool.get("function", {})
            params = func.get("parameters", {})
            
            # Sanitize schema to remove unsupported keywords like 'oneOf'
            sanitized_params = self._sanitize_schema_for_sdk(params) if params else None
            
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
            job = self._sdk_client.batches.get(name=job_name)
            
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
        """Get batch results.
        
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
            batch_job = self._sdk_client.batches.get(name=job.provider_job_id)
            
            results = []
            
            # Check for inline responses
            if hasattr(batch_job, 'dest') and batch_job.dest:
                responses = []
                
                # Try inline responses first
                if hasattr(batch_job.dest, 'inlined_responses') and batch_job.dest.inlined_responses:
                    responses = batch_job.dest.inlined_responses
                
                for i, inline_response in enumerate(responses):
                    if i < len(job.requests):
                        custom_id = job.requests[i].custom_id
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
                            if hasattr(candidate, "content") and candidate.content:
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
            } for req in job.requests]
            
        except Exception as e:
            logger.error(f"Failed to get batch results: {e}")
            # Return error for all requests
            return [{
                "custom_id": req.custom_id,
                "response": None,
                "error": {"message": str(e)},
            } for req in job.requests]
    
    async def cancel_batch(self, job_name: str) -> Dict[str, Any]:
        """Cancel a batch job.
        
        Args:
            job_name: Gemini batch job name
            
        Returns:
            Cancellation response
        """
        try:
            self._sdk_client.batches.cancel(name=job_name)
            return {"status": "cancelled"}
        except Exception as e:
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
            batches = self._sdk_client.batches.list()
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
    
    async def cancel_all_pending_batches(self) -> int:
        """Cancel all non-completed batches.
        
        Returns:
            Number of batches cancelled
        """
        batches = await self.list_batches(limit=100)
        cancelled = 0
        
        for batch in batches:
            state = batch.get("state", "")
            if state not in ("JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED"):
                try:
                    await self.cancel_batch(batch.get("name"))
                    cancelled += 1
                    logger.info(f"Cancelled batch {batch.get('name')}")
                except Exception as e:
                    logger.warning(f"Failed to cancel batch {batch.get('name')}: {e}")
        
        return cancelled
