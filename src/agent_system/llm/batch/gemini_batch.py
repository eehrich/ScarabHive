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
        """Submit batch using Google GenAI SDK."""
        # Convert requests to SDK format
        requests = []
        for req in job.requests:
            # Convert messages to Gemini content format
            contents = self._convert_messages_to_contents(req.messages)
            
            # Create GenerateContentConfig if tools present
            config = None
            if req.tools:
                config = types.GenerateContentConfig(
                    tools=self._convert_tools_to_sdk(req.tools),
                )
            
            # SDK batch request format
            batch_request = {
                "model": f"models/{req.model}",
                "contents": contents,
            }
            if config:
                batch_request["config"] = config
            
            requests.append(batch_request)
        
        # Submit batch using SDK
        # Note: The actual SDK method may vary - this is based on documented API
        try:
            batch_job = self._sdk_client.batches.create(
                model=f"models/{job.model}",
                requests=requests,
            )
            return batch_job.name
        except AttributeError:
            # Fallback to REST if SDK doesn't have batch support yet
            logger.warning("SDK batch not available, falling back to REST")
            self.use_sdk = False
            return await self._submit_batch_rest(job, Path("data/batch"))
    
    async def _submit_batch_rest(
        self,
        job: BatchJob,
        storage_path: Path,
    ) -> str:
        """Submit batch using REST API."""
        # Create batch request payload
        requests = []
        for req in job.requests:
            contents = self._convert_messages_to_contents(req.messages)
            
            request_payload = {
                "contents": contents,
            }
            
            if req.tools:
                request_payload["tools"] = self._convert_tools_to_rest(req.tools)
            
            requests.append({
                "customId": req.custom_id,
                "request": request_payload,
            })
        
        # Submit batch job
        url = f"{self.base_url}/models/{job.model}:batchGenerateContent"
        
        payload = {
            "requests": requests,
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
        """Convert OpenAI-style messages to Gemini content format."""
        contents = []
        
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            
            if role == "system":
                # Gemini handles system messages differently
                # Prepend to first user message or add as user context
                continue
            
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
    
    def _convert_tools_to_sdk(
        self,
        tools: List[Dict[str, Any]],
    ) -> List[types.Tool]:
        """Convert OpenAI tools to SDK format."""
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
                declaration["parameters"] = params
            
            function_declarations.append(declaration)
        
        if function_declarations:
            return [types.Tool(function_declarations=function_declarations)]
        return []
    
    def _convert_tools_to_rest(
        self,
        tools: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Convert OpenAI tools to REST API format."""
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
                declaration["parameters"] = params
            
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
            state = job.state
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
                "error": job.error.message if hasattr(job, "error") and job.error else None,
            }
        except Exception as e:
            logger.error(f"Failed to get batch status: {e}")
            return {"status": BatchStatus.FAILED.value, "error": str(e)}
    
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
        """Get results using SDK."""
        try:
            batch_job = self._sdk_client.batches.get(name=job.provider_job_id)
            
            results = []
            for i, response in enumerate(batch_job.responses):
                if i < len(job.requests):
                    custom_id = job.requests[i].custom_id
                else:
                    custom_id = f"request_{i}"
                
                if hasattr(response, "error") and response.error:
                    results.append({
                        "custom_id": custom_id,
                        "response": None,
                        "error": {"message": response.error.message},
                    })
                else:
                    # Convert SDK response to dict
                    content = ""
                    if hasattr(response, "candidates") and response.candidates:
                        candidate = response.candidates[0]
                        if hasattr(candidate, "content") and candidate.content:
                            for part in candidate.content.parts:
                                if hasattr(part, "text"):
                                    content += part.text
                    
                    results.append({
                        "custom_id": custom_id,
                        "response": {
                            "choices": [{
                                "message": {
                                    "role": "assistant",
                                    "content": content,
                                }
                            }],
                        },
                        "error": None,
                    })
            
            return results
            
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
                batches = self._sdk_client.batches.list(page_size=limit)
                return [{"name": b.name, "state": b.state} for b in batches]
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
