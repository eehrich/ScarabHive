"""Cognitive Stack MCP Server implementation.

This module provides a stack-based working memory for LLMs to manage nested
contexts, interrupt-and-resume patterns, and hierarchical problem-solving.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.hooks.plugin_hook import HookContext, HookResult
from agent_system.utils.id import short_id

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


@dataclass
class StackFrame:
    """Single frame in the cognitive stack."""

    frame_id: str  # Unique ID for this frame
    context: str  # Description of what we're working on
    timestamp: datetime
    data: dict[str, Any] = field(default_factory=dict)  # Optional structured data
    metadata: dict[str, Any] = field(default_factory=dict)  # Frame-specific metadata


@dataclass
class CognitiveStack:
    """Stack state for a single session."""

    stack_id: str
    created_at: datetime
    last_accessed: datetime
    frames: list[StackFrame] = field(default_factory=list)
    max_depth: int = 20  # Prevent infinite recursion


class CognitiveStackServer(SchemaBasedMCPServer):
    """Cognitive Stack MCP server for working memory management.

    This server provides:
    - push_batch: Push one or more contexts onto stack
    - pop_batch: Pop one or more contexts and return them
    - peek: View top context(s) without removing
    - list: List all frames in stack
    - clear: Clear entire stack

    Features:
    - Nested context management (push_batch/pop_batch)
    - Structured data storage per frame
    - Session management with TTL cleanup
    - Depth limit protection
    """

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """
        Initialize Cognitive Stack server.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)

        # Configuration
        self.max_depth = int(getattr(mcp_config, 'max_depth', 20))
        self.max_frames_in_prompt = int(getattr(mcp_config, 'max_frames_in_prompt', 3))

        # Session storage (in-memory)
        self._stacks: dict[str, CognitiveStack] = {}

        # Mapping: agent_session_id → cognitive_stack_id
        self._agent_session_mapping: dict[str, str] = {}

        logger.info(f"Cognitive Stack server '{name}' initialized - max_depth={self.max_depth}")

    def get_template_vars(self) -> dict[str, Any]:
        """Provide custom template variables for schema rendering."""
        return {
            "name": self.name,
            "max_depth": self.max_depth
        }

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Main tool entry point - dispatches to operation-specific methods.

        This method is called when tool name exactly matches server name ({{name}}).
        The SchemaBasedToolMixin automatically routes {{name}} → execute().
        """
        # Support both 'operation' and 'op' (some LLMs abbreviate)
        operation = params.get("operation") or params.get("op")

        if operation == "push_batch":
            return await self.push_batch(params)
        elif operation == "pop_batch":
            return await self.pop_batch(params)
        elif operation == "peek":
            return await self.peek(params)
        elif operation == "list":
            return await self.list_frames(params)
        elif operation == "clear":
            return await self.clear(params)
        else:
            status = params.get("_status")
            if status:
                await status.error(f"Unknown operation: {operation}")
            return {
                "status": "error",
                "error": f"Unknown operation: {operation}. Valid: push_batch, pop_batch, peek, list, clear"
            }

    def _get_or_create_stack(self, stack_id: str | None, agent_session_id: str | None = None) -> CognitiveStack:
        """Get existing stack or create new one.

        Args:
            stack_id: Cognitive stack ID (10-char hex)
            agent_session_id: Agent conversation session ID (for hook lookup)
        """
        if stack_id and stack_id in self._stacks:
            stack = self._stacks[stack_id]
            stack.last_accessed = datetime.now()

            # Update agent session mapping if provided
            if agent_session_id and agent_session_id not in self._agent_session_mapping:
                self._agent_session_mapping[agent_session_id] = stack_id

            return stack

        # Create new stack with short ID
        new_id = stack_id or short_id(10)
        stack = CognitiveStack(
            stack_id=new_id,
            created_at=datetime.now(),
            last_accessed=datetime.now(),
            max_depth=self.max_depth
        )
        self._stacks[new_id] = stack

        # Add to agent session mapping if provided
        if agent_session_id:
            self._agent_session_mapping[agent_session_id] = new_id
            logger.debug(f"Mapped agent session {agent_session_id} → stack {new_id}")

        logger.info(f"Created new cognitive stack: {new_id}")
        return stack

    def _resolve_stack_id(self, params: dict[str, Any]) -> str | None:
        """Resolve stack_id from params or _session_id mapping.

        Priority:
        1. Explicit stack_id in params
        2. Look up via _session_id mapping
        3. Return None (will create new stack or error)

        Args:
            params: Tool call parameters

        Returns:
            Resolved stack_id or None
        """
        # Explicit stack_id has priority
        stack_id = params.get("stack_id")
        if stack_id:
            return stack_id

        # Try to resolve via session mapping
        agent_session_id = params.get("_session_id")
        if agent_session_id:
            mapped_stack_id = self._agent_session_mapping.get(agent_session_id)
            if mapped_stack_id:
                logger.debug(f"Resolved stack_id {mapped_stack_id} from session {agent_session_id}")
                return mapped_stack_id

        return None

    async def peek(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        View top frame without removing it.

        Tool name: {{ name }}_peek → e.g., 'cognitive_stack_peek'
        """
        status = params.get("_status")
        try:
            stack_id = self._resolve_stack_id(params)
            if not stack_id:
                error_msg = "No active cognitive stack found. Use push_batch first to create a stack."
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            depth = params.get("depth", 1)  # How many frames to peek (default: top 1)

            if stack_id not in self._stacks:
                error_msg = f"Stack {stack_id} not found"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            stack = self._stacks[stack_id]
            stack.last_accessed = datetime.now()

            if not stack.frames:
                if status:
                    await status.end("Stack is empty")
                return {
                    "status": "success",
                    "stack_id": stack.stack_id,
                    "frames": [],
                    "depth": 0,
                    "message": "Stack is empty"
                }

            # Get top N frames
            frames_to_show = stack.frames[-depth:] if depth > 0 else stack.frames

            if status:
                await status.end(f"Peeked top {len(frames_to_show)} frame(s)")

            return {
                "status": "success",
                "stack_id": stack.stack_id,
                "frames": [
                    {
                        "frame_id": f.frame_id,
                        "context": f.context,
                        "data": f.data,
                        "timestamp": f.timestamp.isoformat()
                    }
                    for f in frames_to_show
                ],
                "total_depth": len(stack.frames),
                "message": f"Top {len(frames_to_show)} frame(s) of {len(stack.frames)}"
            }

        except Exception as e:
            logger.exception(f"Error in peek: {e}")
            if status:
                await status.error(f"Failed to peek frame: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def list_frames(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        List all frames in stack.

        Tool name: {{ name }}_list → e.g., 'cognitive_stack_list'
        """
        status = params.get("_status")
        try:
            stack_id = self._resolve_stack_id(params)
            if not stack_id:
                error_msg = "No active cognitive stack found. Use push_batch first to create a stack."
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            if stack_id not in self._stacks:
                error_msg = f"Stack {stack_id} not found"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            stack = self._stacks[stack_id]
            stack.last_accessed = datetime.now()

            if status:
                await status.end(f"Listed {len(stack.frames)} frame(s)")

            return {
                "status": "success",
                "stack_id": stack.stack_id,
                "frames": [
                    {
                        "frame_id": f.frame_id,
                        "context": f.context,
                        "data": f.data,
                        "timestamp": f.timestamp.isoformat()
                    }
                    for f in stack.frames
                ],
                "depth": len(stack.frames),
                "max_depth": stack.max_depth,
                "created_at": stack.created_at.isoformat(),
                "last_accessed": stack.last_accessed.isoformat()
            }

        except Exception as e:
            logger.exception(f"Error in list_frames: {e}")
            if status:
                await status.error(f"Failed to list frames: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def clear(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Clear all frames from stack.

        Tool name: {{ name }}_clear → e.g., 'cognitive_stack_clear'
        """
        status = params.get("_status")
        try:
            # Try to resolve stack_id (explicit or via session)
            stack_id = self._resolve_stack_id(params)

            if stack_id:
                # Clear specific stack
                if stack_id not in self._stacks:
                    error_msg = f"Stack {stack_id} not found"
                    if status:
                        await status.error(error_msg)
                    return {"status": "error", "error": error_msg}

                stack = self._stacks[stack_id]
                frame_count = len(stack.frames)
                stack.frames.clear()

                if status:
                    await status.end(f"Cleared {frame_count} frame(s)")

                return {
                    "status": "success",
                    "stack_id": stack.stack_id,
                    "cleared_frames": frame_count,
                    "message": f"Cleared {frame_count} frames from stack"
                }
            else:
                # Clear all stacks
                total_frames = sum(len(s.frames) for s in self._stacks.values())
                stack_count = len(self._stacks)

                self._stacks.clear()
                self._agent_session_mapping.clear()

                if status:
                    await status.end(f"Cleared {stack_count} stack(s) with {total_frames} frame(s)")

                return {
                    "status": "success",
                    "cleared_stacks": stack_count,
                    "cleared_frames": total_frames,
                    "message": f"Cleared all {stack_count} stacks ({total_frames} frames)"
                }

        except Exception as e:
            logger.exception(f"Error in clear: {e}")
            if status:
                await status.error(f"Failed to clear stack: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def push_batch(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Push multiple contexts onto stack in one operation.

        Useful for loading a list of tasks/problems to work through systematically.

        Tool name: {{ name }}_push_batch → e.g., 'cognitive_stack_push_batch'
        """
        status = params.get("_status")
        try:
            # Extract parameters
            items = params.get("items", [])
            stack_id = params.get("stack_id")
            agent_session_id = params.get("_session_id")

            # Validation
            if not items:
                error_msg = (
                    "items array cannot be empty for push_batch. "
                    "CORRECT USAGE: {operation: 'push_batch', items: [{context: 'Task 1'}, {context: 'Task 2'}]}. "
                    "Each item must be an OBJECT with 'context' key, not just a string!"
                )
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            if not isinstance(items, list):
                error_msg = "items must be an array"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            # Get or create stack
            stack = self._get_or_create_stack(stack_id, agent_session_id)

            # Check if batch would exceed depth limit
            if len(stack.frames) + len(items) > stack.max_depth:
                error_msg = f"Batch would exceed max depth ({stack.max_depth}). Current: {len(stack.frames)}, trying to add: {len(items)}"
                if status:
                    await status.error(error_msg)
                return {
                    "status": "error",
                    "error": error_msg,
                    "hint": "Reduce batch size or clear some frames first"
                }

            # Push all items
            pushed_frames = []
            for item in items:
                if not isinstance(item, dict):
                    error_msg = (
                        f"Each item must be an OBJECT with 'context' key, got: {type(item).__name__}. "
                        f"WRONG: items: ['Task 1', 'Task 2']. "
                        f"CORRECT: items: [{{context: 'Task 1'}}, {{context: 'Task 2'}}]"
                    )
                    if status:
                        await status.error(error_msg)
                    return {"status": "error", "error": error_msg}

                context = item.get("context")
                if not context or not context.strip():
                    error_msg = "Each item must have non-empty 'context'"
                    if status:
                        await status.error(error_msg)
                    return {"status": "error", "error": error_msg}

                data = item.get("data", {})

                # Create frame
                frame = StackFrame(
                    frame_id=short_id(),
                    context=context,
                    timestamp=datetime.now(),
                    data=data
                )

                stack.frames.append(frame)
                pushed_frames.append({
                    "frame_id": frame.frame_id,
                    "context": context
                })

            if status:
                await status.end(
                    f"Pushed {len(items)} frames (depth: {len(stack.frames)}/{stack.max_depth})"
                )

            return {
                "status": "success",
                "stack_id": stack.stack_id,
                "pushed_count": len(items),
                "frames": pushed_frames,
                "depth": len(stack.frames),
                "max_depth": stack.max_depth,
                "message": f"Pushed {len(items)} frames onto stack (depth: {len(stack.frames)})"
            }

        except Exception as e:
            logger.exception(f"Error in push_batch: {e}")
            if status:
                await status.error(f"Failed to push batch: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def pop_batch(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Pop multiple frames from stack in one operation.

        Returns all popped frames in order (top to bottom).

        Tool name: {{ name }}_pop_batch → e.g., 'cognitive_stack_pop_batch'
        """
        status = params.get("_status")
        try:
            stack_id = self._resolve_stack_id(params)
            count = params.get("count", 1)

            # Validation
            if not stack_id:
                error_msg = "No active cognitive stack found. Use push_batch first to create a stack."
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            if count < 1:
                error_msg = "count must be at least 1"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            if stack_id not in self._stacks:
                error_msg = f"Stack {stack_id} not found"
                if status:
                    await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            stack = self._stacks[stack_id]
            stack.last_accessed = datetime.now()

            if not stack.frames:
                error_msg = "Stack is empty"
                if status:
                    await status.error(error_msg)
                return {
                    "status": "error",
                    "error": error_msg,
                    "hint": "Use push_batch() to add frames to the stack first"
                }

            # Pop up to count frames (or all available)
            actual_count = min(count, len(stack.frames))
            popped_frames = []

            for _ in range(actual_count):
                frame = stack.frames.pop()
                popped_frames.append({
                    "frame_id": frame.frame_id,
                    "context": frame.context,
                    "data": frame.data,
                    "timestamp": frame.timestamp.isoformat()
                })

            if status:
                await status.end(f"Popped {actual_count} frame(s) (depth: {len(stack.frames)})")

            return {
                "status": "success",
                "stack_id": stack.stack_id,
                "popped_count": actual_count,
                "frames": popped_frames,
                "remaining_depth": len(stack.frames),
                "message": f"Popped {actual_count} frames from stack (depth now: {len(stack.frames)})"
            }

        except Exception as e:
            logger.exception(f"Error in pop_batch: {e}")
            if status:
                await status.error(f"Failed to pop batch: {str(e)}")
            return {"status": "error", "error": str(e)}

    # =========================================================================
    # Hook Implementation: System Prompt Injection
    # =========================================================================

    async def on_pre_llm_call(self, context: HookContext) -> HookResult:
        """
        Inject active cognitive stack into system prompt before LLM call.

        Shows top N frames to provide working memory context.

        Args:
            context: Hook context with messages, session_id, agent

        Returns:
            HookResult with modified=True if stack info was injected
        """
        if not context.messages:
            logger.debug("CognitiveStackHook: No messages in context, skipping")
            return HookResult(success=True, modified=False, context=context)

        if not context.session_id:
            logger.debug("CognitiveStackHook: No session_id in context, skipping")
            return HookResult(success=True, modified=False, context=context)

        try:
            agent_session_id = context.session_id
            stack_id = self._agent_session_mapping.get(agent_session_id)

            from agent_system.llm.models import ChatMessage

            # Check if already injected and REMOVE old injection(s)
            # Loop backwards to safely remove multiple occurrences
            for i in range(len(context.messages) - 1, -1, -1):
                msg = context.messages[i]
                msg_content = msg.content if hasattr(msg, 'content') else msg.get('content', '')
                if msg_content and ("Cognitive Stack" in msg_content and msg_content.startswith("##")):
                    context.messages.pop(i)
                    logger.debug(f"Removed old cognitive stack injection at index {i}")

            if stack_id and stack_id in self._stacks:
                stack = self._stacks[stack_id]

                if stack.frames:
                    # Format active stack
                    stack_prompt = self._format_stack_for_prompt(stack)
                    logger.info(
                        f"[CognitiveStackHook] Injecting stack with {len(stack.frames)} frame(s)"
                    )
                else:
                    # Empty stack - show reminder
                    stack_prompt = self._format_stack_reminder()
                    logger.info("[CognitiveStackHook] Empty stack - injecting reminder")
            else:
                # No active stack - inject reminder about tool
                stack_prompt = self._format_stack_reminder()
                logger.info("[CognitiveStackHook] No active stack - injecting tool reminder")

            # Insert after first system message
            insert_pos = self._find_system_message_position(context.messages)
            context.messages.insert(insert_pos, ChatMessage(
                role="system",
                content=stack_prompt
            ))

            return HookResult(success=True, modified=True, context=context)

        except Exception as e:
            logger.error(f"CognitiveStackHook failed: {e}", exc_info=True)
            # Don't fail the entire LLM call if hook fails
            return HookResult(success=True, modified=False, context=context)

    def _format_stack_reminder(self) -> str:
        """Format cognitive stack tool reminder when no active stack."""
        return f"""## Cognitive Stack Available

You have no active stack. Use `{self.name}(operation="push_batch", items=[...])` to start tracking contexts.

**Example - multiple tasks to solve systematically:**
```
{self.name}(operation="push_batch", items=[
  {{"context": "Task 1: Fix login bug", "data": {{"priority": "high"}}}},
  {{"context": "Task 2: Update UI styling", "data": {{"priority": "low"}}}}
])
{self.name}(operation="list")
{self.name}(operation="pop_batch", count=1)
```
"""

    def _format_stack_for_prompt(self, stack: CognitiveStack) -> str:
        """Format active stack for injection into prompt."""
        lines = []
        lines.append("## Active Cognitive Stack\n")
        lines.append(f"**Depth**: {len(stack.frames)}/{stack.max_depth}\n")

        # Show recent frames (top N)
        frames_to_show = stack.frames[-self.max_frames_in_prompt:]

        if frames_to_show:
            lines.append("**Working Memory (top frames):**")
            for i, frame in enumerate(reversed(frames_to_show), 1):
                # Calculate position from top (after reverse, i=1 is topmost)
                position = len(stack.frames) - i + 1

                # Show context (truncated)
                context = frame.context[:150] + "..." if len(frame.context) > 150 else frame.context

                # Show data if present
                data_info = ""
                if frame.data:
                    data_keys = ", ".join(frame.data.keys())
                    data_info = f" [data: {data_keys}]"

                lines.append(f"- **Frame #{position}**{data_info}: {context}")

        return "\n".join(lines)

    def _find_system_message_position(self, messages: list) -> int:
        """Find position to insert system message (after first system message)."""
        for i, msg in enumerate(messages):
            role = msg.role if hasattr(msg, 'role') else msg.get('role')
            if role == 'system':
                return i + 1
        return 0  # No system message found, insert at start
