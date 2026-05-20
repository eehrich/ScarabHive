"""Batch LLM Client Wrapper.

Wraps a regular LLM client to route requests through the batch queue
when batch processing is enabled. The wrapper is transparent to callers -
they call the same methods but requests may be batched in the background.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..models import ChatMessage, LLMClient, LLMRateLimitError, LLMQuotaExhaustedError

if TYPE_CHECKING:
    from .queue_manager import BatchQueueManager
    from agent_system.config.models import BatchProviderConfig
    from agent_system.mcp.status import StatusScope

logger = logging.getLogger(__name__)


class BatchLLMClient(LLMClient):
    """LLM client wrapper that routes requests through batch processing.
    
    This wrapper intercepts LLM calls and:
    1. Submits them to the BatchQueueManager
    2. Waits for the batch to be processed
    3. Returns the result when available
    
    For callers, this is transparent - they just see a longer response time.
    The benefit is 50% cost reduction on API calls.
    
    Streaming is NOT supported in batch mode - we fall back to the underlying
    client for streaming requests if fallback_to_sync is enabled.
    
    Usage:
        # Created by LLMFactory when provider='batch'
        client = BatchLLMClient(
            underlying_client=regular_llm_client,
            queue_manager=global_batch_manager,
            batch_provider_config=provider_config,
            model_name="gpt-4o",
            batch_provider="openai"
        )
        
        # Use like a normal client
        result = await client.chat_tools(messages, tools)
    """
    
    def __init__(
        self,
        underlying_client: LLMClient,
        queue_manager: "BatchQueueManager",
        batch_provider_config: "BatchProviderConfig",
        model_name: str,
        batch_provider: str,
    ):
        """Initialize the batch wrapper.
        
        Args:
            underlying_client: The real LLM client to use for fallback/streaming
            queue_manager: Global batch queue manager
            batch_provider_config: Batch provider configuration (from global batch.providers.*)
            model_name: Name of the model (for grouping batch requests)
            batch_provider: Batch provider name (openai, gemini)
        """
        self.underlying_client = underlying_client
        self.queue_manager = queue_manager
        self.batch_provider_config = batch_provider_config
        self.model_name = model_name
        self.batch_provider = batch_provider
        
        # Status tracking to avoid duplicate messages
        self._last_status_message: Optional[str] = None
        
        # Copy attributes from underlying client
        if hasattr(underlying_client, 'context_window'):
            self.context_window = underlying_client.context_window
        if hasattr(underlying_client, 'model'):
            self.model = underlying_client.model

    def set_app_title(self, title: str) -> None:
        """Pass through to underlying client (if it supports it)."""
        if hasattr(self.underlying_client, "set_app_title"):
            self.underlying_client.set_app_title(title)
    
    async def _report_status(
        self,
        status_scope: Optional["StatusScope"],
        message: str,
        force: bool = False
    ) -> None:
        """Report a status update if scope is available and message changed.
        
        Args:
            status_scope: Optional StatusScope for reporting progress
            message: Status message to report
            force: If True, send even if message hasn't changed
        """
        if status_scope is None:
            return
            
        # Skip duplicate messages unless forced
        if not force and message == self._last_status_message:
            return
            
        self._last_status_message = message
        
        try:
            await status_scope.progress(message)
        except Exception as e:
            # Never let status reporting break the LLM call
            logger.debug(f"Failed to report LLM status: {e}")
            
    async def chat(
        self, 
        messages: List[ChatMessage], 
        cancellation_token: Optional[Any] = None,
        status_scope: Optional["StatusScope"] = None
    ) -> str:
        """Send a chat request through the batch queue.
        
        Args:
            messages: Chat messages
            cancellation_token: Optional cancellation token
            status_scope: Optional status scope for progress reporting
            
        Returns:
            Response text
        """
        self._last_status_message = None  # Reset for new request
        
        result = await self._submit_batch_request(
            messages=messages,
            tools=None,
            cancellation_token=cancellation_token,
            status_scope=status_scope
        )
        
        if result is None:
            # Fallback to sync if batch failed
            if self.batch_provider_config.fallback_to_sync:
                await self._report_status(status_scope, f"Fallback to sync: {self.model_name}")
                return await self.underlying_client.chat(messages, cancellation_token)
            raise RuntimeError("Batch request failed and fallback is disabled")
        
        # Extract text from result - support both OpenAI batch format and native format
        # Note: Status "Batch completed" already reported by queue_manager
        return self._extract_content(result)
    
    def _extract_content(self, result: Any) -> str:
        """Extract content from batch response.
        
        Supports both OpenAI batch format (choices[0].message.content)
        and native format (assistant.content).
        """
        if isinstance(result, dict):
            # Try OpenAI batch format first: {"choices": [{"message": {"content": "..."}}]}
            choices = result.get("choices")
            if choices and isinstance(choices, list) and len(choices) > 0:
                message = choices[0].get("message", {})
                content = message.get("content", "")
                if content:
                    return content if isinstance(content, str) else str(content)
            
            # Try native format: {"assistant": {"content": "..."}}
            assistant = result.get("assistant", {})
            if isinstance(assistant, dict):
                content = assistant.get("content", "")
                if content:
                    return content if isinstance(content, str) else str(content)
            
            # Try direct content
            content = result.get("content", "")
            if content:
                return content if isinstance(content, str) else str(content)
                
        return str(result) if result else ""
    
    def _convert_to_native_format(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Convert OpenAI batch format to native format expected by agent.
        
        OpenAI batch format: {"choices": [{"message": {"role": "assistant", "content": "..."}}], "usage": {...}}
        Native format: {"assistant": {"role": "assistant", "content": "..."}, "usage": {...}}
        """
        if not isinstance(result, dict):
            return {"assistant": {"role": "assistant", "content": str(result)}}
        
        # Already in native format
        if "assistant" in result:
            return result
            
        # Convert from OpenAI batch format
        choices = result.get("choices")
        if choices and isinstance(choices, list) and len(choices) > 0:
            message = choices[0].get("message", {})
            native = {"assistant": message}
            # Preserve usage data if present
            if "usage" in result:
                native["usage"] = result["usage"]
            return native
        
        # If we have direct content, wrap it
        if "content" in result:
            native = {"assistant": {"role": "assistant", "content": result["content"]}}
            # Preserve usage data if present
            if "usage" in result:
                native["usage"] = result["usage"]
            return native
            
        # Return as-is if we can't determine the format
        return result
    
    async def chat_tools(
        self,
        messages: List[ChatMessage],
        tools: List[Dict[str, Any]],
        cancellation_token: Optional[Any] = None,
        status_scope: Optional["StatusScope"] = None
    ) -> Dict[str, Any]:
        """Send a chat request with tools through the batch queue.
        
        Args:
            messages: Chat messages
            tools: Tool definitions
            cancellation_token: Optional cancellation token
            status_scope: Optional status scope for progress reporting
            
        Returns:
            Dict with 'assistant' key containing response
        """
        self._last_status_message = None  # Reset for new request
        
        # Notify pre-request hook (LLM-client level)
        import time as _time
        _batch_start = _time.time()
        await self._notify_pre_request({
            "provider": f"batch_{self.batch_provider}",
            "model": self.model_name,
            "url": "batch_queue",
            "payload": {"message_count": len(messages), "tool_count": len(tools)},
            "is_streaming": False,
            "timestamp_ms": _time.time() * 1000,
        })
        
        result = await self._submit_batch_request(
            messages=messages,
            tools=tools,
            cancellation_token=cancellation_token,
            status_scope=status_scope
        )
        
        if result is None:
            # Notify post-response hook on failure
            _duration_ms = (_time.time() - _batch_start) * 1000
            await self._notify_post_response({
                "provider": f"batch_{self.batch_provider}", "model": self.model_name,
                "url": "batch_queue", "is_streaming": False,
                "duration_ms": _duration_ms, "error": "Batch request failed",
                "timestamp_ms": _time.time() * 1000,
            })
            # Fallback to sync if batch failed
            if self.batch_provider_config.fallback_to_sync:
                await self._report_status(status_scope, f"Fallback to sync: {self.model_name}")
                return await self.underlying_client.chat_tools(
                    messages, tools, cancellation_token
                )
            raise RuntimeError("Batch request failed and fallback is disabled")
        
        # Notify post-response hook on success
        _duration_ms = (_time.time() - _batch_start) * 1000
        await self._notify_post_response({
            "provider": f"batch_{self.batch_provider}", "model": self.model_name,
            "url": "batch_queue", "is_streaming": False,
            "duration_ms": _duration_ms,
            "timestamp_ms": _time.time() * 1000,
        })
        
        # Convert from OpenAI batch format to native format
        # Note: Status "Batch completed" already reported by queue_manager
        return self._convert_to_native_format(result)
    
    async def chat_tools_streaming(
        self,
        messages: List[ChatMessage],
        tools: List[Dict[str, Any]],
        cancellation_token: Optional[Any] = None,
        status_scope: Optional["StatusScope"] = None
    ):
        """Streaming is not supported in batch mode.
        
        Falls back to underlying client if fallback_to_sync is enabled,
        otherwise uses batch and yields final result.
        """
        if self.batch_provider_config.fallback_to_sync:
            # Use underlying client for streaming
            await self._report_status(status_scope, f"Streaming: {self.model_name}")
            async for chunk in self.underlying_client.chat_tools_streaming(
                messages, tools, cancellation_token
            ):
                yield chunk
        else:
            # Use batch and yield final result
            result = await self.chat_tools(messages, tools, cancellation_token, status_scope)
            yield {"type": "final", "assistant": result.get("assistant", {})}
    
    def supports_streaming(self) -> bool:
        """Batch mode does not support streaming.
        
        Always returns False to ensure agent uses chat_tools() instead of
        chat_tools_streaming(). The fallback_to_sync option is only used
        when batch submission fails, not for enabling streaming.
        """
        return False
    
    async def _submit_batch_request(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]],
        cancellation_token: Optional[Any] = None,
        status_scope: Optional["StatusScope"] = None
    ) -> Optional[Dict[str, Any]]:
        """Submit a request to the batch queue and wait for result.
        
        Args:
            messages: Chat messages
            tools: Optional tool definitions
            cancellation_token: Optional cancellation token
            status_scope: Optional status scope for progress reporting
            
        Returns:
            Result dict or None if failed
        """
        # Check cancellation before starting
        if cancellation_token and cancellation_token.is_cancelled:
            logger.info("Batch request cancelled before submission")
            raise asyncio.CancelledError("Cancelled before batch submission")
        
        await self._report_status(status_scope, f"Queuing batch: {self.model_name}")
        
        # Convert messages to JSON-serializable format
        # Use mode='json' to ensure datetime objects are converted to ISO strings
        messages_data = []
        for msg in messages:
            if hasattr(msg, 'model_dump'):
                # Use mode='json' to convert datetime to ISO strings
                msg_dict = msg.model_dump(mode='json')
                msg_dict.pop('injected_by', None)  # Internal hook metadata
                messages_data.append(msg_dict)
            elif hasattr(msg, 'dict'):
                messages_data.append(msg.dict())
            else:
                messages_data.append({
                    "role": msg.role,
                    "content": msg.content,
                })
        
        # Get generation parameters from underlying client if available
        max_tokens = getattr(self.underlying_client, 'max_tokens', None)
        
        # Get thinking parameters from underlying client's extra_params (Gemini only)
        # Note: include_thoughts is always False for batch to avoid wasting tokens
        extra_params = getattr(self.underlying_client, 'extra_params', {})
        thinking_budget = extra_params.get('thinking_budget')
        thinking_level = extra_params.get('thinking_level')
        safety_settings = getattr(self.underlying_client, 'safety_settings', None)
        
        try:
            # Submit to queue and get future with cancellation support
            # Status updates are reported by the queue_manager
            result = await self.queue_manager.submit_request(
                model=self.model_name,
                provider=self.batch_provider,
                messages=messages_data,
                tools=tools,
                max_tokens=max_tokens,
                thinking_budget=thinking_budget,
                thinking_level=thinking_level,
                safety_settings=safety_settings,
                cancellation_token=cancellation_token,
                status_scope=status_scope,
            )
            return result
            
        except asyncio.CancelledError:
            logger.info("Batch request cancelled")
            raise
        except (LLMRateLimitError, LLMQuotaExhaustedError):
            # Propagate rate limit errors for fallback handling
            await self._report_status(status_scope, f"Rate limited: {self.model_name}")
            raise
        except Exception as e:
            logger.error("Batch request failed: %s", e)
            await self._report_status(status_scope, f"Batch failed: {self.model_name}")
            return None
    
    def __repr__(self) -> str:
        return f"BatchLLMClient(model={self.model_name}, batch_provider={self.batch_provider})"
