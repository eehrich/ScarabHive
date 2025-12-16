"""
Google Gemini native API client.

Uses Gemini's native REST API instead of OpenAI compatibility layer
to avoid issues with tool_calls index handling and thought_signature requirements.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Dict, List, Optional

import httpx

from .models import ChatMessage
from .clients import LLMClient

logger = logging.getLogger(__name__)


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
                # Tool responses go as function responses
                content = {
                    "role": "function",
                    "parts": [{
                        "functionResponse": {
                            "name": msg.name or "unknown",
                            "response": json.loads(msg.content) if isinstance(msg.content, str) else msg.content
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
                        logger.debug(f"Including thoughtSignature for {func_name}")
                    else:
                        # For Gemini 3 Pro, use skip signature if missing
                        # This allows history transfer from other models
                        logger.debug(f"No thoughtSignature for {func_name}, using skip validator")
                        part["thoughtSignature"] = "skip_thought_signature_validator"
                    
                    parts.append(part)
                
                contents.append({"role": role, "parts": parts})
                continue

            # Regular message
            content_text = msg.content if isinstance(msg.content, str) else ""
            if content_text:
                contents.append({
                    "role": role,
                    "parts": [{"text": content_text}]
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

        last_exception = None
        for attempt in range(self.max_retries + 1):
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled before attempt")

            try:
                logger.debug(f"Gemini streaming: Starting request to {self.model}")
                async with httpx.AsyncClient(timeout=self.timeouts, verify=self.verify) as client:
                    async with client.stream("POST", url, json=payload) as response:
                        if response.status_code != 200:
                            error_bytes = await response.aread()
                            error_text = error_bytes.decode('utf-8', errors='replace')
                            error_msg = f"HTTP {response.status_code}: {error_text}"
                            logger.error(f"Gemini streaming request failed: {error_msg}")
                            raise httpx.HTTPStatusError(error_msg, request=response.request, response=response)

                        logger.debug("Gemini streaming: Response started, reading chunks...")
                        async for line in response.aiter_lines():
                            if cancellation_token and cancellation_token.is_cancelled:
                                raise asyncio.CancelledError("Request cancelled during streaming")

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
                                    
                                    logger.debug(f"Gemini function call: {func_name}, has_thought_sig={thought_signature is not None}")
                                    
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
                                    if thought_signature:
                                        tool_call["extra_content"] = {
                                            "google": {
                                                "thought_signature": thought_signature
                                            }
                                        }
                                        # Also store directly for easier access
                                        tool_call["thought_signature"] = thought_signature
                                    
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

        last_exception = None
        for attempt in range(self.max_retries + 1):
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled before attempt")

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

                    # Build assistant message
                    assistant = {"role": "assistant", "content": ""}
                    tool_calls = []
                    text_parts = []

                    for part in parts:
                        if "text" in part:
                            text_parts.append(part["text"])
                        
                        if "functionCall" in part:
                            func_call = part["functionCall"]
                            func_name = func_call.get("name", "")
                            func_args = func_call.get("args", {})
                            
                            # Extract thoughtSignature (Gemini 3 Pro requirement)
                            thought_signature = part.get("thoughtSignature")
                            
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
                            if thought_signature:
                                tool_call["extra_content"] = {
                                    "google": {
                                        "thought_signature": thought_signature
                                    }
                                }
                                tool_call["thought_signature"] = thought_signature
                                logger.debug(f"Non-streaming: stored thought_signature for {func_name}")
                            
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
