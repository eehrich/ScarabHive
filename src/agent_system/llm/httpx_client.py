"""
HTTPX-based LLM client with superior cancellation, timeout, and error handling.

This client uses HTTPX directly for better async control compared to the official
OpenAI client which has known hanging/timeout issues.
"""

import asyncio
import json
import logging
from typing import Any, Optional
from dataclasses import dataclass

import httpx
import ssl

from agent_system.llm.clients import LLMClient
from agent_system.core.cancellation import CancellationToken

logger = logging.getLogger(__name__)


@dataclass
class HTTPXTimeoutConfig:
    """Fine-grained timeout configuration for HTTPX client."""
    connect: float = 10.0      # Connection establishment timeout
    read: float = 180.0        # Read timeout (waiting for response data)
    write: float = 10.0        # Write timeout (sending request data)
    pool: float = 5.0          # Pool timeout (getting connection from pool)


class HTTPXOpenAIClient(LLMClient):
    """
    HTTPX-based OpenAI API client with superior async handling.

    Advantages over official OpenAI client:
    - Native asyncio.CancelledError support (no polling required)
    - Fine-grained timeout control (connect, read, write, pool)
    - Direct HTTP error handling without exception wrapping
    - Better connection management and retry logic
    - Cleaner cancellation without complex task management
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_config: Optional[HTTPXTimeoutConfig] = None,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        verify: Optional[bool] = None,
        context_window: Optional[int] = None,
        capabilities: Optional[dict] = None,
        **extra_params
    ):
        # LLMClient doesn't have __init__, so no super() call needed
        self.model = model
        self.provider = "openai"
        self.context_window = context_window
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_config = timeout_config or HTTPXTimeoutConfig()
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.verify = verify
        self.extra_params = extra_params
        self.capabilities = capabilities or {}

        # Validate API type - HTTPX client only supports chat_completions
        if self.capabilities and hasattr(self.capabilities, 'default_api_type'):
            api_type = self.capabilities.default_api_type
            # Extract value from enum if it's an enum
            if hasattr(api_type, 'value'):
                api_type = api_type.value
            else:
                api_type = str(api_type) if api_type else 'chat_completions'

            if api_type != 'chat_completions':
                raise NotImplementedError(
                    f"HTTPX client only supports 'chat_completions' API. "
                    f"Requested API type: '{api_type}'. "
                    f"For Realtime API, use provider='openai' instead of 'openai_httpx'. "
                    f"Current model: {self.model}"
                )

        # Normalize verify: when explicitly False, create an SSLContext that disables
        # certificate verification. This is more robust across httpx/httpcore
        # backends and when using proxies that perform TLS interception.
        if self.verify is False:
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                self._verify = ctx
            except Exception:
                # Fall back to boolean False if SSLContext can't be created for any reason
                self._verify = False
        else:
            # Keep None or True as-is (None means httpx default behavior)
            self._verify = self.verify

        logger.debug(f"HTTPXOpenAIClient initialized model={model} base_url={base_url} verify={self._verify}")

        # Create timeout object for HTTPX
        self._timeout = httpx.Timeout(
            connect=self.timeout_config.connect,
            read=self.timeout_config.read,
            write=self.timeout_config.write,
            pool=self.timeout_config.pool
        )

        # HTTPX client will be created per request to ensure proper cleanup
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "AgentSystem-HTTPX/1.0"
        }

    async def chat(
        self,
        messages: list,
        cancellation_token: Optional[CancellationToken] = None
    ) -> str:
        """Send chat completion request without tools."""
        result = await self._make_request(messages, tools=[], cancellation_token=cancellation_token)
        return result.get("assistant", {}).get("content", "")

    async def chat_tools(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None
    ) -> dict:
        """Send chat completion request with tools."""
        return await self._make_request(messages, tools=tools, cancellation_token=cancellation_token)

    async def chat_tools_streaming(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None
    ):
        """Stream chat completion request with tools.

        Yields:
            dict: Streaming chunks with different types:
                {"type": "content_delta", "delta": str, "accumulated": str}
                {"type": "tool_call_delta", "index": int, "delta": dict}
                {"type": "final", "assistant": dict}
        """
        async for chunk in self._make_request_streaming(messages, tools=tools, cancellation_token=cancellation_token):
            yield chunk

    def supports_streaming(self) -> bool:
        """Check if this client supports streaming based on model capabilities."""
        # Check if capabilities explicitly disable streaming (handle both dict and object)
        if self.capabilities:
            if isinstance(self.capabilities, dict):
                return self.capabilities.get('streaming', True)
            elif hasattr(self.capabilities, 'streaming'):
                return self.capabilities.streaming
        return True  # Default to True if capabilities not set

    async def _make_request(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None
    ) -> dict:
        """Make the actual HTTP request with proper cancellation and error handling."""

        # Check if streaming is disabled in capabilities (handle both dict and object)
        streaming_enabled = True  # Default
        if self.capabilities:
            if isinstance(self.capabilities, dict):
                streaming_enabled = self.capabilities.get('streaming', True)
            elif hasattr(self.capabilities, 'streaming'):
                streaming_enabled = self.capabilities.streaming

        logger.debug(f"_make_request: streaming_enabled={streaming_enabled}, capabilities type={type(self.capabilities)}")

        if not streaming_enabled:
            # Use non-streaming request
            logger.debug("Using non-streaming request path")
            return await self._make_request_non_streaming(messages, tools, cancellation_token)

        # Use streaming request (default behavior)
        logger.debug("Using streaming request path")
        final_result = None
        async for chunk in self._make_request_streaming(messages, tools, cancellation_token):
            if chunk.get("type") == "final":
                # Extract all fields from final chunk (assistant, usage, etc.)
                final_result = {k: v for k, v in chunk.items() if k != "type"}
                break

        return final_result if final_result else {"assistant": {"role": "assistant", "content": ""}}

    async def _make_request_non_streaming(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None
    ) -> dict:
        """Make non-streaming HTTP POST request for models that don't support streaming.

        Returns:
            dict: Response with 'assistant' key containing the assistant message
        """
        logger.debug(f"_make_request_non_streaming called for model {self.model}")

        # Build request payload - convert ChatMessage objects to dicts
        message_dicts = []
        for msg in messages:
            if hasattr(msg, 'model_dump'):
                message_dicts.append(msg.model_dump(exclude_none=True, mode='json'))
            elif isinstance(msg, dict):
                message_dicts.append(msg)
            else:
                message_dicts.append(dict(msg))

        payload = {
            "model": self.model,
            "messages": message_dicts,
            "stream": False,  # ⚡ Disable streaming
            **self.extra_params
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        url = f"{self.base_url}/chat/completions"

        # Retry logic with exponential backoff
        last_exception = None
        for attempt in range(self.max_retries + 1):
            # Check cancellation before each attempt
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")

            try:
                # Create fresh client for each request
                client_kwargs: dict[str, Any] = {"timeout": self._timeout}
                if getattr(self, "_verify", None) is not None:
                    client_kwargs["verify"] = self._verify

                async with httpx.AsyncClient(**client_kwargs) as client:
                    logger.debug(f"HTTPX non-streaming request attempt {attempt + 1}/{self.max_retries + 1} to {url}")

                    # Make regular POST request (not streaming)
                    response = await client.post(url=url, headers=self._headers, json=payload)

                    # Handle rate limiting
                    if response.status_code == 429 and attempt < self.max_retries:
                        retry_after = self._parse_retry_after(response.headers.get("retry-after"))
                        backoff_time = retry_after or (self.retry_backoff * (2 ** attempt))
                        logger.warning(f"Rate limited (429), retrying in {backoff_time}s")
                        await asyncio.sleep(backoff_time)
                        continue

                    # Check for HTTP errors
                    if response.status_code >= 400:
                        error_text = response.text[:200] if response.text else ""
                        error_msg = f"HTTP {response.status_code}: {error_text}"
                        logger.error(f"HTTPX non-streaming request failed: {error_msg}")
                        raise httpx.HTTPStatusError(error_msg, request=response.request, response=response)

                    # Parse successful response
                    response_data = response.json()

                    # Use centralized response formatting (handles usage, tool_calls, etc.)
                    return self._format_response(response_data)

            except httpx.HTTPStatusError:
                raise  # Re-raise HTTP errors immediately
            except asyncio.CancelledError:
                raise  # Re-raise cancellation
            except Exception as e:
                last_exception = e
                if attempt < self.max_retries:
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Request failed (attempt {attempt + 1}/{self.max_retries + 1}): {e}. Retrying in {backoff_time}s")
                    await asyncio.sleep(backoff_time)
                else:
                    logger.error(f"Request failed after {self.max_retries + 1} attempts")
                    raise Exception(f"HTTP request failed after {self.max_retries + 1} attempts: {last_exception}") from last_exception

        # Should never reach here
        raise Exception(f"HTTP request failed after {self.max_retries + 1} attempts: {last_exception}") from last_exception

    async def _make_request_streaming(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None
    ):
        """Make streaming HTTP request that yields chunks.

        Yields:
            dict: Chunks with types: content_delta, tool_call_delta, final
        """

        # Build request payload - convert ChatMessage objects to dicts
        message_dicts = []
        for msg in messages:
            if hasattr(msg, 'model_dump'):
                # ChatMessage object - convert to dict, exclude None values for API compatibility
                message_dicts.append(msg.model_dump(exclude_none=True, mode='json'))
            elif isinstance(msg, dict):
                # Already a dict
                message_dicts.append(msg)
            else:
                # Fallback - try to convert to dict
                message_dicts.append(dict(msg))

        payload = {
            "model": self.model,
            "messages": message_dicts,
            "stream": True,  # ⚡ Enable streaming
            "stream_options": {"include_usage": True},  # Request usage stats in stream
            **self.extra_params
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        url = f"{self.base_url}/chat/completions"

        # Accumulators for building complete response
        accumulated_content = []
        accumulated_tool_calls = {}  # index -> tool call data
        accumulated_usage = None  # usage information from final chunk

        # Retry logic with exponential backoff
        last_exception = None
        for attempt in range(self.max_retries + 1):
            # Check cancellation before each attempt
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")

            try:
                # Create fresh client for each request to avoid connection issues
                client_kwargs: dict[str, Any] = {"timeout": self._timeout}
                # Only include verify if explicitly configured (None means use httpx default)
                if getattr(self, "_verify", None) is not None:
                    client_kwargs["verify"] = self._verify

                async with httpx.AsyncClient(**client_kwargs) as client:
                    logger.debug(f"HTTPX streaming request attempt {attempt + 1}/{self.max_retries + 1} to {url}")

                    # Make streaming request
                    async with client.stream("POST", url=url, headers=self._headers, json=payload) as response:
                        # Check status code (don't use raise_for_status() - it tries to read the body)
                        if response.status_code == 429 and attempt < self.max_retries:
                            # Rate limit - retry with backoff
                            retry_after = self._parse_retry_after(response.headers.get("retry-after"))
                            backoff_time = retry_after or (self.retry_backoff * (2 ** attempt))

                            logger.warning(f"Rate limited (429), retrying in {backoff_time}s")
                            await asyncio.sleep(backoff_time)
                            continue

                        # Check for errors without reading body (streaming response)
                        if response.status_code >= 400:
                            # Read the error body for streaming responses
                            error_body = await response.aread()
                            error_text = error_body.decode('utf-8', errors='replace')
                            error_msg = f"HTTP {response.status_code}: {error_text[:200]}"
                            logger.error(f"HTTPX streaming request failed: {error_msg}")
                            raise httpx.HTTPStatusError(error_msg, request=response.request, response=response)

                        # Parse SSE stream
                        async for line in response.aiter_lines():
                            if cancellation_token and cancellation_token.is_cancelled:
                                raise asyncio.CancelledError("Request cancelled during streaming")

                            if not line or not line.startswith("data: "):
                                continue

                            data = line[6:]  # Remove "data: " prefix

                            if data == "[DONE]":
                                # Stream finished - yield final result
                                assistant = {
                                    "role": "assistant",
                                    "content": "".join(accumulated_content) if accumulated_content else ""
                                }

                                # Add tool calls if any
                                if accumulated_tool_calls:
                                    tool_calls_list = [accumulated_tool_calls[idx] for idx in sorted(accumulated_tool_calls.keys())]
                                    assistant["tool_calls"] = tool_calls_list

                                final_result = {"assistant": assistant}

                                # Add usage if available
                                if accumulated_usage:
                                    final_result["usage"] = accumulated_usage

                                yield {"type": "final", **final_result}
                                return  # Success - exit retry loop

                            try:
                                chunk_data = json.loads(data)
                            except Exception:
                                logger.debug(f"Failed to parse chunk data: {data[:100]}")
                                continue

                            # Track usage if available in chunk
                            if "usage" in chunk_data:
                                accumulated_usage = chunk_data["usage"]

                            # Process chunk
                            choices = chunk_data.get("choices", [])
                            if not choices:
                                continue

                            delta = choices[0].get("delta", {})

                            # Handle content delta
                            if "content" in delta and delta["content"]:
                                accumulated_content.append(delta["content"])
                                yield {
                                    "type": "content_delta",
                                    "delta": delta["content"],
                                    "accumulated": "".join(accumulated_content)
                                }

                            # Handle tool call deltas
                            if "tool_calls" in delta:
                                for tc_delta in delta["tool_calls"]:
                                    index = tc_delta.get("index", 0)

                                    # Initialize tool call buffer if needed
                                    if index not in accumulated_tool_calls:
                                        accumulated_tool_calls[index] = {
                                            "id": "",
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""}
                                        }

                                    # Accumulate deltas
                                    if "id" in tc_delta:
                                        accumulated_tool_calls[index]["id"] = tc_delta["id"]

                                    if "function" in tc_delta:
                                        func_delta = tc_delta["function"]
                                        if "name" in func_delta:
                                            accumulated_tool_calls[index]["function"]["name"] += func_delta["name"]
                                        if "arguments" in func_delta:
                                            accumulated_tool_calls[index]["function"]["arguments"] += func_delta["arguments"]

                                    # Yield delta with accumulated state
                                    yield {
                                        "type": "tool_call_delta",
                                        "index": index,
                                        "delta": tc_delta,
                                        "accumulated": accumulated_tool_calls[index]
                                    }

            except asyncio.CancelledError:
                # Re-raise cancellation without wrapping
                logger.info("HTTPX streaming request cancelled by user")
                raise

            except httpx.TimeoutException as e:
                last_exception = e
                if attempt < self.max_retries:
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Request timeout, retrying in {backoff_time}s: {e}")
                    await asyncio.sleep(backoff_time)
                    continue
                else:
                    logger.error(f"Request timed out after {self.max_retries + 1} attempts: {e}")
                    raise Exception(f"Request timed out: {e}") from e

            except httpx.HTTPStatusError as e:
                last_exception = e
                if e.response.status_code >= 500 and attempt < self.max_retries:
                    # Server error - retry
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Server error {e.response.status_code}, retrying in {backoff_time}s")
                    await asyncio.sleep(backoff_time)
                    continue
                else:
                    # Client error or max retries exceeded
                    # Error message already in exception (we read it before raising in streaming mode)
                    error_msg = str(e)
                    logger.error(f"HTTP error (streaming): {error_msg}")
                    raise Exception(error_msg) from e

            except (httpx.NetworkError, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                last_exception = e
                if attempt < self.max_retries:
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Network/protocol error (stream interrupted), retrying in {backoff_time}s: {e}")
                    await asyncio.sleep(backoff_time)
                    continue
                else:
                    logger.error(f"Network/protocol error after {self.max_retries + 1} attempts: {e}")
                    raise Exception(f"Network/protocol error: {e}") from e

        # Should never reach here, but just in case
        raise Exception(f"Request failed after {self.max_retries + 1} attempts") from last_exception

    async def _make_request_old_nonstreaming(
        self,
        messages: list,
        tools: list,
        cancellation_token: Optional[CancellationToken] = None
    ) -> dict:
        """OLD non-streaming version - kept for reference, will be removed."""

        # Build request payload - convert ChatMessage objects to dicts
        message_dicts = []
        for msg in messages:
            if hasattr(msg, 'model_dump'):
                # ChatMessage object - convert to dict, exclude None values for API compatibility
                message_dicts.append(msg.model_dump(exclude_none=True, mode='json'))
            elif isinstance(msg, dict):
                # Already a dict
                message_dicts.append(msg)
            else:
                # Fallback - try to convert to dict
                message_dicts.append(dict(msg))

        payload = {
            "model": self.model,
            "messages": message_dicts,
            **self.extra_params
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        url = f"{self.base_url}/chat/completions"

        # Retry logic with exponential backoff
        last_exception = None
        for attempt in range(self.max_retries + 1):
            # Check cancellation before each attempt
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError("Request cancelled by user")

            try:
                # Create fresh client for each request to avoid connection issues
                client_kwargs: dict[str, Any] = {"timeout": self._timeout}
                # Only include verify if explicitly configured (None means use httpx default)
                if getattr(self, "_verify", None) is not None:
                    client_kwargs["verify"] = self._verify
                async with httpx.AsyncClient(**client_kwargs) as client:
                    logger.debug(f"HTTPX request attempt {attempt + 1}/{self.max_retries + 1} to {url}")

                    # Make request - this will raise CancelledError naturally if cancelled
                    response = await client.post(
                        url=url,
                        headers=self._headers,
                        json=payload
                    )

                    # Check for HTTP errors
                    if response.status_code == 429 and attempt < self.max_retries:
                        # Rate limit - retry with backoff
                        retry_after = self._parse_retry_after(response.headers.get("retry-after"))
                        backoff_time = retry_after or (self.retry_backoff * (2 ** attempt))

                        logger.warning(f"Rate limited (429), retrying in {backoff_time}s")
                        await asyncio.sleep(backoff_time)
                        continue

                    response.raise_for_status()

                    # Parse response
                    response_data = response.json()
                    return self._format_response(response_data)

            except asyncio.CancelledError:
                # Re-raise cancellation without wrapping
                logger.info("HTTPX request cancelled by user")
                raise

            except httpx.TimeoutException as e:
                last_exception = e
                if attempt < self.max_retries:
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Request timeout, retrying in {backoff_time}s: {e}")
                    await asyncio.sleep(backoff_time)
                    continue
                else:
                    logger.error(f"Request timed out after {self.max_retries + 1} attempts: {e}")
                    raise Exception(f"Request timed out: {e}") from e

            except httpx.HTTPStatusError as e:
                last_exception = e
                if e.response.status_code >= 500 and attempt < self.max_retries:
                    # Server error - retry
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Server error {e.response.status_code}, retrying in {backoff_time}s")
                    await asyncio.sleep(backoff_time)
                    continue
                else:
                    # Client error or max retries exceeded
                    error_detail = self._parse_error_response(e.response)
                    logger.error(f"HTTP error {e.response.status_code}: {error_detail}")
                    raise Exception(f"HTTP {e.response.status_code}: {error_detail}") from e

            except (httpx.NetworkError, httpx.ConnectError) as e:
                last_exception = e
                if attempt < self.max_retries:
                    backoff_time = self.retry_backoff * (2 ** attempt)
                    logger.warning(f"Network error, retrying in {backoff_time}s: {e}")
                    await asyncio.sleep(backoff_time)
                    continue
                else:
                    logger.error(f"Network error after {self.max_retries + 1} attempts: {e}")
                    raise Exception(f"Network error: {e}") from e

        # Should never reach here, but just in case
        raise Exception(f"Request failed after {self.max_retries + 1} attempts") from last_exception

    def _parse_retry_after(self, retry_after: Optional[str]) -> Optional[float]:
        """Parse Retry-After header value."""
        if not retry_after:
            return None

        try:
            # Can be seconds or HTTP date, we only handle seconds for simplicity
            return float(retry_after)
        except ValueError:
            return None

    def _parse_error_response(self, response: httpx.Response) -> str:
        """Extract error message from HTTP error response."""
        try:
            error_data = response.json()
            if "error" in error_data:
                error_info = error_data["error"]
                if isinstance(error_info, dict):
                    return error_info.get("message", f"HTTP {response.status_code}")
                else:
                    return str(error_info)
        except Exception:
            pass

        return f"HTTP {response.status_code}: {response.text[:200]}"

    def _format_response(self, response_data: dict) -> dict:
        """Format OpenAI API response to our standard format."""
        try:
            logger.debug(f"Formatting response_data keys: {list(response_data.keys())}")

            choices = response_data.get("choices", [])
            if not choices:
                return {"assistant": {"role": "assistant", "content": ""}}

            choice = choices[0]
            message = choice.get("message", {})

            # Build assistant response
            assistant = {
                "role": "assistant",
                "content": message.get("content", "") or ""
            }

            # Add tool calls if present
            tool_calls = message.get("tool_calls")
            if tool_calls:
                assistant["tool_calls"] = tool_calls

            # Track usage if available
            usage = response_data.get("usage", {})
            logger.debug(f"Extracted usage from response_data: {usage}")

            result = {"assistant": assistant}

            if usage:
                # Pass through complete usage data (OpenAI may include additional details like cached_tokens, reasoning_tokens etc.)
                result["usage"] = usage
                logger.debug(f"Added usage to result: {result['usage']}")
            else:
                logger.debug("No usage data in response_data")

            return result

        except Exception as e:
            logger.error(f"Failed to format response: {e}, raw data: {response_data}")
            return {"assistant": {"role": "assistant", "content": ""}}


# Factory function for easy integration
def create_httpx_openai_client(
    model: str,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    verify: Optional[bool] = None,
    **kwargs
) -> HTTPXOpenAIClient:
    """Create HTTPX-based OpenAI client with sensible defaults."""
    return HTTPXOpenAIClient(
        model=model,
        api_key=api_key,
        base_url=base_url,
        verify=verify,
        **kwargs
    )