"""Gemini Batch API Client.

Implements batch processing using Google's Gemini Batch API:
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

import httpx

from .models import BatchJob, BatchStatus

logger = logging.getLogger(__name__)


class GeminiBatchClient:
    """Client for Gemini Batch API operations.
    
    The Gemini Batch API provides:
    - 50% cost reduction compared to sync API
    - Higher rate limits
    - Results within 24 hours
    - Support for generateContent endpoint
    
    Two modes are supported:
    1. SDK mode (google-genai): Uses official SDK's batch capabilities
    2. REST mode: Direct HTTP calls to Gemini API
    
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
        use_sdk: bool = True,
    ):
        """Initialize the Gemini batch client.
        
        Args:
            api_key: Gemini API key
            base_url: Base URL for Gemini API
            timeout: Request timeout in seconds
            use_sdk: Use Google GenAI SDK if available (recommended)
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.use_sdk = use_sdk and HAS_GENAI_SDK
        
        if self.use_sdk:
            # Initialize SDK client
            self._sdk_client = genai.Client(api_key=api_key)
            logger.info("GeminiBatchClient initialized with SDK mode")
        else:
            # Use HTTP client
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
            )
            logger.info("GeminiBatchClient initialized with REST mode")
    
    async def close(self) -> None:
        """Close the client."""
        if not self.use_sdk:
            await self._http_client.aclose()
    
    async def submit_batch(
        self,
        job: BatchJob,
        storage_path: Path,
    ) -> str:
        """Submit a batch job to Gemini.
        
        Args:
            job: BatchJob containing requests to submit
            storage_path: Path to store temporary files
            
        Returns:
            Gemini batch job name/ID
        """
        if self.use_sdk:
            return await self._submit_batch_sdk(job)
        else:
            return await self._submit_batch_rest(job, storage_path)
    
    async def _submit_batch_sdk(self, job: BatchJob) -> str:
        """Submit batch using Google GenAI SDK with inline requests.
        
        Uses the SDK's batches.create() with inline requests format.
        Reference: https://ai.google.dev/gemini-api/docs/batch-api
        
        Note: The SDK validates request structure strictly via Pydantic.
        Tools must be SDK types.Tool objects, not dicts.
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
            # Don't fallback to REST - propagate the error so it can be properly handled
            logger.error("SDK batch submission failed: %s: %s", type(e).__name__, e)
            raise
    
    async def _submit_batch_rest(
        self,
        job: BatchJob,
        storage_path: Path,
    ) -> str:
        """Submit batch using REST API.
        
        Uses the Gemini Batch API format:
        https://ai.google.dev/gemini-api/docs/batch-api
        """
        # Create batch request payload with correct nested structure
        requests = []
        for req in job.requests:
            contents = self._convert_messages_to_contents(req.messages)
            
            request_payload = {
                "contents": contents,
            }
            
            if req.tools:
                request_payload["tools"] = self._convert_tools_to_rest(req.tools)
            
            requests.append({
                "request": request_payload,
                "metadata": {
                    "key": req.custom_id,
                }
            })
        
        # Submit batch job - use correct nested format
        url = f"{self.base_url}/models/{job.model}:batchGenerateContent"
        
        # Gemini REST API expects this nested structure:
        # {"batch": {"display_name": "...", "input_config": {"requests": {"requests": [...]}}}}
        payload = {
            "batch": {
                "display_name": f"batch_{job.job_id}",
                "input_config": {
                    "requests": {
                        "requests": requests,
                    }
                }
            }
        }
        
        response = await self._http_client.post(
            url,
            json=payload,
            params={"key": self.api_key},
        )
        
        if response.status_code != 200:
            raise RuntimeError(f"Batch submission failed: {response.status_code} {response.text}")
        
        data = response.json()
        
        # Extract job name from response
        job_name = data.get("name")
        if not job_name:
            # If immediate response (small batch), create synthetic job ID
            job_name = f"batch_{job.job_id}"
            # Store immediate results
            job.metadata["immediate_results"] = data.get("responses", [])
        
        return job_name
    
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
    
    def _convert_tools_to_rest(
        self,
        tools: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Convert OpenAI tools to REST API format.
        
        Also sanitizes JSON schemas to remove unsupported keywords.
        """
        function_declarations = []
        
        for tool in tools:
            if tool.get("type") != "function":
                continue
            
            func = tool.get("function", {})
            declaration = {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
            }
            
            params = func.get("parameters", {})
            if params:
                # Sanitize schema to remove unsupported keywords
                declaration["parameters"] = self._sanitize_schema_for_sdk(params)
            
            function_declarations.append(declaration)
        
        if function_declarations:
            return [{"functionDeclarations": function_declarations}]
        return []
    
    async def get_batch_status(self, job_name: str) -> Dict[str, Any]:
        """Get the status of a batch job.
        
        Args:
            job_name: Gemini batch job name
            
        Returns:
            Status info dict with 'status' key mapping to BatchStatus
        """
        if self.use_sdk:
            return await self._get_status_sdk(job_name)
        else:
            return await self._get_status_rest(job_name)
    
    async def _get_status_sdk(self, job_name: str) -> Dict[str, Any]:
        """Get status using SDK."""
        try:
            job = self._sdk_client.batches.get(name=job_name)
            
            # Map Gemini status to our BatchStatus
            # The state is an enum, access .name for string comparison
            state_name = job.state.name if hasattr(job.state, 'name') else str(job.state)
            logger.debug(f"Gemini batch status for {job_name}: {state_name}")
            
            status_mapping = {
                "STATE_UNSPECIFIED": BatchStatus.PENDING.value,
                "JOB_STATE_PENDING": BatchStatus.PENDING.value,
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
    
    async def _get_status_rest(self, job_name: str) -> Dict[str, Any]:
        """Get status using REST API."""
        # Check if we have immediate results
        if job_name.startswith("batch_"):
            return {"status": BatchStatus.COMPLETED.value}
        
        url = f"{self.base_url}/{job_name}"
        
        response = await self._http_client.get(
            url,
            params={"key": self.api_key},
        )
        
        if response.status_code != 200:
            raise RuntimeError(f"Status check failed: {response.status_code} {response.text}")
        
        data = response.json()
        
        # Map Gemini status
        state = data.get("state", "")
        status_mapping = {
            "STATE_UNSPECIFIED": BatchStatus.PENDING.value,
            "JOB_STATE_PENDING": BatchStatus.PENDING.value,
            "JOB_STATE_RUNNING": BatchStatus.IN_PROGRESS.value,
            "JOB_STATE_SUCCEEDED": BatchStatus.COMPLETED.value,
            "JOB_STATE_FAILED": BatchStatus.FAILED.value,
            "JOB_STATE_CANCELLED": BatchStatus.CANCELLED.value,
        }
        
        return {
            "status": status_mapping.get(state, BatchStatus.IN_PROGRESS.value),
            "error": data.get("error", {}).get("message") if data.get("error") else None,
        }
    
    async def get_batch_results(self, job: BatchJob) -> List[Dict[str, Any]]:
        """Get batch results.
        
        Args:
            job: BatchJob to get results for
            
        Returns:
            List of result dicts with custom_id and response/error
        """
        # Check for immediate results (small batches)
        immediate_results = job.metadata.get("immediate_results")
        if immediate_results:
            return self._parse_immediate_results(job, immediate_results)
        
        if self.use_sdk:
            return await self._get_results_sdk(job)
        else:
            return await self._get_results_rest(job)
    
    def _parse_immediate_results(
        self,
        job: BatchJob,
        responses: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Parse immediate batch results (for small batches)."""
        results = []
        
        for i, response in enumerate(responses):
            # Match with request by index
            if i < len(job.requests):
                req = job.requests[i]
                custom_id = req.custom_id
            else:
                custom_id = f"request_{i}"
            
            if response.get("error"):
                results.append({
                    "custom_id": custom_id,
                    "response": None,
                    "error": response.get("error"),
                })
            else:
                # Extract content from response
                candidates = response.get("candidates", [])
                if candidates:
                    content = candidates[0].get("content", {})
                    results.append({
                        "custom_id": custom_id,
                        "response": {
                            "choices": [{
                                "message": {
                                    "role": "assistant",
                                    "content": self._extract_text_from_parts(
                                        content.get("parts", [])
                                    ),
                                }
                            }],
                            "usage": response.get("usageMetadata", {}),
                        },
                        "error": None,
                    })
                else:
                    results.append({
                        "custom_id": custom_id,
                        "response": None,
                        "error": {"message": "No candidates in response"},
                    })
        
        return results
    
    def _extract_text_from_parts(self, parts: List[Dict[str, Any]]) -> str:
        """Extract text content from Gemini parts."""
        texts = []
        for part in parts:
            if "text" in part:
                texts.append(part["text"])
        return "".join(texts)
    
    async def _get_results_sdk(self, job: BatchJob) -> List[Dict[str, Any]]:
        """Get results using SDK.
        
        According to the Gemini Batch API, results are in:
        - batch_job.dest.inlined_responses for inline requests
        - batch_job.dest.file_name for file-based requests (needs download)
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
                        
                        results.append({
                            "custom_id": custom_id,
                            "response": {
                                "choices": [{
                                    "message": message,
                                }],
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
    
    async def _get_results_rest(self, job: BatchJob) -> List[Dict[str, Any]]:
        """Get results using REST API."""
        url = f"{self.base_url}/{job.provider_job_id}"
        
        response = await self._http_client.get(
            url,
            params={"key": self.api_key},
        )
        
        if response.status_code != 200:
            raise RuntimeError(f"Get results failed: {response.status_code} {response.text}")
        
        data = response.json()
        responses = data.get("responses", [])
        
        return self._parse_immediate_results(job, responses)
    
    async def cancel_batch(self, job_name: str) -> Dict[str, Any]:
        """Cancel a batch job.
        
        Args:
            job_name: Gemini batch job name
            
        Returns:
            Cancellation response
        """
        if self.use_sdk:
            try:
                self._sdk_client.batches.cancel(name=job_name)
                return {"status": "cancelled"}
            except Exception as e:
                raise RuntimeError(f"Cancellation failed: {e}")
        else:
            url = f"{self.base_url}/{job_name}:cancel"
            
            response = await self._http_client.post(
                url,
                params={"key": self.api_key},
            )
            
            if response.status_code != 200:
                raise RuntimeError(f"Cancellation failed: {response.status_code} {response.text}")
            
            return response.json()
    
    async def list_batches(self, limit: int = 20) -> List[Dict[str, Any]]:
        """List batch jobs.
        
        Args:
            limit: Max number of batches to return
            
        Returns:
            List of batch info dicts
        """
        if self.use_sdk:
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
        else:
            # REST API listing not directly supported the same way
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
