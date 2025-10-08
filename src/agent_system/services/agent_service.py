"""
Agent Service

Centralized agent execution and session management service.
Handles agent task execution, session management, and streaming responses.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

from agent_system.config.models import AgentSystemConfig


logger = logging.getLogger(__name__)


class AgentService:
    """Centralized agent execution service.
    
    This service provides unified agent management including:
    - Task execution with streaming results
    - Session creation and management
    - Session optimization
    - Multi-turn conversation support
    """

    def __init__(self, agent: Any, config: AgentSystemConfig):
        """Initialize the AgentService.
        
        Args:
            agent: Agent instance (from agent_system.agent.agent).
            config: AgentSystemConfig instance.
        """
        self._agent = agent
        self._config = config
        logger.info("AgentService initialized with agent type: %s", type(agent).__name__)

    async def execute_task(
        self,
        task: str,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        images: Optional[list[bytes]] = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute an agent task with streaming results.
        
        Args:
            task: Task description or prompt.
            session_id: Optional session ID for multi-turn conversations.
            request_id: Optional request ID for tracking.
            images: Optional list of image bytes for multimodal input.
        
        Yields:
            Event dictionaries with structure:
            - type: Event type (step, thought, tool_call, result, error, end)
            - data: Event-specific data
            - timestamp: ISO timestamp
        """
        if not task:
            logger.error("execute_task called with empty task")
            yield {
                "type": "error",
                "message": "Task cannot be empty",
                "request_id": request_id
            }
            return

        request_id = request_id or self._generate_request_id()
        
        logger.info(
            "Executing task: request_id=%s, session_id=%s, task_length=%d, images=%s",
            request_id,
            session_id,
            len(task),
            len(images) if images else 0
        )

        try:
            # Handle multimodal input if images provided
            if images:
                message = await self._create_multimodal_message(task, images)
                logger.debug("Created multimodal message with %d images", len(images))
            else:
                message = task

            # Execute agent with streaming
            async for event in self._agent.run_events(
                message,
                request_id=request_id,
                session_id=session_id
            ):
                event_type = event.get("type")
                logger.debug(
                    "Agent event: type=%s, request_id=%s, session_id=%s",
                    event_type,
                    request_id,
                    session_id
                )
                yield event
                
                # Stop if end event received
                if event_type == "end":
                    logger.info("Task execution completed: request_id=%s", request_id)
                    break

        except Exception as e:
            logger.exception(
                "Task execution failed: request_id=%s, session_id=%s, error=%s",
                request_id,
                session_id,
                e
            )
            yield {
                "type": "error",
                "message": str(e),
                "request_id": request_id,
                "session_id": session_id
            }

    async def execute_task_collect_result(
        self,
        task: str,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        images: Optional[list[bytes]] = None
    ) -> dict[str, Any]:
        """Execute a task and collect the final result (non-streaming).
        
        Args:
            task: Task description.
            session_id: Optional session ID.
            request_id: Optional request ID.
            images: Optional image bytes.
        
        Returns:
            Dictionary with final result containing:
            - result: Final answer/response
            - steps: List of execution steps
            - request_id: Request identifier
            - session_id: Session identifier (if used)
            - error: Error message (if failed)
        """
        request_id = request_id or self._generate_request_id()
        
        logger.info(
            "Executing task (collect result): request_id=%s, session_id=%s",
            request_id,
            session_id
        )

        try:
            # Collect all events
            events = []
            async for event in self.execute_task(task, session_id, request_id, images):
                events.append(event)
            
            # Extract final result
            result_event = next(
                (e for e in reversed(events) if e.get("type") == "result"),
                None
            )
            
            if result_event:
                return {
                    "result": result_event.get("data", {}).get("result", ""),
                    "steps": [e for e in events if e.get("type") in ("step", "thought", "tool_call")],
                    "request_id": request_id,
                    "session_id": session_id,
                    "status": "success"
                }
            else:
                # Check for error
                error_event = next(
                    (e for e in events if e.get("type") == "error"),
                    None
                )
                
                if error_event:
                    logger.error(
                        "Task failed: request_id=%s, error=%s",
                        request_id,
                        error_event.get("message")
                    )
                    return {
                        "error": error_event.get("message", "Unknown error"),
                        "request_id": request_id,
                        "session_id": session_id,
                        "status": "error"
                    }
                else:
                    logger.warning("Task completed without result or error: request_id=%s", request_id)
                    return {
                        "result": "",
                        "request_id": request_id,
                        "session_id": session_id,
                        "status": "no_result"
                    }

        except Exception as e:
            logger.exception("Failed to collect task result: request_id=%s, error=%s", request_id, e)
            return {
                "error": str(e),
                "request_id": request_id,
                "session_id": session_id,
                "status": "error"
            }

    async def create_session(self) -> str:
        """Create a new session for multi-turn conversations.
        
        Returns:
            Session ID string.
        """
        session_id = self._generate_session_id()
        
        logger.info("Creating new session: session_id=%s", session_id)
        
        try:
            # Pre-create empty session in agent
            async with self._agent._request_lock:
                self._agent._sessions.setdefault(session_id, [])
            
            logger.debug("Session created successfully: session_id=%s", session_id)
            return session_id
            
        except Exception as e:
            logger.exception("Failed to create session: error=%s", e)
            raise RuntimeError(f"Session creation failed: {e}") from e

    async def get_session(self, session_id: str) -> Optional[list[dict[str, Any]]]:
        """Get session messages.
        
        Args:
            session_id: Session identifier.
        
        Returns:
            List of session messages or None if session doesn't exist.
        """
        logger.debug("Retrieving session: session_id=%s", session_id)
        
        try:
            async with self._agent._request_lock:
                messages = self._agent._sessions.get(session_id)
            
            if messages is None:
                logger.warning("Session not found: session_id=%s", session_id)
                return None
            
            logger.debug("Session retrieved: session_id=%s, message_count=%d", session_id, len(messages))
            return messages
            
        except Exception as e:
            logger.exception("Failed to retrieve session: session_id=%s, error=%s", session_id, e)
            raise RuntimeError(f"Session retrieval failed: {e}") from e

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session.
        
        Args:
            session_id: Session identifier.
        
        Returns:
            True if deleted, False if not found.
        """
        logger.info("Deleting session: session_id=%s", session_id)
        
        try:
            async with self._agent._request_lock:
                if session_id in self._agent._sessions:
                    del self._agent._sessions[session_id]
                    logger.debug("Session deleted: session_id=%s", session_id)
                    return True
                else:
                    logger.warning("Session not found for deletion: session_id=%s", session_id)
                    return False
                    
        except Exception as e:
            logger.exception("Failed to delete session: session_id=%s, error=%s", session_id, e)
            raise RuntimeError(f"Session deletion failed: {e}") from e

    async def append_to_session(
        self,
        session_id: str,
        content: str,
        role: str = "user"
    ) -> bool:
        """Append a message to a session.
        
        Args:
            session_id: Session identifier.
            content: Message content.
            role: Message role (user/assistant/system).
        
        Returns:
            True if appended successfully, False if session not found.
        """
        logger.debug(
            "Appending to session: session_id=%s, role=%s, content_length=%d",
            session_id,
            role,
            len(content)
        )
        
        try:
            success = await self._agent.append_to_session(session_id, content)
            
            if success:
                logger.debug("Message appended to session: session_id=%s", session_id)
            else:
                logger.warning("Failed to append to session (not found): session_id=%s", session_id)
            
            return success
            
        except Exception as e:
            logger.exception(
                "Failed to append to session: session_id=%s, error=%s",
                session_id,
                e
            )
            raise RuntimeError(f"Session append failed: {e}") from e

    async def optimize_session(self, session_id: str) -> dict[str, Any]:
        """Force session optimization (summarization/compression).
        
        Args:
            session_id: Session identifier.
        
        Returns:
            Dictionary with optimization results:
            - success: Boolean
            - original_tokens: Token count before optimization
            - optimized_tokens: Token count after optimization
            - reduction_percent: Percentage reduction
            - error: Error message (if failed)
        """
        logger.info("Optimizing session: session_id=%s", session_id)
        
        try:
            # Check session exists
            async with self._agent._request_lock:
                if session_id not in self._agent._sessions:
                    logger.warning("Cannot optimize non-existent session: session_id=%s", session_id)
                    return {
                        "success": False,
                        "error": f"Session {session_id} not found"
                    }
                
                # Get token count before
                session_messages = self._agent._sessions[session_id]
                original_count = len(session_messages)

            # Trigger optimization
            if hasattr(self._agent, 'optimize_context'):
                await self._agent.optimize_context(session_id)
                logger.debug("Context optimization triggered for session: session_id=%s", session_id)
            else:
                logger.warning("Agent does not support context optimization")
                return {
                    "success": False,
                    "error": "Optimization not supported by agent"
                }

            # Get token count after
            async with self._agent._request_lock:
                optimized_messages = self._agent._sessions.get(session_id, [])
                optimized_count = len(optimized_messages)

            reduction = original_count - optimized_count
            reduction_percent = (reduction / original_count * 100) if original_count > 0 else 0

            logger.info(
                "Session optimized: session_id=%s, original=%d, optimized=%d, reduction=%.1f%%",
                session_id,
                original_count,
                optimized_count,
                reduction_percent
            )

            return {
                "success": True,
                "session_id": session_id,
                "original_messages": original_count,
                "optimized_messages": optimized_count,
                "reduction_percent": round(reduction_percent, 1)
            }

        except Exception as e:
            logger.exception("Session optimization failed: session_id=%s, error=%s", session_id, e)
            return {
                "success": False,
                "error": str(e),
                "session_id": session_id
            }

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List all active sessions.
        
        Returns:
            List of session info dictionaries with:
            - session_id: Session identifier
            - message_count: Number of messages in session
        """
        logger.debug("Listing all sessions")
        
        try:
            async with self._agent._request_lock:
                sessions = [
                    {
                        "session_id": sid,
                        "message_count": len(messages)
                    }
                    for sid, messages in self._agent._sessions.items()
                ]
            
            logger.debug("Listed %d sessions", len(sessions))
            return sessions
            
        except Exception as e:
            logger.exception("Failed to list sessions: error=%s", e)
            raise RuntimeError(f"Session listing failed: {e}") from e

    async def cancel_request(self, request_id: str) -> bool:
        """Cancel an ongoing request.
        
        Args:
            request_id: Request identifier.
        
        Returns:
            True if cancelled, False if not found or already completed.
        """
        logger.info("Cancelling request: request_id=%s", request_id)
        
        try:
            if hasattr(self._agent, 'cancel_request'):
                success = await self._agent.cancel_request(request_id)
                
                if success:
                    logger.info("Request cancelled: request_id=%s", request_id)
                else:
                    logger.warning("Request not found or already completed: request_id=%s", request_id)
                
                return success
            else:
                logger.warning("Agent does not support request cancellation")
                return False
                
        except Exception as e:
            logger.exception("Failed to cancel request: request_id=%s, error=%s", request_id, e)
            raise RuntimeError(f"Request cancellation failed: {e}") from e

    async def _create_multimodal_message(
        self,
        task: str,
        images: list[bytes]
    ) -> Any:
        """Create a multimodal message with text and images.
        
        Args:
            task: Text prompt.
            images: List of image bytes.
        
        Returns:
            Multimodal message in agent-compatible format.
        """
        logger.debug("Creating multimodal message: task_length=%d, images=%d", len(task), len(images))
        
        try:
            from agent_system.utils.image_processor import create_multimodal_message, ImageProcessingError
            import tempfile
            from pathlib import Path
            
            # Save images to temp files
            temp_dir = Path(tempfile.mkdtemp())
            temp_files = []
            
            try:
                for i, img_bytes in enumerate(images):
                    temp_path = temp_dir / f"image_{i}.jpg"
                    temp_path.write_bytes(img_bytes)
                    temp_files.append(str(temp_path))
                
                # Create multimodal message
                message = create_multimodal_message(task, temp_files)
                logger.debug("Multimodal message created with %d images", len(temp_files))
                return message
                
            finally:
                # Cleanup temp files
                for temp_file in temp_files:
                    try:
                        Path(temp_file).unlink()
                    except Exception as cleanup_error:
                        logger.warning("Failed to cleanup temp file: %s, error=%s", temp_file, cleanup_error)
                        
        except ImageProcessingError as e:
            logger.error("Image processing failed: %s", e)
            raise RuntimeError(f"Image processing failed: {e}") from e
        except Exception as e:
            logger.exception("Failed to create multimodal message: %s", e)
            raise RuntimeError(f"Multimodal message creation failed: {e}") from e

    @staticmethod
    def _generate_request_id() -> str:
        """Generate a unique request ID."""
        return str(uuid4())[:8]

    @staticmethod
    def _generate_session_id() -> str:
        """Generate a unique session ID."""
        return str(uuid4())[:8]
