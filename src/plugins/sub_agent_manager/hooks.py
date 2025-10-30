"""Hook implementations for sub-agent context injection."""
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from agent_system.hooks.plugin_hook import HookContext, HookResult
from agent_system.llm.models import ChatMessage

from .manager import SubAgentManager


logger = logging.getLogger(__name__)


class SubAgentContextInjector:
    """Injects active sub-agent information into system prompt before LLM calls."""
    
    def __init__(self, manager: SubAgentManager, config: Optional[Dict[str, Any]] = None):
        """Initialize the context injector.
        
        Args:
            manager: SubAgentManager instance for querying sub-agents
            config: Optional configuration (max_shown, show_completed, etc.)
        """
        self.manager = manager
        self.config = config or {}
        
        # Configuration options
        self.enabled = self.config.get("enabled", True)
        self.max_sub_agents_shown = self.config.get("max_sub_agents_shown", 10)
        self.show_completed = self.config.get("show_completed", False)
        self.show_tool_state = self.config.get("show_tool_state", True)
        self.format = self.config.get("format", "markdown")
    
    async def inject_sub_agent_context(self, context: HookContext) -> HookResult:
        """Inject sub-agent context into messages before LLM call.
        
        This hook modifies the messages list by appending a system message
        containing information about active sub-agents for the current session.
        
        Args:
            context: Hook context with session_id and messages
            
        Returns:
            HookResult with modified=True if context was injected
        """
        try:
            # Check if enabled
            if not self.enabled:
                logger.debug("[SubAgentContext] Hook disabled in config, skipping")
                return HookResult(success=True, modified=False, context=context)
            
            # Validate context
            if not context.session_id:
                logger.debug("[SubAgentContext] No session_id in context, skipping")
                return HookResult(success=True, modified=False, context=context)
            
            if not context.messages:
                logger.debug("[SubAgentContext] No messages in context, skipping")
                return HookResult(success=True, modified=False, context=context)
            
            # Query sub-agents for this session
            try:
                sub_agents = await self.manager.list_sub_sessions(
                    parent_session_id=context.session_id,
                    include_completed=self.show_completed
                )
            except FileNotFoundError:
                # Session file doesn't exist yet (new session) - this is normal
                logger.debug(f"[SubAgentContext] Session {context.session_id} not found (likely new session), skipping")
                return HookResult(success=True, modified=False, context=context)
            except Exception as e:
                logger.warning(f"[SubAgentContext] Unexpected error listing sub-agents: {e}")
                return HookResult(success=True, modified=False, context=context)
            
            # Skip if no sub-agents
            if not sub_agents:
                logger.debug(f"[SubAgentContext] No sub-agents for session {context.session_id}")
                return HookResult(success=True, modified=False, context=context)
            
            # Limit number shown
            if len(sub_agents) > self.max_sub_agents_shown:
                sub_agents = sub_agents[:self.max_sub_agents_shown]
                logger.debug(f"[SubAgentContext] Limited to {self.max_sub_agents_shown} sub-agents")
            
            # Build context message
            context_content = self._build_context_message(sub_agents)
            
            # Inject as system message (insert after first system message if exists)
            injection_message = ChatMessage(
                role="system",
                content=context_content
            )
            
            # Find first user message and insert before it (after system prompt)
            # This keeps the sub-agent context near the beginning but after main system prompt
            modified_messages = list(context.messages)
            insert_position = 1  # Default: after first system message
            
            for i, msg in enumerate(modified_messages):
                if msg.role == "user":
                    insert_position = i
                    break
            
            modified_messages.insert(insert_position, injection_message)
            
            # Update context
            context.messages = modified_messages
            
            logger.info(
                f"[SubAgentContext] Injected {len(sub_agents)} sub-agent(s) into session {context.session_id}"
            )
            
            return HookResult(
                success=True,
                modified=True,
                context=context,
                metadata={"sub_agents_count": len(sub_agents)}
            )
            
        except Exception as e:
            logger.error(f"[SubAgentContext] Hook execution failed: {e}", exc_info=True)
            return HookResult(
                success=False,
                modified=False,
                context=context,
                metadata={"error": str(e)}
            )
    
    def _build_context_message(self, sub_agents: List[Dict[str, Any]]) -> str:
        """Build the context message content from sub-agent metadata.
        
        Args:
            sub_agents: List of sub-agent metadata dicts
            
        Returns:
            Formatted context message string
        """
        if self.format == "markdown":
            return self._build_markdown_context(sub_agents)
        else:
            return self._build_text_context(sub_agents)
    
    def _build_markdown_context(self, sub_agents: List[Dict[str, Any]]) -> str:
        """Build Markdown-formatted context message."""
        lines = [
            "## Active Sub-Agents",
            "",
            "You have access to the following persistent sub-agent instances:",
            ""
        ]
        
        for sub_agent in sub_agents:
            instance_id = sub_agent.get("instance_id", "unknown")
            agent_type = sub_agent.get("agent_type", "unknown")
            status = sub_agent.get("status", "unknown")
            message_count = sub_agent.get("message_count", 0)
            task_summary = sub_agent.get("task_summary", "No description")
            last_used = sub_agent.get("last_used")
            
            # Format last used time
            last_used_str = self._format_time_ago(last_used) if last_used else "Never"
            
            # Build sub-agent section
            lines.extend([
                f"### {agent_type}",
                f"- **Instance ID**: `{instance_id}`",
                f"- **Status**: {status}",
                f"- **Last Used**: {last_used_str}",
                f"- **Messages**: {message_count}",
                f"- **Task**: {task_summary}",
            ])
            
            # Add tool state if enabled
            if self.show_tool_state and sub_agent.get("tools_used"):
                tools_str = ", ".join(sub_agent["tools_used"])
                lines.append(f"- **Tools Used**: {tools_str}")
            
            # Add continue example
            lines.extend([
                "",
                "**To continue this sub-agent:**",
                "```json",
                "{",
                '  "tool": "manage_sub_agent",',
                '  "arguments": {',
                '    "operation": "continue",',
                f'    "instance_id": "{instance_id}",',
                '    "message": "Your follow-up question here"',
                "  }",
                "}",
                "```",
                ""
            ])
        
        lines.extend([
            "---",
            "",
            "**Note:** Sub-agents maintain full conversation history. Use `manage_sub_agent` with `operation: continue` for follow-up questions.",
            ""
        ])
        
        return "\n".join(lines)
    
    def _build_text_context(self, sub_agents: List[Dict[str, Any]]) -> str:
        """Build plain text context message."""
        lines = ["ACTIVE SUB-AGENTS:", ""]
        
        for i, sub_agent in enumerate(sub_agents, 1):
            instance_id = sub_agent.get("instance_id", "unknown")
            agent_type = sub_agent.get("agent_type", "unknown")
            status = sub_agent.get("status", "unknown")
            message_count = sub_agent.get("message_count", 0)
            task_summary = sub_agent.get("task_summary", "No description")
            
            lines.extend([
                f"{i}. {agent_type} ({instance_id})",
                f"   Status: {status} | Messages: {message_count}",
                f"   Task: {task_summary}",
                ""
            ])
        
        lines.append("Use manage_sub_agent tool with operation='continue' to resume conversations.")
        
        return "\n".join(lines)
    
    def _format_time_ago(self, timestamp_str: Optional[str]) -> str:
        """Format timestamp as human-readable 'X time ago' string.
        
        Args:
            timestamp_str: ISO format timestamp string
            
        Returns:
            Human-readable time string (e.g., '2 minutes ago', '1 hour ago')
        """
        if not timestamp_str:
            return "Never"
        
        try:
            timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            delta = now - timestamp
            
            seconds = delta.total_seconds()
            
            if seconds < 60:
                return "just now"
            elif seconds < 3600:
                minutes = int(seconds / 60)
                return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            elif seconds < 86400:
                hours = int(seconds / 3600)
                return f"{hours} hour{'s' if hours != 1 else ''} ago"
            else:
                days = int(seconds / 86400)
                return f"{days} day{'s' if days != 1 else ''} ago"
                
        except Exception as e:
            logger.debug(f"Failed to parse timestamp {timestamp_str}: {e}")
            return "recently"
