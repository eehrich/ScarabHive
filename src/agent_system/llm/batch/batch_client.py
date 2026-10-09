"""Batch LLM Client Wrapper.

Wraps a regular LLM client to route requests through the batch queue
when batch processing is enabled. The wrapper is transparent to callers -
they call the same methods but requests may be batched in the background.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..models import (
    PRIVATE_MESSAGE_FIELDS, ChatMessage, LLMClient, LLMRateLimitError, LLMQuotaExhaustedError,
    LLMConnectionError,
)
from ..status_mixin import LLMStatusMixin

if TYPE_CHECKING:
    from .queue_manager import BatchQueueManager
    from agent_system.config.models import BatchProviderConfig
    from agent_system.tools.status import StatusScope

logger = logging.getLogger(__name__)


class BatchLLMClient(LLMClient, LLMStatusMixin):
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
        
        # Status tracking to avoid duplicate messages (LLMStatusMixin)
        self.reset_status_tracking()

        # Whether the last answer came from the batch attempt (False after the
        # sync fallback answered): what prices the call at the batch discount.
        # batch_provider only says what this client is, not who answered.
        self.last_was_batch = True
        
        # Copy attributes from underlying client
        if hasattr(underlying_client, 'context_window'):
            self.context_window = underlying_client.context_window
        if hasattr(underlying_client, 'model'):
            self.model = underlying_client.model

    def set_app_title(self, title: str) -> None:
        """Pass through to underlying client (if it supports it)."""
        super().set_app_title(title)
        if hasattr(self.underlying_client, "set_app_title"):
            self.underlying_client.set_app_title(title)
    
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
        self.reset_status_tracking()  # Reset for new request

        result = await self._submit_reported(messages, None, cancellation_token, status_scope)

        if result is None:
            # Fallback to sync if batch failed
            if self.batch_provider_config.fallback_to_sync:
                await self.report_status(status_scope, f"Fallback to sync: {self.model_name}")
                answer = await self.underlying_client.chat(messages, cancellation_token)
                self._take_sync_latency()
                return answer
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
            # the loop's guards read it: a cut answer (length), a content filter
            if choices[0].get("finish_reason"):
                native["finish_reason"] = choices[0]["finish_reason"]
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
        self.reset_status_tracking()  # Reset for new request

        result = await self._submit_reported(messages, tools, cancellation_token, status_scope)

        if result is None:
            # Fallback to sync if batch failed
            if self.batch_provider_config.fallback_to_sync:
                await self.report_status(status_scope, f"Fallback to sync: {self.model_name}")
                answer = await self.underlying_client.chat_tools(
                    messages, tools, cancellation_token
                )
                self._take_sync_latency()
                return answer
            raise RuntimeError("Batch request failed and fallback is disabled")

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
            self.last_was_batch = False
            await self.report_status(status_scope, f"Streaming: {self.model_name}")
            async for chunk in self.underlying_client.chat_tools_streaming(
                messages, tools, cancellation_token
            ):
                yield chunk
            self._take_sync_latency()
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
    
    def _take_sync_latency(self) -> None:
        """The sync client's latency for the answer it served, where the
        agent-level readers look for it (this client)."""
        self._last_response_duration_ms = getattr(
            self.underlying_client, "_last_response_duration_ms", None)

    def set_llm_hooks(self, on_pre_request: Any = None, on_post_response: Any = None) -> None:
        """Wire the hooks here AND on the underlying client.

        The sync fallback and the streaming path call the underlying client,
        which then reports its own request. The batch attempt before it is a
        request of its own, reported by this wrapper: two requests, one end each.
        """
        super().set_llm_hooks(on_pre_request, on_post_response)
        if hasattr(self.underlying_client, "set_llm_hooks"):
            self.underlying_client.set_llm_hooks(on_pre_request, on_post_response)

    async def _submit_reported(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]],
        cancellation_token: Optional[Any],
        status_scope: Optional["StatusScope"],
    ) -> Optional[Dict[str, Any]]:
        """_submit_batch_request, reported to pre/post_llm_response exactly once.

        Every way the batch request ends reaches post_llm_response once: a
        result (with its usage -- the one row the cost readers count), a failure
        that falls back to sync, a cancel or an exception while waiting (re-raised
        unchanged).
        """
        start = time.time()
        await self._notify_pre_request({
            "provider": f"batch_{self.batch_provider}",
            "model": self.model_name,
            "url": "batch_queue",
            "payload": {"message_count": len(messages), "tool_count": len(tools or [])},
            "is_streaming": False,
            "timestamp_ms": start * 1000,
        })
        ended = False

        async def report_end(**info: Any) -> None:
            nonlocal ended
            ended = True
            await self._notify_post_response({
                "provider": f"batch_{self.batch_provider}", "model": self.model_name,
                "url": "batch_queue", "is_streaming": False,
                "duration_ms": (time.time() - start) * 1000,
                "timestamp_ms": time.time() * 1000, **info,
            })

        try:
            result = await self._submit_batch_request(
                messages=messages,
                tools=tools,
                cancellation_token=cancellation_token,
                status_scope=status_scope,
            )
            self.last_was_batch = result is not None
            if result is None:
                await report_end(error="Batch request failed")
            else:
                usage = result.get("usage") if isinstance(result, dict) else None
                await report_end(usage=usage)
            return result
        except BaseException as error:
            if not ended:
                if isinstance(error, asyncio.CancelledError):
                    text = str(error) or "cancelled"
                else:
                    text = str(error) or type(error).__name__
                await report_end(error=text)
            raise

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
        
        await self.report_status(status_scope, f"Queuing batch: {self.model_name}")
        
        # Convert messages to JSON-serializable format
        # Use mode='json' to ensure datetime objects are converted to ISO strings
        messages_data = []
        for msg in messages:
            if hasattr(msg, 'model_dump'):
                # Use mode='json' to convert datetime to ISO strings
                msg_dict = msg.model_dump(mode='json')
                for key in PRIVATE_MESSAGE_FIELDS:  # ours, never the provider's
                    msg_dict.pop(key, None)
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
        except (LLMRateLimitError, LLMQuotaExhaustedError, LLMConnectionError):
            # Typisierte Fallback-Fehler durchreichen — der Agent-Server
            # schaltet darauf die Profil-Kette. Der Generic-Handler unten
            # (return None) wuerde LLMConnectionError schlucken und in
            # fallback_to_sync degradieren — gegen einen toten Endpoint hilft
            # der Sync-Weg desselben Providers nicht. LLMServerError (5xx)
            # bleibt BEWUSST beim Sync-Fallback: der Batch-Weg kann kaputt
            # sein, waehrend der Sync-Weg antwortet.
            await self.report_status(status_scope, f"LLM error: {self.model_name}")
            raise
        except Exception as e:
            logger.error("Batch request failed: %s", e)
            await self.report_status(status_scope, f"Batch failed: {self.model_name}")
            return None
    
    def __repr__(self) -> str:
        return f"BatchLLMClient(model={self.model_name}, batch_provider={self.batch_provider})"
