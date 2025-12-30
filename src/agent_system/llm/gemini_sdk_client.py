"""
Gemini SDK Client - Using Official Google Gen AI SDK

Alternative implementation using the official google-genai SDK instead of raw HTTP.
This serves as a comparison to verify if the current HTTP-based implementation 
has issues with system instruction handling.

Key differences from gemini_client.py (HTTP-based):
- Uses official google.genai Client
- SDK handles message conversion internally
- SDK handles thought signatures automatically
- SDK supports automatic function calling

Feature parity with gemini_client.py:
- Thoughts/Thinking support
- Token tracking with cached tokens
- Streaming with deltas
- Tool calls with thought signatures
- Retry logic
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from google import genai
from google.genai import types

from agent_system.llm.models import ChatMessage
from agent_system.llm.clients import LLMClient
from agent_system.llm.retry_utils import parse_retry_delay, is_rate_limit_error
from agent_system.llm.gemini_utils import (
    convert_openai_messages_to_gemini,
    convert_openai_tools_to_gemini,
    clean_schema_for_gemini
)

logger = logging.getLogger(__name__)


class GeminiSDKClient(LLMClient):
    """Gemini client using official Google Gen AI SDK.
    
    Feature-complete alternative to GeminiClient (HTTP-based) with:
    - Full streaming support
    - Thoughts/Thinking integration
    - Token tracking with cached tokens
    - Tool calling with thought signatures
    - Retry logic with exponential backoff
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",  # Ignored, kept for compatibility
        context_window: int = 200000,
        request_timeout: int = 180,
        ssl_verify: bool | str = True,  # Ignored by SDK, kept for compatibility
        httpx_timeouts: dict | None = None,  # Ignored by SDK, kept for compatibility
        max_retries: int = 3,
        parallel_tool_calls: bool = True,  # Ignored, kept for compatibility
        include_thoughts: bool | None = None,
        thinking_budget: int | None = None,
        **extra_params
    ):
        """Initialize Gemini SDK client.
        
        Args:
            model: Model name (e.g., "gemini-2.5-flash", "gemini-3-pro-preview")
            api_key: Google AI API key
            base_url: Ignored (SDK handles endpoint)
            context_window: Maximum context window size
            request_timeout: Request timeout in seconds
            ssl_verify: Ignored by SDK
            httpx_timeouts: Ignored by SDK
            max_retries: Maximum retry attempts
            parallel_tool_calls: Ignored (SDK handles this)
            include_thoughts: Enable thought/reasoning output
            thinking_budget: Token budget for thinking
            **extra_params: Additional generation parameters (temperature, top_p, etc.)
        """
        self.model = model
        self.model_name = model  # For token tracking compatibility
        self.api_key = api_key
        self.context_window = context_window
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.extra_params = extra_params
        
        # Store include_thoughts in extra_params for consistency
        if include_thoughts is not None:
            self.extra_params["include_thoughts"] = include_thoughts
        
        # Store thinking_budget in extra_params for consistency
        if thinking_budget is not None:
            self.extra_params["thinking_budget"] = thinking_budget
        
        # Initialize the official client
        self._client = genai.Client(api_key=api_key)
        
        logger.info(
            f"Initialized GeminiSDKClient with model={model} "
            f"context_window={context_window} "
            f"include_thoughts={self.extra_params.get('include_thoughts')} "
            f"thinking_budget={self.extra_params.get('thinking_budget')}"
        )

    def _convert_messages_to_sdk(
        self, messages: List[ChatMessage]
    ) -> tuple[Optional[str], List[types.Content]]:
        """Convert ChatMessage list to SDK Content format.
        
        Uses shared conversion logic, then wraps in SDK types.
        
        Returns:
            (system_instruction, contents_list)
        """
        # Use shared conversion utility to get plain dicts
        system_instruction, dict_contents = convert_openai_messages_to_gemini(messages)
        
        # Convert dicts to SDK Content objects
        sdk_contents: List[types.Content] = []
        for content in dict_contents:
            role = content["role"]
            parts_list = content["parts"]
            
            # Convert each part to SDK Part
            sdk_parts = []
            for part_dict in parts_list:
                if "text" in part_dict:
                    sdk_parts.append(types.Part(text=part_dict["text"]))
                
                elif "functionCall" in part_dict:
                    fc = part_dict["functionCall"]
                    # NOTE: We intentionally do NOT restore thought_signature for historical calls
                    # Historical thought_signatures become invalid, causing MALFORMED_FUNCTION_CALL
                    sdk_parts.append(types.Part.from_function_call(
                        name=fc["name"],
                        args=fc["args"]
                    ))
                    logger.debug(f"[GeminiSDK] Created historical function call for {fc['name']} without thought_signature")
                
                elif "functionResponse" in part_dict:
                    fr = part_dict["functionResponse"]
                    sdk_parts.append(types.Part.from_function_response(
                        name=fr["name"],
                        response=fr["response"]
                    ))
                
                elif "inlineData" in part_dict:
                    inline = part_dict["inlineData"]
                    # Decode base64 string to bytes for SDK
                    import base64
                    data_bytes = base64.b64decode(inline["data"])
                    sdk_parts.append(types.Part(
                        inline_data=types.Blob(
                            mime_type=inline["mimeType"],
                            data=data_bytes
                        )
                    ))
            
            if sdk_parts:
                sdk_contents.append(types.Content(role=role, parts=sdk_parts))
        
        return system_instruction, sdk_contents

    def _convert_tools_to_sdk(self, tools: List[Dict]) -> Optional[types.Tool]:
        """Convert OpenAI tool schema to SDK Tool format.
        
        Uses shared conversion logic, then wraps in SDK types.
        """
        # Use shared conversion utility
        function_declarations_dicts = convert_openai_tools_to_gemini(tools)
        
        if not function_declarations_dicts:
            return None
        
        # Wrap in SDK types.Tool
        return types.Tool(function_declarations=function_declarations_dicts)

    def _clean_schema_for_sdk(self, schema: Dict) -> Dict:
        """Clean JSON schema for SDK compatibility.
        
        Delegates to shared utility with SDK-specific flattening enabled.
        """
        return clean_schema_for_gemini(schema, flatten_complex_schemas=True)

    def _build_generation_config(
        self, 
        system_instruction: Optional[str],
        sdk_tools: Optional[types.Tool]
    ) -> types.GenerateContentConfig:
        """Build generation config with all parameters."""
        config = types.GenerateContentConfig(
            temperature=self.extra_params.get("temperature", 1.0),
            top_p=self.extra_params.get("top_p", 0.95),
            top_k=self.extra_params.get("top_k", 40),
        )
        
        # Add tools if present
        if sdk_tools:
            config.tools = [sdk_tools]
            # Disable automatic function calling - we handle it ourselves
            config.automatic_function_calling = types.AutomaticFunctionCallingConfig(
                disable=True
            )
        
        # Add system instruction if present
        if system_instruction:
            config.system_instruction = system_instruction
            logger.debug(f"[GeminiSDK] System instruction set: {len(system_instruction)} chars")
        
        # Enable thinking if requested
        if self.extra_params.get("include_thoughts") is True:
            budget = self.extra_params.get("thinking_budget", 8192)
            config.thinking_config = types.ThinkingConfig(
                thinking_budget=budget,
                include_thoughts=True
            )
        
        return config

    def _extract_usage(self, usage_metadata) -> Dict[str, Any]:
        """Extract usage info in OpenAI-compatible format."""
        if not usage_metadata:
            return {}
        
        usage = {
            "prompt_tokens": getattr(usage_metadata, 'prompt_token_count', 0),
            "completion_tokens": getattr(usage_metadata, 'candidates_token_count', 0),
            "total_tokens": getattr(usage_metadata, 'total_token_count', 0),
        }
        
        # Extract cached tokens (implicit caching for Gemini 2.5+)
        cached_tokens = getattr(usage_metadata, 'cached_content_token_count', 0)
        if cached_tokens and cached_tokens > 0:
            # Store in OpenAI-compatible format: prompt_tokens_details.cached_tokens
            usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
        
        return usage

    async def chat_tools_streaming(
        self,
        messages: List[ChatMessage],
        tools: List[Dict],
        cancellation_token=None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream chat with tools using official SDK.
        
        Yields events in format compatible with gemini_client.py:
        - content_delta: Text content chunks with delta and accumulated
        - tool_call_delta: Tool call information
        - final: Final accumulated result
        """
        system_instruction, contents = self._convert_messages_to_sdk(messages)
        sdk_tools = self._convert_tools_to_sdk(tools)
        
        # Accumulators
        accumulated_content = []  # Only non-thought content
        accumulated_thoughts = []  # Thought summaries
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
            generation_config = self._build_generation_config(system_instruction, sdk_tools)
            if attempt > 0 and got_malformed_function_call:
                logger.info(f"[GeminiSDK] Retry #{attempt} with forced function calling (mode=ANY)")
                generation_config["tool_config"] = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="ANY")
                )
            
            try:
                logger.debug(f"[GeminiSDK] Starting streaming request to {self.model} (attempt {attempt + 1})")
                logger.debug(f"[GeminiSDK] Contents count: {len(contents)}")
                logger.debug(f"[GeminiSDK] Has system instruction: {system_instruction is not None}")
                logger.debug(f"[GeminiSDK] Has tools: {sdk_tools is not None}")
                
                # Use async streaming
                async for chunk in await self._client.aio.models.generate_content_stream(
                    model=self.model,
                    contents=contents,
                    config=generation_config,
                ):
                    if cancellation_token and cancellation_token.is_cancelled:
                        raise asyncio.CancelledError("Request cancelled during streaming")
                    
                    # Process chunk
                    if not chunk.candidates:
                        continue
                    
                    candidate = chunk.candidates[0]
                    
                    # Always log finish_reason (critical for debugging MALFORMED_FUNCTION_CALL)
                    if hasattr(candidate, 'finish_reason') and candidate.finish_reason:
                        finish_reason_str = str(candidate.finish_reason)
                        finish_msg = getattr(candidate, 'finish_message', None)
                        
                        # Detect MALFORMED_FUNCTION_CALL for auto-retry
                        if 'MALFORMED' in finish_reason_str:
                            got_malformed_function_call = True
                            # Log contents to debug what was sent
                            contents_json = json.dumps([
                                {
                                    "role": c.role,
                                    "parts": [
                                        {
                                            "text": p.text if hasattr(p, 'text') and p.text else None,
                                            "function_call": {
                                                "name": p.function_call.name,
                                                "args": dict(p.function_call.args) if p.function_call.args else {}
                                            } if hasattr(p, 'function_call') and p.function_call else None,
                                            "function_response": {
                                                "name": p.function_response.name if hasattr(p.function_response, 'name') else None,
                                                "response": p.function_response.response if hasattr(p.function_response, 'response') else None
                                            } if hasattr(p, 'function_response') and p.function_response else None
                                        }
                                        for p in c.parts
                                    ]
                                }
                                for c in contents
                            ], indent=2, default=str)
                            logger.warning(f"[GeminiSDK] MALFORMED_FUNCTION_CALL detected. Contents sent:\n{contents_json}")
                        
                        # Log at WARNING level if there's a message or if it's a problematic finish_reason
                        if finish_msg or 'MALFORMED' in finish_reason_str or 'ERROR' in finish_reason_str:
                            msg = f"[GeminiSDK] finish_reason: {candidate.finish_reason}"
                            if finish_msg:
                                msg += f", finish_message: {finish_msg}"
                            logger.warning(msg)
                        else:
                            # Normal STOP etc at DEBUG level
                            logger.debug(f"[GeminiSDK] finish_reason: {candidate.finish_reason}")
                    
                    if not candidate.content or not candidate.content.parts:
                        continue
                    
                    for part in candidate.content.parts:
                        # Handle thought parts
                        if hasattr(part, 'thought') and part.thought:
                            text = part.text if hasattr(part, 'text') else ""
                            if text:
                                accumulated_thoughts.append(text)
                                logger.debug(f"[GeminiSDK] Thought delta: {len(text)} chars")
                                
                                # Stream thoughts as content_delta (like HTTP Gemini client)
                                # Thoughts appear before regular content in the accumulated text
                                all_text = "".join(accumulated_thoughts) + "".join(accumulated_content)
                                yield {
                                    "type": "content_delta",
                                    "delta": text,
                                    "accumulated": all_text
                                }
                        
                        # Handle function calls
                        elif hasattr(part, 'function_call') and part.function_call:
                            func_call = part.function_call
                            tool_call_id = f"call_{uuid.uuid4().hex[:16]}"
                            
                            # Extract thought signature if present on this part
                            thought_signature = None
                            if hasattr(part, 'thought_signature') and part.thought_signature:
                                # Convert bytes to base64 for JSON serialization
                                thought_signature = base64.b64encode(part.thought_signature).decode('utf-8')
                                # Store as the first thought_signature for this turn
                                # (parallel FC: only first functionCall has the signature)
                                if first_thought_signature is None:
                                    first_thought_signature = thought_signature
                                    logger.debug(f"[GeminiSDK] Captured first thoughtSignature from {func_call.name}")
                            
                            # For parallel function calls: use first_thought_signature if this part has none
                            effective_signature = thought_signature or first_thought_signature
                            
                            tool_call = {
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": func_call.name,
                                    "arguments": json.dumps(dict(func_call.args) if func_call.args else {})
                                }
                            }
                            
                            # Store thought signature for round-trip (Gemini 3 Pro requirement)
                            # Use effective_signature to ensure all parallel calls get the signature
                            if effective_signature:
                                tool_call["extra_content"] = {
                                    "google": {"thought_signature": effective_signature}
                                }
                                tool_call["thought_signature"] = effective_signature
                                if thought_signature:
                                    logger.debug(f"[GeminiSDK] Including thoughtSignature for {func_call.name} (original)")
                                else:
                                    logger.debug(f"[GeminiSDK] Including thoughtSignature for {func_call.name} (propagated from first)")
                            
                            accumulated_tool_calls[tool_call_id] = tool_call
                            
                            logger.debug(f"[GeminiSDK] Function call: {func_call.name}")
                            yield {
                                "type": "tool_call_delta",
                                "index": len(accumulated_tool_calls) - 1,
                                "delta": {"function": {"name": func_call.name}},
                                "accumulated": tool_call
                            }
                        
                        # Handle text parts (non-thought)
                        elif hasattr(part, 'text') and part.text:
                            text_delta = part.text
                            accumulated_content.append(text_delta)
                            
                            logger.debug(f"[GeminiSDK] Text delta: {len(text_delta)} chars")
                            
                            # Stream with thoughts + content combined for display
                            all_text = "".join(accumulated_thoughts) + "".join(accumulated_content)
                            yield {
                                "type": "content_delta",
                                "delta": text_delta,
                                "accumulated": all_text
                            }
                    
                    # Extract usage from chunks
                    if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
                        accumulated_usage = self._extract_usage(chunk.usage_metadata)
                
                # Stream finished successfully
                logger.debug(
                    f"[GeminiSDK] Streaming complete: {len(accumulated_content)} content parts, "
                    f"{len(accumulated_tool_calls)} tool calls"
                )
                
                # Check for MALFORMED_FUNCTION_CALL with empty response - auto-retry
                if got_malformed_function_call and not accumulated_content and not accumulated_tool_calls:
                    if attempt < self.max_retries:
                        wait_time = 1.0 + attempt  # 1s, 2s, 3s
                        logger.warning(
                            f"[GeminiSDK] MALFORMED_FUNCTION_CALL with empty response. "
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
                            "[GeminiSDK] MALFORMED_FUNCTION_CALL persisted after all retries. "
                            "This may indicate invalid tool schema or complex function call arguments."
                        )
                
                # Warn if response is completely empty (MALFORMED_FUNCTION_CALL indicator)
                if not accumulated_content and not accumulated_tool_calls:
                    logger.warning(
                        "[GeminiSDK] Empty response from Gemini (no content, no tool calls). "
                        "This typically indicates MALFORMED_FUNCTION_CALL or invalid function response format."
                    )
                
                # Build final result in same format as gemini_client.py
                assistant = {
                    "role": "assistant",
                    "content": "".join(accumulated_content) if accumulated_content else ""
                }
                
                if accumulated_tool_calls:
                    assistant["tool_calls"] = list(accumulated_tool_calls.values())
                
                final_result = {"assistant": assistant}
                if accumulated_usage:
                    final_result["usage"] = accumulated_usage
                
                logger.debug("[GeminiSDK] Yielding final result")
                yield {"type": "final", **final_result}
                return  # Success
                
            except asyncio.CancelledError:
                logger.info("[GeminiSDK] Request cancelled by user")
                raise
            
            except Exception as e:
                last_exception = e
                error_str = str(e)
                
                # Check if this is a rate limit error
                is_rate_limit = is_rate_limit_error(e)
                
                if is_rate_limit:
                    # Parse retry delay from error message, default to 60s for rate limits
                    parsed_delay = parse_retry_delay(error_str)
                    wait_time = parsed_delay if parsed_delay else 60.0
                    # Add small buffer to parsed delay
                    if parsed_delay:
                        wait_time = parsed_delay + 2.0
                    
                    logger.warning(
                        f"[GeminiSDK] Rate limit hit (429). Waiting {wait_time:.1f}s before retry "
                        f"(attempt {attempt + 1}/{self.max_retries + 1})"
                    )
                    await asyncio.sleep(wait_time)
                    # Reset accumulators for retry
                    accumulated_content = []
                    accumulated_thoughts = []
                    accumulated_tool_calls = {}
                    accumulated_usage = None
                    first_thought_signature = None
                    # Keep got_malformed_function_call for mode=ANY if it was set
                    continue
                
                logger.error(f"[GeminiSDK] Streaming error: {e}", exc_info=True)
                
                if attempt < self.max_retries:
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"[GeminiSDK] Retrying in {wait_time}s "
                        f"(attempt {attempt + 1}/{self.max_retries + 1})"
                    )
                    await asyncio.sleep(wait_time)
                    # Reset accumulators for retry
                    accumulated_content = []
                    accumulated_thoughts = []
                    accumulated_tool_calls = {}
                    accumulated_usage = None
                    continue
                else:
                    raise Exception(f"Gemini SDK streaming failed: {str(e)}") from e
        
        # If we get here, all retries failed
        if last_exception:
            raise Exception(
                f"Gemini SDK streaming failed after {self.max_retries + 1} attempts"
            ) from last_exception
        raise Exception(f"Gemini SDK streaming failed after {self.max_retries + 1} attempts")

    async def chat_tools(
        self,
        messages: List[ChatMessage],
        tools: List[Dict],
        cancellation_token=None
    ) -> Dict[str, Any]:
        """Non-streaming chat with tools.
        
        Returns complete result in format compatible with gemini_client.py:
        {"assistant": {...}, "usage": {...}}
        """
        system_instruction, contents = self._convert_messages_to_sdk(messages)
        sdk_tools = self._convert_tools_to_sdk(tools)
        generation_config = self._build_generation_config(system_instruction, sdk_tools)
        
        last_exception = None
        for attempt in range(self.max_retries + 1):
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled before attempt")
            
            try:
                logger.debug(f"[GeminiSDK] Starting non-streaming request to {self.model}")
                
                response = await self._client.aio.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=generation_config,
                )
                
                # Extract response
                if not response.candidates:
                    return {
                        "assistant": {"role": "assistant", "content": ""},
                        "usage": {}
                    }
                
                candidate = response.candidates[0]
                if not candidate.content or not candidate.content.parts:
                    return {
                        "assistant": {"role": "assistant", "content": ""},
                        "usage": self._extract_usage(getattr(response, 'usage_metadata', None))
                    }
                
                # Build assistant message
                assistant = {"role": "assistant", "content": ""}
                tool_calls = []
                text_parts = []
                # For parallel function calls: track first thought_signature
                first_thought_signature = None
                
                for part in candidate.content.parts:
                    # Handle text
                    if hasattr(part, 'text') and part.text:
                        # Skip thought parts for content
                        if not (hasattr(part, 'thought') and part.thought):
                            text_parts.append(part.text)
                    
                    # Handle function calls
                    if hasattr(part, 'function_call') and part.function_call:
                        func_call = part.function_call
                        tool_call_id = f"call_{uuid.uuid4().hex[:16]}"
                        
                        tool_call = {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": func_call.name,
                                "arguments": json.dumps(dict(func_call.args) if func_call.args else {})
                            }
                        }
                        
                        # Extract thought signature if present on this part
                        thought_sig_b64 = None
                        if hasattr(part, 'thought_signature') and part.thought_signature:
                            # Convert bytes to base64 for JSON serialization
                            thought_sig_b64 = base64.b64encode(part.thought_signature).decode('utf-8')
                            # Store as first thought_signature for parallel FC
                            if first_thought_signature is None:
                                first_thought_signature = thought_sig_b64
                                logger.debug(f"[GeminiSDK] Non-streaming: captured first thought_signature from {func_call.name}")
                        
                        # For parallel function calls: use first_thought_signature if this part has none
                        effective_signature = thought_sig_b64 or first_thought_signature
                        
                        if effective_signature:
                            tool_call["extra_content"] = {
                                "google": {"thought_signature": effective_signature}
                            }
                            tool_call["thought_signature"] = effective_signature
                            if thought_sig_b64:
                                logger.debug(f"[GeminiSDK] Non-streaming: stored thought_signature for {func_call.name} (original)")
                            else:
                                logger.debug(f"[GeminiSDK] Non-streaming: stored thought_signature for {func_call.name} (propagated)")
                        
                        tool_calls.append(tool_call)
                
                assistant["content"] = "".join(text_parts)
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                
                # Extract usage
                usage = self._extract_usage(getattr(response, 'usage_metadata', None))
                
                return {"assistant": assistant, "usage": usage}
                
            except asyncio.CancelledError:
                logger.info("[GeminiSDK] Request cancelled by user")
                raise
            
            except Exception as e:
                last_exception = e
                error_str = str(e)
                
                # Check if this is a rate limit error
                is_rate_limit = is_rate_limit_error(e)
                
                if is_rate_limit:
                    # Parse retry delay from error message, default to 60s for rate limits
                    parsed_delay = parse_retry_delay(error_str)
                    wait_time = parsed_delay if parsed_delay else 60.0
                    # Add small buffer to parsed delay
                    if parsed_delay:
                        wait_time = parsed_delay + 2.0
                    
                    logger.warning(
                        f"[GeminiSDK] Rate limit hit (429). Waiting {wait_time:.1f}s before retry "
                        f"(attempt {attempt + 1}/{self.max_retries + 1})"
                    )
                    await asyncio.sleep(wait_time)
                    continue
                
                logger.error(f"[GeminiSDK] Request error: {e}", exc_info=True)
                
                if attempt < self.max_retries:
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"[GeminiSDK] Retrying in {wait_time}s "
                        f"(attempt {attempt + 1}/{self.max_retries + 1})"
                    )
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"Gemini SDK request failed: {str(e)}") from e
        
        # If we get here, all retries failed
        if last_exception:
            raise Exception(
                f"Gemini SDK request failed after {self.max_retries + 1} attempts"
            ) from last_exception
        raise Exception(f"Gemini SDK request failed after {self.max_retries + 1} attempts")

    async def chat(self, messages: List[ChatMessage], cancellation_token=None) -> str:
        """Simple chat without tools."""
        result = await self.chat_tools(messages, [], cancellation_token)
        return result["assistant"]["content"]

    def supports_streaming(self) -> bool:
        """GeminiSDKClient supports true streaming."""
        return True


# Factory function for easy switching
def create_gemini_client(
    api_key: str,
    model: str = "gemini-2.5-flash",
    use_sdk: bool = False,
    **kwargs
):
    """Create a Gemini client (HTTP or SDK based).
    
    Args:
        api_key: API key
        model: Model name
        use_sdk: If True, use official SDK client; if False, use HTTP client
        **kwargs: Additional client parameters
    
    Returns:
        GeminiSDKClient or GeminiClient instance
    """
    if use_sdk:
        return GeminiSDKClient(api_key=api_key, model=model, **kwargs)
    else:
        from agent_system.llm.gemini_client import GeminiClient
        return GeminiClient(api_key=api_key, model=model, **kwargs)
