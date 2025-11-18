"""Cognitive Stack MCP Server implementation.

This module provides a stack-based working memory for LLMs to manage nested
contexts, interrupt-and-resume patterns, and hierarchical problem-solving.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    - push: Push new context onto stack
    - pop: Pop and return top context
    - peek: View top context without removing
    - list: List all frames in stack
    - clear: Clear entire stack

    Features:
    - Nested context management (push/pop)
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
        self.session_ttl_seconds = int(getattr(mcp_config, 'session_ttl_seconds', 3600))
        self.max_frames_in_prompt = int(getattr(mcp_config, 'max_frames_in_prompt', 3))

        # Session storage (in-memory)
        self._stacks: dict[str, CognitiveStack] = {}

        # Mapping: agent_session_id → cognitive_stack_id
        self._agent_session_mapping: dict[str, str] = {}

        logger.info(
            f"Cognitive Stack server '{name}' initialized - "
            f"max_depth={self.max_depth}, ttl={self.session_ttl_seconds}s"
        )

    def get_template_vars(self) -> dict[str, Any]:
        """Provide custom template variables for schema rendering."""
        return {
            "name": self.name,
            "max_depth": self.max_depth,
            "session_ttl_seconds": self.session_ttl_seconds
        }

    async def handle_tool_call(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Unified tool handler - dispatches to operation-specific methods.

        Tool name: {{ name }} → e.g., 'cognitive_stack'
        """
        operation = params.get("operation")

        if operation == "push":
            return await self.push(params)
        elif operation == "pop":
            return await self.pop(params)
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
                "error": f"Unknown operation: {operation}. Valid: push, pop, peek, list, clear"
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

    def _cleanup_old_stacks(self) -> None:
        """Remove expired stacks based on TTL."""
        now = datetime.now()
        expired = [
            sid for sid, stack in self._stacks.items()
            if (now - stack.last_accessed) > timedelta(seconds=self.session_ttl_seconds)
        ]
        for sid in expired:
            del self._stacks[sid]

            # Clean up agent session mapping
            for agent_sid, stack_id in list(self._agent_session_mapping.items()):
                if stack_id == sid:
                    del self._agent_session_mapping[agent_sid]

            logger.info(f"Cleaned up expired cognitive stack: {sid}")

    async def push(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Push new context onto cognitive stack.

        Tool name: {{ name }}_push → e.g., 'cognitive_stack_push'
        """
        try:
            # Cleanup old stacks first
            self._cleanup_old_stacks()

            # Extract parameters
            context = params["context"]
            data = params.get("data", {})
            stack_id = params.get("stack_id")
            status = params["_status"]
            agent_session_id = params.get("_session_id")

            # Validation
            if not context or not context.strip():
                await status.error("context cannot be empty")
                return {"status": "error", "error": "context cannot be empty"}

            # Get or create stack
            stack = self._get_or_create_stack(stack_id, agent_session_id)

            # Check depth limit
            if len(stack.frames) >= stack.max_depth:
                await status.error(f"Maximum stack depth ({stack.max_depth}) reached")
                return {
                    "status": "error",
                    "error": f"Maximum stack depth ({stack.max_depth}) reached",
                    "hint": "Pop some frames before pushing more, or use clear() to reset"
                }

            # Create frame
            frame = StackFrame(
                frame_id=short_id(),
                context=context,
                timestamp=datetime.now(),
                data=data
            )

            stack.frames.append(frame)

            await status.end(
                f"Pushed frame #{len(stack.frames)} (depth: {len(stack.frames)}/{stack.max_depth})"
            )

            return {
                "status": "success",
                "stack_id": stack.stack_id,
                "frame_id": frame.frame_id,
                "depth": len(stack.frames),
                "max_depth": stack.max_depth,
                "message": f"Pushed context onto stack (depth: {len(stack.frames)})"
            }

        except Exception as e:
            logger.exception(f"Error in push: {e}")
            await status.error(f"Failed to push frame: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def pop(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Pop and return top frame from cognitive stack.

        Tool name: {{ name }}_pop → e.g., 'cognitive_stack_pop'
        """
        try:
            stack_id = params["stack_id"]
            status = params["_status"]

            if stack_id not in self._stacks:
                await status.error(f"Stack {stack_id} not found")
                return {"status": "error", "error": f"Stack {stack_id} not found"}

            stack = self._stacks[stack_id]
            stack.last_accessed = datetime.now()

            if not stack.frames:
                await status.error("Stack is empty")
                return {
                    "status": "error",
                    "error": "Stack is empty",
                    "hint": "Use push() to add frames to the stack"
                }

            # Pop top frame
            frame = stack.frames.pop()

            await status.end(f"Popped frame (depth: {len(stack.frames)})")

            return {
                "status": "success",
                "stack_id": stack.stack_id,
                "frame": {
                    "frame_id": frame.frame_id,
                    "context": frame.context,
                    "data": frame.data,
                    "timestamp": frame.timestamp.isoformat()
                },
                "remaining_depth": len(stack.frames),
                "message": f"Popped frame from stack (depth now: {len(stack.frames)})"
            }

        except Exception as e:
            logger.exception(f"Error in pop: {e}")
            await status.error(f"Failed to pop frame: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def peek(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        View top frame without removing it.

        Tool name: {{ name }}_peek → e.g., 'cognitive_stack_peek'
        """
        try:
            stack_id = params["stack_id"]
            depth = params.get("depth", 1)  # How many frames to peek (default: top 1)
            status = params["_status"]

            if stack_id not in self._stacks:
                await status.error(f"Stack {stack_id} not found")
                return {"status": "error", "error": f"Stack {stack_id} not found"}

            stack = self._stacks[stack_id]
            stack.last_accessed = datetime.now()

            if not stack.frames:
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
            await status.error(f"Failed to peek frame: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def list_frames(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        List all frames in stack.

        Tool name: {{ name }}_list → e.g., 'cognitive_stack_list'
        """
        try:
            stack_id = params["stack_id"]
            status = params["_status"]

            if stack_id not in self._stacks:
                await status.error(f"Stack {stack_id} not found")
                return {"status": "error", "error": f"Stack {stack_id} not found"}

            stack = self._stacks[stack_id]
            stack.last_accessed = datetime.now()

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
            await status.error(f"Failed to list frames: {str(e)}")
            return {"status": "error", "error": str(e)}

    async def clear(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Clear all frames from stack.

        Tool name: {{ name }}_clear → e.g., 'cognitive_stack_clear'
        """
        try:
            stack_id = params.get("stack_id")
            status = params["_status"]

            if stack_id:
                # Clear specific stack
                if stack_id not in self._stacks:
                    await status.error(f"Stack {stack_id} not found")
                    return {"status": "error", "error": f"Stack {stack_id} not found"}

                stack = self._stacks[stack_id]
                frame_count = len(stack.frames)
                stack.frames.clear()

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

                await status.end(f"Cleared {stack_count} stack(s) with {total_frames} frame(s)")

                return {
                    "status": "success",
                    "cleared_stacks": stack_count,
                    "cleared_frames": total_frames,
                    "message": f"Cleared all {stack_count} stacks ({total_frames} frames)"
                }

        except Exception as e:
            logger.exception(f"Error in clear: {e}")
            await status.error(f"Failed to clear stack: {str(e)}")
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

            # Check if already injected and REMOVE old injection
            for i, msg in enumerate(context.messages):
                msg_content = msg.content if hasattr(msg, 'content') else msg.get('content', '')
                if msg_content and ("## Cognitive Stack Tool" in msg_content or "## Active Cognitive Stack" in msg_content):
                    context.messages.pop(i)
                    break

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
        return f"""## Cognitive Stack Tool Available

Use `{self.name}` tool to manage nested contexts and interrupt-and-resume patterns.

**Start new stack:**
```
{self.name}(
    operation="push",
    context="Analyzing requirements for feature X",
    data={{"feature_id": "X", "step": "requirements"}}
)
```

**Operations:**
- `operation="push"` + `context` + `data={{}}` - Push new context onto stack
- `operation="pop"` + `stack_id` - Pop and return top context
- `operation="peek"` + `stack_id` + `depth=1` - View top N frames without removing
- `operation="list"` + `stack_id` - List all frames
- `operation="clear"` + `stack_id` - Clear stack

**Use cases:**
- Interrupt current work to handle sub-problem
- Track nested problem-solving hierarchies
- Resume previous context after interruption
"""

    def _format_stack_for_prompt(self, stack: CognitiveStack) -> str:
        """Format active stack for injection into prompt."""
        lines = []
        lines.append("## Active Cognitive Stack\n")
        lines.append(f"**Stack ID**: `{stack.stack_id}`")
        lines.append(f"**Depth**: {len(stack.frames)}/{stack.max_depth}\n")

        # Show recent frames (top N)
        frames_to_show = stack.frames[-self.max_frames_in_prompt:]

        if frames_to_show:
            lines.append("**Working Memory (top frames):**")
            for i, frame in enumerate(reversed(frames_to_show), 1):
                # Calculate position from top
                position = len(stack.frames) - (len(frames_to_show) - i)

                # Show context (truncated)
                context = frame.context[:150] + "..." if len(frame.context) > 150 else frame.context

                # Show data if present
                data_info = ""
                if frame.data:
                    data_keys = ", ".join(frame.data.keys())
                    data_info = f" [data: {data_keys}]"

                lines.append(f"- **Frame #{position}**{data_info}: {context}")

        lines.append(f"\n**Operations** (use `stack_id='{stack.stack_id}'):")
        lines.append(f"- `{self.name}(operation='pop', stack_id='{stack.stack_id}')` - Return to previous context")
        lines.append(f"- `{self.name}(operation='push', context='...', data={{}})` - Push new nested context")
        lines.append(f"- `{self.name}(operation='peek', stack_id='{stack.stack_id}', depth=3)` - View more frames")

        return "\n".join(lines)

    def _find_system_message_position(self, messages: list) -> int:
        """Find position to insert system message (after first system message)."""
        for i, msg in enumerate(messages):
            role = msg.role if hasattr(msg, 'role') else msg.get('role')
            if role == 'system':
                return i + 1
        return 0  # No system message found, insert at start
