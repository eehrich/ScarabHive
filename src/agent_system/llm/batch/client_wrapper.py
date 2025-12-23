"""Batch LLM Client Wrapper.

Wraps a regular LLM client to route requests through the batch queue
when batch processing is enabled. The wrapper is transparent to callers -
they call the same methods but requests may be batched in the background.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..models import ChatMessage, LLMClient

if TYPE_CHECKING:
    from .queue_manager import BatchQueueManager
    from agent_system.config.models import BatchAPIConfig

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
        # Created by LLMFactory when batch is enabled
        client = BatchLLMClient(
            underlying_client=regular_llm_client,
            queue_manager=global_batch_manager,
            batch_config=config,
            model_name="gpt-4o"
        )
        
        # Use like a normal client
        result = await client.chat_tools(messages, tools)
    """
    
    def __init__(
        self,
        underlying_client: LLMClient,
        queue_manager: "BatchQueueManager",
        batch_config: "BatchAPIConfig",
        model_name: str,
        provider: str,
    ):
        """Initialize the batch wrapper.
        
        Args:
            underlying_client: The real LLM client to use for fallback/streaming
            queue_manager: Global batch queue manager
            batch_config: Batch configuration from LLM config
            model_name: Name of the model (for grouping batch requests)
            provider: Provider name (openai, gemini, etc.)
        """
        self.underlying_client = underlying_client
        self.queue_manager = queue_manager
        self.batch_config = batch_config
        self.model_name = model_name
        self.provider = provider
        
        # Copy attributes from underlying client
        if hasattr(underlying_client, 'context_window'):
            self.context_window = underlying_client.context_window
        if hasattr(underlying_client, 'model'):
            self.model = underlying_client.model
            
    async def chat(
        self, 
        messages: List[ChatMessage], 
        cancellation_token: Optional[Any] = None
    ) -> str:
        """Send a chat request through the batch queue.
        
        Args:
            messages: Chat messages
            cancellation_token: Optional cancellation token
            
        Returns:
            Response text
        """
        result = await self._submit_batch_request(
            messages=messages,
            tools=None,
            cancellation_token=cancellation_token
        )
        
        if result is None:
            # Fallback to sync if batch failed
            if self.batch_config.fallback_to_sync:
                logger.warning("Batch request failed, falling back to sync")
                return await self.underlying_client.chat(messages, cancellation_token)
            raise RuntimeError("Batch request failed and fallback is disabled")
            
        # Extract text from result
        if isinstance(result, dict):
            assistant = result.get("assistant", {})
            content = assistant.get("content", "")
            return content if isinstance(content, str) else str(content)
        return str(result)
    
    async def chat_tools(
        self,
        messages: List[ChatMessage],
        tools: List[Dict[str, Any]],
        cancellation_token: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Send a chat request with tools through the batch queue.
        
        Args:
            messages: Chat messages
            tools: Tool definitions
            cancellation_token: Optional cancellation token
            
        Returns:
            Dict with 'assistant' key containing response
        """
        result = await self._submit_batch_request(
            messages=messages,
            tools=tools,
            cancellation_token=cancellation_token
        )
        
        if result is None:
            # Fallback to sync if batch failed
            if self.batch_config.fallback_to_sync:
                logger.warning("Batch request failed, falling back to sync")
                return await self.underlying_client.chat_tools(
                    messages, tools, cancellation_token
                )
            raise RuntimeError("Batch request failed and fallback is disabled")
            
        return result
    
    async def chat_tools_streaming(
        self,
        messages: List[ChatMessage],
        tools: List[Dict[str, Any]],
        cancellation_token: Optional[Any] = None
    ):
        """Streaming is not supported in batch mode.
        
        Falls back to underlying client if fallback_to_sync is enabled,
        otherwise uses batch and yields final result.
        """
        if self.batch_config.fallback_to_sync:
            # Use underlying client for streaming
            logger.debug("Batch mode: falling back to sync client for streaming")
            async for chunk in self.underlying_client.chat_tools_streaming(
                messages, tools, cancellation_token
            ):
                yield chunk
        else:
            # Use batch and yield final result
            result = await self.chat_tools(messages, tools, cancellation_token)
            yield {"type": "final", "assistant": result.get("assistant", {})}
    
    def supports_streaming(self) -> bool:
        """Batch mode does not support true streaming."""
        # If fallback is enabled, we can stream through underlying client
        return self.batch_config.fallback_to_sync and self.underlying_client.supports_streaming()
    
    async def _submit_batch_request(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]],
        cancellation_token: Optional[Any] = None
    ) -> Optional[Dict[str, Any]]:
        """Submit a request to the batch queue and wait for result.
        
        Args:
            messages: Chat messages
            tools: Optional tool definitions
            cancellation_token: Optional cancellation token
            
        Returns:
            Result dict or None if failed
        """
        # Convert messages to JSON-serializable format
        # Use mode='json' to ensure datetime objects are converted to ISO strings
        messages_data = []
        for msg in messages:
            if hasattr(msg, 'model_dump'):
                # Use mode='json' to convert datetime to ISO strings
                messages_data.append(msg.model_dump(mode='json'))
            elif hasattr(msg, 'dict'):
                messages_data.append(msg.dict())
            else:
                messages_data.append({
                    "role": msg.role,
                    "content": msg.content,
                })
        
        try:
            # Submit to queue and get future
            result = await self.queue_manager.submit_request(
                model=self.model_name,
                provider=self.provider,
                messages=messages_data,
                tools=tools,
            )
            return result
            
        except asyncio.CancelledError:
            logger.info("Batch request cancelled")
            raise
        except Exception as e:
            logger.error("Batch request failed: %s", e)
            return None
    
    def __repr__(self) -> str:
        return f"BatchLLMClient(model={self.model_name}, provider={self.provider})"
