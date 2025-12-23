"""
Google Gemini native API client.

Uses Gemini's native REST API instead of OpenAI compatibility layer
to avoid issues with tool_calls index handling and thought_signature requirements.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Dict, List, Optional

import httpx

from .models import ChatMessage
from .clients import LLMClient

logger = logging.getLogger(__name__)


def _parse_retry_delay(error_msg: str) -> float | None:
    """Parse retry delay from Gemini 429 error message.
    
    Looks for patterns like:
    - 'Please retry in 32.487019579s'
    - 'retryDelay': '32s'
    
    Returns delay in seconds, or None if not found.
    """
    # Try to find "Please retry in Xs" pattern
    match = re.search(r'retry in ([\d.]+)s', error_msg, re.IGNORECASE)
    if match:
        return float(match.group(1))
    
    # Try to find retryDelay JSON pattern
    match = re.search(r'"retryDelay"\s*:\s*"(\d+)s?"', error_msg)
    if match:
        return float(match.group(1))
    
    return None


def _is_rate_limit_error(error: Exception) -> bool:
    """Check if error is a rate limit (429) error."""
    error_str = str(error).lower()
    return (
        '429' in error_str or
        'rate limit' in error_str or
        'resource_exhausted' in error_str or
        'quota' in error_str or
        'too many requests' in error_str
    )


class GeminiClient(LLMClient):
    """Native Google Gemini API client."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        context_window: int = 200000,
        request_timeout: int = 180,
        ssl_verify: bool | str = True,
        httpx_timeouts: dict | None = None,
        max_retries: int = 3,
        parallel_tool_calls: bool = True,
        include_thoughts: bool | None = None,
        thinking_budget: int | None = None,
        **extra_params
    ):
        self.model = model
        self.model_name = model  # For token tracking
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.context_window = context_window
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.parallel_tool_calls = parallel_tool_calls
        self.extra_params = extra_params
        
        # Store include_thoughts in extra_params for consistency
        if include_thoughts is not None:
            self.extra_params["include_thoughts"] = include_thoughts
        
        # Store thinking_budget in extra_params for consistency
        if thinking_budget is not None:
            self.extra_params["thinking_budget"] = thinking_budget

        # Setup HTTPX timeouts
        if httpx_timeouts:
            self.timeouts = httpx.Timeout(**httpx_timeouts)
        else:
            self.timeouts = httpx.Timeout(
                connect=10.0,
                read=float(request_timeout),
                write=10.0,
                pool=5.0
            )

        # SSL verification
        if isinstance(ssl_verify, str):
            self.verify = ssl_verify
        else:
            self.verify = ssl_verify

        logger.debug(
            f"GeminiClient initialized model={model} base_url={base_url} verify={self.verify} include_thoughts={self.extra_params.get('include_thoughts')} thinking_budget={self.extra_params.get('thinking_budget')}"
        )

    def _convert_messages_to_gemini(self, messages: List[ChatMessage]) -> tuple[Optional[str], List[Dict]]:
        """Convert ChatMessage list to Gemini format.
        
        Returns:
            (system_instruction, contents) tuple
        """
        system_instructions: List[str] = []
        contents = []
        
        # Add critical instruction to prevent MALFORMED_FUNCTION_CALL
        # (Gemini sometimes generates Python code instead of JSON for function calls)
        system_instructions.append(
            "CRITICAL: When calling functions, output the function name exactly as defined. "
            "Do NOT prepend 'default_api.' or any other namespace. "
            "Always generate valid JSON for function arguments. "
            "Properly escape all special characters in JSON strings (quotes, backslashes, newlines)."
        )

        for msg in messages:
            if msg.role == "system":
                # Gemini uses systemInstruction separately - collect ALL system messages
                content = msg.content if isinstance(msg.content, str) else ""
                if content:
                    system_instructions.append(content)
                    logger.debug(f"Collected system instruction: {len(content)} chars")
                continue

            # Map roles
            role = "user" if msg.role == "user" else "model"

            # Handle tool responses
            if msg.role == "tool":
                # Tool responses use role="tool" (per Gemini SDK docs)
                # Note: "function" was the old Gemini 1.5 convention
                tool_name = msg.name or "unknown"
                
                # Try to parse as JSON, fallback to string content
                if isinstance(msg.content, str):
                    try:
                        result_data = json.loads(msg.content)
                    except json.JSONDecodeError:
                        # Content is not valid JSON (e.g., compacted reference like "[Tool:xxx ref:yyy]")
                        result_data = {"result": msg.content}
                else:
                    result_data = msg.content
                
                # Convert our error format to Gemini's expected format
                # Our format: {"error": True, "message": "..."}
                # Gemini format: {"error": "..."}
                if isinstance(result_data, dict) and result_data.get("error") is True:
                    error_message = result_data.get("message", "Unknown error")
                    result_data = {"error": error_message}
                    logger.warning(f"[Gemini] DEPRECATED: Tool returned old error format. Converted for {tool_name}: {error_message[:100]}")
                
                content = {
                    "role": "tool",
                    "parts": [{
                        "functionResponse": {
                            "name": tool_name,
                            "response": result_data
                        }
                    }]
                }
                contents.append(content)
                continue

            # Handle assistant with tool_calls
            if msg.role == "assistant" and msg.tool_calls:
                parts = []
                # Add text if present
                if msg.content:
                    parts.append({"text": msg.content})
                
                # Add function calls with thought_signature preservation
                for idx, tc in enumerate(msg.tool_calls):
                    func = tc.get("function", {})
                    func_name = func.get("name", "")
                    func_args = func.get("arguments", "{}")
                    
                    # Parse args if string
                    if isinstance(func_args, str):
                        try:
                            func_args = json.loads(func_args)
                        except json.JSONDecodeError:
                            func_args = {}
                    
                    part = {
                        "functionCall": {
                            "name": func_name,
                            "args": func_args
                        }
                    }
                    
                    # Preserve thoughtSignature if present (required for Gemini 3 Pro)
                    # The signature is stored in extra_content.google.thought_signature
                    extra_content = tc.get("extra_content", {})
                    google_extra = extra_content.get("google", {})
                    thought_sig = google_extra.get("thought_signature")
                    
                    # Also check direct thought_signature field
                    if not thought_sig:
                        thought_sig = tc.get("thought_signature")
                    
                    if thought_sig:
                        part["thoughtSignature"] = thought_sig
                        logger.debug(f"[Gemini] Restored thoughtSignature for {func_name}")
                    # IMPORTANT: Do NOT set skip_thought_signature_validator for historical function calls
                    # Gemini will handle this automatically. Setting it explicitly can cause
                    # MALFORMED_FUNCTION_CALL errors when the model tries to validate historical calls.
                    
                    parts.append(part)
                
                contents.append({"role": role, "parts": parts})
                continue

            # Regular message - handle both string and multimodal content
            if isinstance(msg.content, str):
                # Simple text message
                if msg.content:
                    contents.append({
                        "role": role,
                        "parts": [{"text": msg.content}]
                    })
            elif isinstance(msg.content, list):
                # Multimodal content (text + images/etc.)
                parts = []
                for item in msg.content:
                    # Handle dict format (direct JSON)
                    if isinstance(item, dict):
                        content_type = item.get("type")
                        
                        if content_type == "text":
                            text_val = item.get("text", "")
                            if text_val:
                                parts.append({"text": text_val})
                        
                        elif content_type in ("image", "image_url"):
                            # Extract image data
                            image_url = item.get("image_url")
                            image_source = item.get("source")
                            
                            # Handle OpenAI format: image_url can be string or dict with "url" key
                            data_url = None
                            if isinstance(image_url, str):
                                data_url = image_url
                            elif isinstance(image_url, dict):
                                data_url = image_url.get("url")
                            
                            # Handle Anthropic format: source with base64 data
                            if not data_url and image_source:
                                if isinstance(image_source, dict):
                                    source_type = image_source.get("type")
                                    if source_type == "base64":
                                        media_type = image_source.get("media_type", "image/jpeg")
                                        data = image_source.get("data", "")
                                        if data:
                                            data_url = f"data:{media_type};base64,{data}"
                                    elif source_type == "url":
                                        data_url = image_source.get("url")
                            
                            # Convert data URL to Gemini inlineData format
                            if data_url:
                                if data_url.startswith("data:"):
                                    # Parse data URL: data:image/png;base64,iVBORw0KG...
                                    try:
                                        header, base64_data = data_url.split(",", 1)
                                        mime_type = header.split(":")[1].split(";")[0]
                                        parts.append({
                                            "inlineData": {
                                                "mimeType": mime_type,
                                                "data": base64_data
                                            }
                                        })
                                    except (ValueError, IndexError) as e:
                                        logger.warning(f"[Gemini] Failed to parse data URL: {e}")
                                else:
                                    # External URL - Gemini doesn't support external URLs directly
                                    # Would need to download and convert to base64
                                    logger.warning(f"[Gemini] External image URLs not yet supported: {data_url[:100]}")
                    
                    # Handle Pydantic model objects (TextContent, ImageContent, etc.)
                    elif hasattr(item, "type"):
                        if item.type == "text" and hasattr(item, "text"):
                            if item.text:
                                parts.append({"text": item.text})
                        
                        elif item.type in ("image", "image_url"):
                            # Extract from Pydantic ImageContent model
                            image_url = getattr(item, "image_url", None)
                            image_source = getattr(item, "source", None)
                            
                            data_url = None
                            if isinstance(image_url, str):
                                data_url = image_url
                            elif isinstance(image_url, dict):
                                data_url = image_url.get("url")
                            
                            if not data_url and image_source:
                                if hasattr(image_source, "type"):
                                    if image_source.type == "base64":
                                        media_type = getattr(image_source, "media_type", "image/jpeg")
                                        data = getattr(image_source, "data", "")
                                        if data:
                                            data_url = f"data:{media_type};base64,{data}"
                                    elif image_source.type == "url":
                                        data_url = getattr(image_source, "url", None)
                            
                            # Convert to Gemini format
                            if data_url:
                                if data_url.startswith("data:"):
                                    try:
                                        header, base64_data = data_url.split(",", 1)
                                        mime_type = header.split(":")[1].split(";")[0]
                                        parts.append({
                                            "inlineData": {
                                                "mimeType": mime_type,
                                                "data": base64_data
                                            }
                                        })
                                    except (ValueError, IndexError) as e:
                                        logger.warning(f"[Gemini] Failed to parse data URL from Pydantic model: {e}")
                                else:
                                    logger.warning(f"[Gemini] External image URLs not yet supported: {data_url[:100]}")
                
                if parts:
                    contents.append({
                        "role": role,
                        "parts": parts
                    })

        # Merge all system instructions (first one is the main prompt, others are context additions)
        system_instruction = None
        if system_instructions:
            system_instruction = "\n\n".join(system_instructions)
            logger.debug(f"Final merged system instruction: {len(system_instruction)} chars from {len(system_instructions)} parts")

        return system_instruction, contents

    def _convert_tools_to_gemini(self, tools: List[Dict]) -> List[Dict]:
        """Convert OpenAI tool schema to Gemini function declarations."""
        function_declarations = []

        for tool in tools:
            if tool.get("type") != "function":
                continue

            func = tool.get("function", {})
            declaration = {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
            }

            # Add parameters if present, but clean them for Gemini compatibility
            params = func.get("parameters", {})
            if params:
                # Deep copy to avoid modifying original
                clean_params = self._clean_schema_for_gemini(params)
                declaration["parameters"] = clean_params

            function_declarations.append(declaration)

        return function_declarations

    def _clean_schema_for_gemini(self, schema: Dict) -> Dict:
        """Remove fields that Gemini doesn't support from JSON schema."""
        if not isinstance(schema, dict):
            return schema
        
        # Create a copy to avoid modifying original
        cleaned = {}
        
        # Fields to exclude (Gemini doesn't support these OpenAI-specific fields)
        exclude_fields = {"additionalProperties", "$schema", "$defs", "definitions"}
        
        for key, value in schema.items():
            if key in exclude_fields:
                continue
                
            if isinstance(value, dict):
                cleaned[key] = self._clean_schema_for_gemini(value)
            elif isinstance(value, list):
                cleaned[key] = [
                    self._clean_schema_for_gemini(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                cleaned[key] = value
        
        return cleaned

    async def chat_tools_streaming(
        self,
        messages: List[ChatMessage],
        tools: List[Dict],
        cancellation_token=None
    ):
        """Stream chat with tools using Gemini native API."""
        system_instruction, contents = self._convert_messages_to_gemini(messages)
        function_declarations = self._convert_tools_to_gemini(tools)

        # Build generationConfig
        generation_config: dict = {
            "temperature": self.extra_params.get("temperature", 1.0),
            "topP": self.extra_params.get("top_p", 0.95),
            "topK": self.extra_params.get("top_k", 40),
        }

        # Optional: enable Gemini "thought summaries" in responses.
        # When enabled, Gemini may emit parts with {"text": "...", "thought": true}.
        # thinkingConfig must be inside generationConfig.
        # - Gemini 2.5: use thinkingBudget (e.g. 8192)
        # - Gemini 3: use thinkingBudget (works for both) or thinkingLevel
        if self.extra_params.get("include_thoughts") is True:
            budget = self.extra_params.get("thinking_budget", 8192)
            generation_config["thinkingConfig"] = {
                "thinkingBudget": budget,
                "includeThoughts": True
            }

        payload = {
            "contents": contents,
            "generationConfig": generation_config
        }

        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        if function_declarations:
            payload["tools"] = [{
                "functionDeclarations": function_declarations
            }]

        url = f"{self.base_url}/models/{self.model}:streamGenerateContent?key={self.api_key}&alt=sse"

        # Accumulators
        accumulated_content = []  # Only non-thought content (for final message)
        accumulated_thoughts = []  # Thought summaries (streamed but not saved)
        accumulated_tool_calls = {}  # id -> tool call
        accumulated_usage = None
        # For parallel function calls: store the first thought_signature to propagate to all calls
        first_thought_signature = None
        # Track MALFORMED_FUNCTION_CALL for auto-retry
        got_malformed_function_call = False

        last_exception = None
        for attempt in range(self.max_retries + 1):
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled before attempt")
            
            # On retry after MALFORMED_FUNCTION_CALL, force function calling with mode=ANY
            # This helps the model generate proper JSON instead of Python code
            if attempt > 0 and got_malformed_function_call:
                logger.info(f"[Gemini] Retry #{attempt} with forced function calling (mode=ANY)")
                if "toolConfig" not in payload:
                    payload["toolConfig"] = {}
                payload["toolConfig"]["functionCallingConfig"] = {"mode": "ANY"}

            try:
                logger.debug(f"Gemini streaming: Starting request to {self.model}")
                async with httpx.AsyncClient(timeout=self.timeouts, verify=self.verify) as client:
                    async with client.stream("POST", url, json=payload) as response:
                        # Handle server errors (5xx) - retry with exponential backoff
                        if response.status_code >= 500 and attempt < self.max_retries:
                            wait_time = 2 ** attempt
                            logger.warning(f"Gemini server error {response.status_code}, retrying in {wait_time}s")
                            await asyncio.sleep(wait_time)
                            continue
                        
                        if response.status_code != 200:
                            error_bytes = await response.aread()
                            error_text = error_bytes.decode('utf-8', errors='replace')
                            error_msg = f"HTTP {response.status_code}: {error_text}"
                            logger.error(f"Gemini streaming request failed: {error_msg}")
                            raise httpx.HTTPStatusError(error_msg, request=response.request, response=response)

                        logger.debug("Gemini streaming: Response started, reading chunks...")
                        
                        # Use timeout from config for chunk-level timeout
                        chunk_timeout = self.timeouts.read if hasattr(self.timeouts, 'read') else self.request_timeout
                        line_iter = response.aiter_lines().__aiter__()
                        
                        while True:
                            if cancellation_token and cancellation_token.is_cancelled:
                                raise asyncio.CancelledError("Request cancelled during streaming")
                            
                            try:
                                line = await asyncio.wait_for(line_iter.__anext__(), timeout=chunk_timeout)
                            except StopAsyncIteration:
                                break  # Stream completed
                            except asyncio.TimeoutError:
                                logger.warning(f"Gemini stream chunk timeout after {chunk_timeout}s")
                                raise httpx.RemoteProtocolError(f"Stream stalled - no data for {chunk_timeout}s")

                            if not line or not line.startswith("data: "):
                                continue

                            data = line[6:]
                            if data == "[DONE]":
                                break

                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                logger.debug(f"Failed to parse chunk: {data[:100]}")
                                continue

                            # Process candidates
                            candidates = chunk.get("candidates", [])
                            if not candidates:
                                logger.debug("Gemini chunk has no candidates, skipping")
                                continue

                            candidate = candidates[0]
                            content = candidate.get("content", {})
                            parts = content.get("parts", [])
                            
                            # Check for MALFORMED_FUNCTION_CALL finish reason
                            finish_reason = candidate.get("finishReason")
                            if finish_reason:
                                logger.debug(f"Gemini chunk finishReason: {finish_reason}")
                                if finish_reason == "MALFORMED_FUNCTION_CALL":
                                    got_malformed_function_call = True
                                    logger.warning("[Gemini] MALFORMED_FUNCTION_CALL detected in chunk")
                            
                            logger.debug(f"Gemini chunk: {len(parts)} parts")

                            for part in parts:
                                # Handle text (both normal content and thought summaries)
                                if "text" in part:
                                    text_delta = part["text"]
                                    is_thought = part.get("thought", False)

                                    # Separate thoughts from content
                                    if is_thought:
                                        accumulated_thoughts.append(text_delta)
                                    else:
                                        accumulated_content.append(text_delta)
                                    
                                    logger.debug(f"Gemini text delta: {len(text_delta)} chars (thought={is_thought})")
                                    
                                    # Stream everything live (thoughts + content combined for display)
                                    all_text = "".join(accumulated_thoughts) + "".join(accumulated_content)
                                    yield {
                                        "type": "content_delta",
                                        "delta": text_delta,
                                        "accumulated": all_text
                                    }

                                # Handle function calls
                                if "functionCall" in part:
                                    func_call = part["functionCall"]
                                    func_name = func_call.get("name", "")
                                    func_args = func_call.get("args", {})
                                    
                                    # Extract thoughtSignature if present (Gemini 3 Pro)
                                    thought_signature = part.get("thoughtSignature")
                                    
                                    # For parallel function calls: capture first signature and propagate
                                    if thought_signature and first_thought_signature is None:
                                        first_thought_signature = thought_signature
                                        logger.debug(f"Gemini: captured first thoughtSignature from {func_name}")
                                    
                                    # Use effective signature (original or propagated from first)
                                    effective_signature = thought_signature or first_thought_signature
                                    
                                    logger.debug(f"Gemini function call: {func_name}, has_thought_sig={effective_signature is not None}")
                                    
                                    # Generate stable unique ID for this tool call (UUID4 ensures no collisions)
                                    tool_call_id = f"call_{uuid.uuid4().hex[:16]}"
                                    
                                    tool_call = {
                                        "id": tool_call_id,
                                        "type": "function",
                                        "function": {
                                            "name": func_name,
                                            "arguments": json.dumps(func_args)
                                        }
                                    }
                                    
                                    # Store thoughtSignature for round-trip (Gemini 3 Pro requirement)
                                    # Using OpenAI-compatible format: extra_content.google.thought_signature
                                    if effective_signature:
                                        tool_call["extra_content"] = {
                                            "google": {
                                                "thought_signature": effective_signature
                                            }
                                        }
                                        # Also store directly for easier access
                                        tool_call["thought_signature"] = effective_signature
                                        if thought_signature:
                                            logger.debug(f"Including thoughtSignature for {func_name} (original)")
                                        else:
                                            logger.debug(f"Including thoughtSignature for {func_name} (propagated from first)")
                                    
                                    accumulated_tool_calls[tool_call_id] = tool_call
                                    
                                    logger.debug(f"Yielding tool_call_delta for {func_name}")
                                    yield {
                                        "type": "tool_call_delta",
                                        "index": len(accumulated_tool_calls) - 1,
                                        "delta": {"function": {"name": func_name}},
                                        "accumulated": tool_call
                                    }

                            # Handle usage metadata (at top level of chunk, not in candidate)
                            usage_metadata = chunk.get("usageMetadata")
                            if usage_metadata:
                                # Gemini API uses snake_case: prompt_token_count, candidates_token_count, total_token_count, cached_content_token_count
                                accumulated_usage = {
                                    "prompt_tokens": usage_metadata.get("promptTokenCount", usage_metadata.get("prompt_token_count", 0)),
                                    "completion_tokens": usage_metadata.get("candidatesTokenCount", usage_metadata.get("candidates_token_count", 0)),
                                    "total_tokens": usage_metadata.get("totalTokenCount", usage_metadata.get("total_token_count", 0))
                                }
                                
                                # Extract cached tokens (implicit caching for Gemini 2.5+)
                                cached_tokens = usage_metadata.get("cachedContentTokenCount", usage_metadata.get("cached_content_token_count", 0))
                                if cached_tokens > 0:
                                    # Store in OpenAI-compatible format: prompt_tokens_details.cached_tokens
                                    accumulated_usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}

                        # Stream finished successfully
                        logger.debug(f"Gemini streaming complete: {len(accumulated_content)} content parts, {len(accumulated_tool_calls)} tool calls")
                        
                        # Check for MALFORMED_FUNCTION_CALL with empty response - auto-retry
                        if got_malformed_function_call and not accumulated_content and not accumulated_tool_calls:
                            if attempt < self.max_retries:
                                wait_time = 1.0 + attempt  # 1s, 2s, 3s
                                logger.warning(
                                    f"[Gemini] MALFORMED_FUNCTION_CALL with empty response. "
                                    f"Retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries + 1})"
                                )
                                await asyncio.sleep(wait_time)
                                # Reset accumulators for retry (keep got_malformed_function_call=True for mode=ANY)
                                accumulated_content = []
                                accumulated_thoughts = []
                                accumulated_tool_calls = {}
                                accumulated_usage = None
                                first_thought_signature = None
                                # DON'T reset got_malformed_function_call - we need it for mode=ANY in retry
                                continue
                            else:
                                logger.error(
                                    "[Gemini] MALFORMED_FUNCTION_CALL persisted after all retries. "
                                    "This may indicate invalid tool schema or complex function call arguments."
                                )
                        
                        assistant = {
                            "role": "assistant",
                            "content": "".join(accumulated_content) if accumulated_content else ""
                        }

                        if accumulated_tool_calls:
                            assistant["tool_calls"] = list(accumulated_tool_calls.values())

                        final_result = {"assistant": assistant}
                        if accumulated_usage:
                            final_result["usage"] = accumulated_usage

                        logger.debug("Yielding final result")
                        yield {"type": "final", **final_result}
                        return  # Success

            except asyncio.CancelledError:
                logger.info("Gemini streaming request cancelled by user")
                raise

            except httpx.TimeoutException as e:
                last_exception = e
                if attempt < self.max_retries:
                    wait_time = 2 ** attempt
                    logger.warning(f"Gemini request timeout, retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries + 1})")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Gemini request failed after {self.max_retries + 1} attempts")
                    raise Exception(f"Request timeout after {self.max_retries + 1} attempts") from e

            except Exception as e:
                last_exception = e
                error_str = str(e)
                
                # Check if this is a rate limit error
                is_rate_limit = _is_rate_limit_error(e)
                
                if is_rate_limit:
                    # Parse retry delay from error message, default to 60s for rate limits
                    parsed_delay = _parse_retry_delay(error_str)
                    wait_time = parsed_delay if parsed_delay else 60.0
                    # Add small buffer to parsed delay
                    if parsed_delay:
                        wait_time = parsed_delay + 2.0
                    
                    logger.warning(
                        f"Gemini rate limit hit (429). Waiting {wait_time:.1f}s before retry "
                        f"(attempt {attempt + 1}/{self.max_retries + 1})"
                    )
                    await asyncio.sleep(wait_time)
                    continue
                
                logger.error(f"Gemini streaming error: {e}", exc_info=True)
                if attempt < self.max_retries:
                    wait_time = 2 ** attempt
                    logger.warning(f"Retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries + 1})")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"Gemini streaming failed: {str(e)}") from e

        # If we get here, all retries failed
        if last_exception:
            raise Exception(f"Gemini streaming failed after {self.max_retries + 1} attempts") from last_exception
        raise Exception(f"Gemini streaming failed after {self.max_retries + 1} attempts")

    async def chat_tools(
        self,
        messages: List[ChatMessage],
        tools: List[Dict],
        cancellation_token=None
    ) -> Dict:
        """Non-streaming chat with tools using Gemini native API."""
        system_instruction, contents = self._convert_messages_to_gemini(messages)
        function_declarations = self._convert_tools_to_gemini(tools)

        # Build generationConfig
        generation_config: dict = {
            "temperature": self.extra_params.get("temperature", 1.0),
            "topP": self.extra_params.get("top_p", 0.95),
            "topK": self.extra_params.get("top_k", 40),
        }

        # Optional: enable Gemini "thought summaries" in responses.
        # When enabled, Gemini may emit parts with {"text": "...", "thought": true}.
        # thinkingConfig must be inside generationConfig.
        if self.extra_params.get("include_thoughts") is True:
            budget = self.extra_params.get("thinking_budget", 8192)
            generation_config["thinkingConfig"] = {
                "thinkingBudget": budget,
                "includeThoughts": True
            }

        payload = {
            "contents": contents,
            "generationConfig": generation_config
        }

        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        if function_declarations:
            payload["tools"] = [{
                "functionDeclarations": function_declarations
            }]

        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"

        # Track MALFORMED_FUNCTION_CALL for auto-retry
        got_malformed_function_call = False
        last_exception = None
        for attempt in range(self.max_retries + 1):
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled before attempt")
            
            # On retry after MALFORMED_FUNCTION_CALL, force function calling with mode=ANY
            if attempt > 0 and got_malformed_function_call:
                logger.info(f"[Gemini] Non-streaming retry #{attempt} with forced function calling (mode=ANY)")
                if "toolConfig" not in payload:
                    payload["toolConfig"] = {}
                payload["toolConfig"]["functionCallingConfig"] = {"mode": "ANY"}

            try:
                async with httpx.AsyncClient(timeout=self.timeouts, verify=self.verify) as client:
                    response = await client.post(url, json=payload)

                    if response.status_code != 200:
                        error_text = response.text
                        error_msg = f"HTTP {response.status_code}: {error_text}"
                        logger.error(f"Gemini request failed: {error_msg}")
                        raise httpx.HTTPStatusError(error_msg, request=response.request, response=response)

                    data = response.json()

                    # Extract response
                    candidates = data.get("candidates", [])
                    if not candidates:
                        return {
                            "assistant": {"role": "assistant", "content": ""},
                            "usage": {}
                        }

                    candidate = candidates[0]
                    content = candidate.get("content", {})
                    parts = content.get("parts", [])
                    
                    # Check for MALFORMED_FUNCTION_CALL finish reason
                    finish_reason = candidate.get("finishReason")
                    if finish_reason == "MALFORMED_FUNCTION_CALL":
                        got_malformed_function_call = True
                        logger.warning("[Gemini] Non-streaming MALFORMED_FUNCTION_CALL detected")
                        
                        # Retry if we have attempts left
                        if attempt < self.max_retries:
                            wait_time = 1.0 + attempt  # 1s, 2s, 3s
                            logger.warning(
                                f"[Gemini] MALFORMED_FUNCTION_CALL detected. "
                                f"Retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries + 1})"
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            logger.error(
                                "[Gemini] MALFORMED_FUNCTION_CALL persisted after all retries. "
                                "This may indicate invalid tool schema or complex function call arguments."
                            )

                    # Build assistant message
                    assistant = {"role": "assistant", "content": ""}
                    tool_calls = []
                    text_parts = []
                    # For parallel function calls: track first thought_signature
                    first_thought_signature = None

                    for part in parts:
                        if "text" in part:
                            text_parts.append(part["text"])
                        
                        if "functionCall" in part:
                            func_call = part["functionCall"]
                            func_name = func_call.get("name", "")
                            func_args = func_call.get("args", {})
                            
                            # Extract thoughtSignature (Gemini 3 Pro requirement)
                            thought_signature = part.get("thoughtSignature")
                            
                            # For parallel function calls: capture first and propagate
                            if thought_signature and first_thought_signature is None:
                                first_thought_signature = thought_signature
                                logger.debug(f"Non-streaming: captured first thoughtSignature from {func_name}")
                            
                            # Use effective signature (original or propagated)
                            effective_signature = thought_signature or first_thought_signature
                            
                            # Generate stable unique ID for this tool call (UUID4 ensures no collisions)
                            tool_call_id = f"call_{uuid.uuid4().hex[:16]}"
                            tool_call = {
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": func_name,
                                    "arguments": json.dumps(func_args)
                                }
                            }
                            
                            # Store thoughtSignature for round-trip
                            if effective_signature:
                                tool_call["extra_content"] = {
                                    "google": {
                                        "thought_signature": effective_signature
                                    }
                                }
                                tool_call["thought_signature"] = effective_signature
                                if thought_signature:
                                    logger.debug(f"Non-streaming: stored thought_signature for {func_name} (original)")
                                else:
                                    logger.debug(f"Non-streaming: stored thought_signature for {func_name} (propagated)")
                            
                            tool_calls.append(tool_call)

                    assistant["content"] = "".join(text_parts)
                    if tool_calls:
                        assistant["tool_calls"] = tool_calls

                    # Extract usage from top-level response (not from candidate!)
                    usage_metadata = data.get("usageMetadata", {})
                    
                    # Gemini API uses snake_case: prompt_token_count, candidates_token_count, total_token_count, cached_content_token_count
                    usage = {
                        "prompt_tokens": usage_metadata.get("promptTokenCount", usage_metadata.get("prompt_token_count", 0)),
                        "completion_tokens": usage_metadata.get("candidatesTokenCount", usage_metadata.get("candidates_token_count", 0)),
                        "total_tokens": usage_metadata.get("totalTokenCount", usage_metadata.get("total_token_count", 0))
                    }
                    
                    # Extract cached tokens (implicit caching for Gemini 2.5+)
                    cached_tokens = usage_metadata.get("cachedContentTokenCount", usage_metadata.get("cached_content_token_count", 0))
                    if cached_tokens > 0:
                        # Store in OpenAI-compatible format: prompt_tokens_details.cached_tokens
                        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}

                    return {"assistant": assistant, "usage": usage}

            except asyncio.CancelledError:
                logger.info("Gemini request cancelled by user")
                raise

            except httpx.TimeoutException as e:
                last_exception = e
                if attempt < self.max_retries:
                    wait_time = 2 ** attempt
                    logger.warning(f"Gemini request timeout, retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries + 1})")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Gemini request failed after {self.max_retries + 1} attempts")
                    raise Exception(f"Request timeout after {self.max_retries + 1} attempts") from e

            except Exception as e:
                last_exception = e
                error_str = str(e)
                
                # Check if this is a rate limit error
                is_rate_limit = _is_rate_limit_error(e)
                
                if is_rate_limit:
                    # Parse retry delay from error message, default to 60s for rate limits
                    parsed_delay = _parse_retry_delay(error_str)
                    wait_time = parsed_delay if parsed_delay else 60.0
                    # Add small buffer to parsed delay
                    if parsed_delay:
                        wait_time = parsed_delay + 2.0
                    
                    logger.warning(
                        f"Gemini rate limit hit (429). Waiting {wait_time:.1f}s before retry "
                        f"(attempt {attempt + 1}/{self.max_retries + 1})"
                    )
                    await asyncio.sleep(wait_time)
                    continue
                
                logger.error(f"Gemini request error: {e}", exc_info=True)
                if attempt < self.max_retries:
                    wait_time = 2 ** attempt
                    logger.warning(f"Retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries + 1})")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"Gemini request failed: {str(e)}") from e

        # If we get here, all retries failed
        if last_exception:
            raise Exception(f"Gemini request failed after {self.max_retries + 1} attempts") from last_exception
        raise Exception(f"Gemini request failed after {self.max_retries + 1} attempts")

    async def chat(self, messages: List[ChatMessage], cancellation_token=None) -> str:
        """Simple chat without tools."""
        result = await self.chat_tools(messages, [], cancellation_token)
        return result["assistant"]["content"]

    def supports_streaming(self) -> bool:
        """GeminiClient supports true streaming via SSE."""
        return True
