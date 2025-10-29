"""OpenAI Realtime API WebSocket session management.

This module provides a clean abstraction for managing WebSocket connections
to the OpenAI Realtime API, handling event serialization/deserialization,
and maintaining session state.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional, Any, AsyncGenerator

logger = logging.getLogger(__name__)


class RealtimeSession:
    """Manages a single OpenAI Realtime API WebSocket session.
    
    This class handles:
    - WebSocket connection lifecycle
    - Event sending and receiving
    - Session configuration
    - Resource cleanup
    
    Usage:
        async with RealtimeSession(model="gpt-realtime", api_key="sk-...") as session:
            await session.configure_session({"instructions": "You are helpful"})
            await session.add_conversation_item({"role": "user", "content": "Hello"})
            await session.create_response()
            
            async for event in session.receive_events():
                if event["type"] == "response.done":
                    break
    """
    
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "wss://api.openai.com/v1/realtime"
    ):
        """Initialize Realtime API session.
        
        Args:
            model: Model name (e.g., "gpt-realtime", "gpt-4o-realtime-preview")
            api_key: OpenAI API key
            base_url: WebSocket base URL (default: official OpenAI endpoint)
        """
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.ws: Optional[Any] = None
        self._connected = False
        self._receive_task: Optional[asyncio.Task] = None
        self._event_queue: asyncio.Queue = asyncio.Queue()
        
    async def __aenter__(self):
        """Async context manager entry - connect to WebSocket."""
        await self.connect()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - close WebSocket."""
        await self.close()
        
    async def connect(self) -> None:
        """Establish WebSocket connection to Realtime API.
        
        Raises:
            RuntimeError: If websockets package is not installed
            Exception: If connection fails
        """
        if self._connected:
            logger.warning("RealtimeSession already connected")
            return
            
        try:
            import websockets
        except ImportError as e:
            raise RuntimeError(
                "websockets package required for Realtime API. "
                "Install with: pip install websockets"
            ) from e
        
        # Validate API key
        if not self.api_key or self.api_key == "None" or self.api_key == "":
            raise ValueError("API key is required for Realtime API connection")
            
        # Build WebSocket URL with model parameter
        url = f"{self.base_url}?model={self.model}"
        
        # Prepare headers with authentication
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1"
        }
        
        try:
            self.ws = await websockets.connect(
                url,
                additional_headers=headers,
                ping_interval=20,  # Keep connection alive
                ping_timeout=10
            )
            self._connected = True
            
            logger.info("WebSocket connection established, starting receive loop")
            
            # Start background task to receive events
            self._receive_task = asyncio.create_task(self._receive_loop())
            
            logger.info("Waiting for session.created event...")
            
            # Wait for session.created event
            event = await self._wait_for_event("session.created", timeout=10.0)
            logger.info(f"Session created: {event.get('session', {}).get('id')}")
            
        except Exception as e:
            logger.error(f"Failed to connect to Realtime API: {e}", exc_info=True)
            self._connected = False
            raise
    
    async def _receive_loop(self) -> None:
        """Background task to receive events from WebSocket and queue them."""
        try:
            async for message in self.ws:
                try:
                    event = json.loads(message)
                    logger.debug(f"Received event: {event.get('type')}")
                    await self._event_queue.put(event)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse event JSON: {e}")
                except Exception as e:
                    logger.error(f"Error processing received event: {e}", exc_info=True)
        except Exception as e:
            if self._connected:
                logger.error(f"WebSocket receive loop error: {e}", exc_info=True)
            # Connection closed or error - stop receiving
        finally:
            logger.debug("WebSocket receive loop ended")
    
    async def _wait_for_event(self, event_type: str, timeout: float = 30.0) -> dict:
        """Wait for a specific event type from the queue.
        
        Args:
            event_type: Event type to wait for (e.g., "session.created")
            timeout: Maximum time to wait in seconds
            
        Returns:
            The event dictionary
            
        Raises:
            asyncio.TimeoutError: If event not received within timeout
        """
        try:
            while True:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
                if event.get("type") == event_type:
                    return event
                # Put back events we're not waiting for
                await self._event_queue.put(event)
                await asyncio.sleep(0.01)  # Yield to avoid busy loop
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for event: {event_type}")
            raise
    
    async def send_event(self, event: dict) -> None:
        """Send an event to the Realtime API.
        
        Args:
            event: Event dictionary to send
            
        Raises:
            RuntimeError: If not connected
        """
        if not self._connected or not self.ws:
            raise RuntimeError("Not connected to Realtime API")
            
        try:
            message = json.dumps(event)
            await self.ws.send(message)
            logger.debug(f"Sent event: {event.get('type')}")
        except Exception as e:
            logger.error(f"Failed to send event: {e}", exc_info=True)
            raise
    
    async def configure_session(self, config: dict) -> None:
        """Configure the Realtime API session.
        
        Args:
            config: Session configuration dictionary containing:
                - modalities: list of "text" and/or "audio"
                - instructions: system instructions (optional)
                - voice: voice to use for audio output (optional)
                - temperature: sampling temperature (optional)
                - tools: list of tool/function definitions (optional)
                - tool_choice: "auto", "none", "required", or specific tool (optional)
                
        Example:
            await session.configure_session({
                "modalities": ["text"],
                "instructions": "You are a helpful assistant",
                "temperature": 0.8,
                "tools": [{"type": "function", "function": {...}}],
                "tool_choice": "auto"
            })
        """
        event = {
            "type": "session.update",
            "session": config
        }
        await self.send_event(event)
        
        # Wait for session.updated confirmation or error event
        # Realtime API may send error events instead of session.updated if config is invalid
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < 10.0:
            try:
                event_rcv = await asyncio.wait_for(self._event_queue.get(), timeout=0.5)
                
                if event_rcv.get("type") == "session.updated":
                    return
                elif event_rcv.get("type") == "error":
                    error_msg = event_rcv.get("error", {}).get("message", "Unknown error")
                    logger.error(f"Realtime API error during session configuration: {error_msg}")
                    raise Exception(f"Realtime API error: {error_msg}")
            except asyncio.TimeoutError:
                continue
        
        # Timeout - neither session.updated nor error received
        raise Exception("session.updated event not received within 10 seconds")
    
    async def add_conversation_item(self, item: dict) -> str:
        """Add an item to the conversation.
        
        Args:
            item: Conversation item dictionary containing:
                - type: "message" or "function_call" or "function_call_output"
                - role: "user", "assistant", or "system" (for messages)
                - content: list of content parts (for messages)
                
        Returns:
            Item ID of the created conversation item
            
        Example:
            item_id = await session.add_conversation_item({
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Hello!"}]
            })
        """
        event = {
            "type": "conversation.item.create",
            "item": item
        }
        await self.send_event(event)
        
        # Wait for confirmation
        response_event = await self._wait_for_event("conversation.item.created", timeout=5.0)
        item_id = response_event.get("item", {}).get("id")
        logger.debug(f"Conversation item created: {item_id}")
        return item_id
    
    async def create_response(
        self,
        instructions: Optional[str] = None,
        modalities: Optional[list[str]] = None
    ) -> None:
        """Request the model to generate a response.
        
        Args:
            instructions: Additional instructions for this response (optional)
            modalities: Override modalities for this response (optional)
        """
        event: dict[str, Any] = {
            "type": "response.create"
        }
        
        if instructions or modalities:
            response_config = {}
            if instructions:
                response_config["instructions"] = instructions
            if modalities:
                response_config["modalities"] = modalities
            event["response"] = response_config
            
        await self.send_event(event)
        logger.debug("Response creation requested")
    
    async def receive_events(self) -> AsyncGenerator[dict, None]:
        """Yield events from the Realtime API as they arrive.
        
        Yields:
            Event dictionaries from the server
            
        Example:
            async for event in session.receive_events():
                if event["type"] == "response.text.delta":
                    print(event["delta"], end="")
                elif event["type"] == "response.done":
                    break
        """
        while self._connected:
            try:
                # Get event from queue with timeout
                event = await asyncio.wait_for(self._event_queue.get(), timeout=0.1)
                yield event
            except asyncio.TimeoutError:
                # No event ready, continue loop
                continue
            except Exception as e:
                logger.error(f"Error receiving event: {e}", exc_info=True)
                break
    
    async def close(self) -> None:
        """Close the WebSocket connection and cleanup resources."""
        if not self._connected:
            return
            
        self._connected = False
        
        # Cancel receive task
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        
        # Close WebSocket
        if self.ws:
            try:
                await self.ws.close()
                logger.info("Realtime API WebSocket closed")
            except Exception as e:
                logger.error(f"Error closing WebSocket: {e}")
            finally:
                self.ws = None
