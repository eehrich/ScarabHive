"""Hook implementations for sub-agent context injection."""
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Dict, List, Optional

from agent_system.hooks.plugin_hook import HookContext, HookResult
from agent_system.llm.message_roles import DEVELOPER
from agent_system.llm.models import ChatMessage

from .manager import OPEN_STATUSES, SubAgentManager


logger = logging.getLogger(__name__)

# Headers of the injected block (markdown / text format). Only used to find
# blocks persisted before the block carried an injected_by marker.
_LEGACY_HEADERS = ("## Active Sub-Agents", "ACTIVE SUB-AGENTS:")

#: What supersedes the list once the last sub-agent is gone. Saying it
#: costs one turn; deleting the old block instead would rewrite history
#: that the provider has already cached.
_EMPTY_BLOCK = ("## Sub-Agents\n\n"
                "None left -- every sub-agent of this session is archived.")

#: What the statuses mean -- "idle" alone does not say that a run is over.
_LEGEND = ("running: working now. idle: its last run is done; poll or info for the answer, "
           "continue for more. interrupted, failed, cancelled: its last run did not finish.")


class SubAgentContextInjector:
    """Appends this session's sub-agents and what each is doing to the history before an LLM call.

    A `developer` turn at the end, written only when the list says
    something new. Never at the head: there a provider hoists it into the
    prompt and every change invalidates the cached prefix behind it. At the
    end a change costs the new turn and nothing before it -- which is what
    lets the list say running and idle, and not only that an instance exists.
    """

    def __init__(
        self,
        manager: SubAgentManager,
        server_name: str,
        config: Optional[Dict[str, Any]] = None,
        allowed_agents: Optional[List[str]] = None,
        phase_filtering_config: Optional[Dict[str, Any]] = None,
        *,
        status_of: Callable[[Dict[str, Any]], Awaitable[str]],
    ):
        """Initialize the context injector.

        Args:
            manager: SubAgentManager instance for querying sub-agents
            server_name: Name of the sub_agent_manager plugin instance (e.g., "w_sam_gemini")
            config: Optional configuration (max_shown, show_completed, etc.)
            allowed_agents: List of allowed agent types for this manager
            phase_filtering_config: Phase filtering config from server (top-level, not hook_config)
            status_of: A sub-agent's stored metadata -> what it is doing (running, idle, or how
                its last run ended). The server's, which knows the runs; stored, a running and
                an idle instance both say "active".
        """
        self.manager = manager
        self.server_name = server_name  # Track which plugin instance this belongs to
        self.status_of = status_of
        self.config = config or {}
        self.allowed_agents = allowed_agents or []

        # Configuration options
        self.enabled = self.config.get("enabled", True)
        self.max_sub_agents_shown = self.config.get("max_sub_agents_shown", 10)
        self.show_completed = self.config.get("show_completed", False)
        self.format = self.config.get("format", "markdown")
        # Marks the injected message so this instance recognises exactly its
        # own previous block -- never a tool result or user text that quotes
        # the header. Recognises, not replaces: the old block stays.
        self.marker = f"sub_agent_manager:{server_name}"
        
        # Phase-based agent filtering (from server's top-level config)
        # Config structure:
        # phase_filtering:
        #   enabled: true
        #   phase_variable: "workflow_phase"
        #   phase_agents:
        #     planning: [story_designer, story_reviewer]
        #     characters: [character_designer, character_reviewer]
        phase_filtering = phase_filtering_config or {}
        self.phase_filtering_enabled = phase_filtering.get("enabled", False)
        self.phase_variable = phase_filtering.get("phase_variable", "workflow_phase")
        self.phase_agents = phase_filtering.get("phase_agents", {})

    async def inject_sub_agent_context(self, context: HookContext) -> HookResult:
        """Inject sub-agent context into messages before LLM call.

        Appends this instance's sub-agents as a developer turn, and only when
        that list says something new. The block used to sit behind the leading
        system messages, where a provider hoists it into the prompt head: from
        there every change invalidated the cache for the whole history. As a
        turn at the end it changes nothing that came before it.

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

            # Get current phase from session template vars (for phase-based filtering)
            current_phase = self._get_current_phase(context)
            phase_allowed_agents = self._get_phase_allowed_agents(current_phase)
            
            # Query sub-agents for this session
            # Filter by creator_plugin (self.server_name) to only show sub-agents from THIS manager
            try:
                # A failed or cancelled run is listed like an open one: the coordinator may not
                # have ended it itself, and a row that just vanished would say nothing about how.
                # Archived ones only with show_completed -- that is the coordinator's own delete.
                sub_agents = [
                    sub_agent for sub_agent in await self.manager.list_sub_sessions(
                        parent_session_id=context.session_id,
                        include_completed=True,
                        creator_plugin=self.server_name  # Only show sub-agents created by THIS instance
                    )
                    if self.show_completed or sub_agent.get("status") != "archived"
                ]
            except (FileNotFoundError, Exception) as e:
                # Session file doesn't exist yet (new session) or other session-related error
                # This is normal - sessions are created on first save
                if "not found" in str(e).lower():
                    logger.debug(f"[SubAgentContext] Session {context.session_id} not found (likely new session), skipping")
                else:
                    logger.warning(f"[SubAgentContext] Unexpected error listing sub-agents: {e}")
                return HookResult(success=True, modified=False, context=context)

            messages = context.messages
            previous = next((m for m in reversed(messages)
                             if self._is_own_block(m)), None)

            if not sub_agents:
                logger.debug(f"[SubAgentContext] No sub-agents for session {context.session_id}")
                # A list that has become empty is news too, but only once: the
                # old block stays where it is and is superseded, never deleted.
                if previous is None or previous.content == _EMPTY_BLOCK:
                    return HookResult(success=True, modified=False, context=context)
                messages.append(ChatMessage(role=DEVELOPER, content=_EMPTY_BLOCK,
                                            injected_by=self.marker))
                return HookResult(success=True, modified=True, context=context)

            # Open ones first, since the cut below keeps the head: a coordinator that
            # replaced two failed workers must still see the older two it can use.
            # Then newest first by creation time: unlike last_used, it does not move
            # when a sub-agent is continued, so the block stays byte-identical.
            sub_agents.sort(key=lambda x: (x.get("status") in OPEN_STATUSES,
                                           x.get("created_at", ""), x.get("instance_id", "")),
                            reverse=True)

            # Limit number shown
            if len(sub_agents) > self.max_sub_agents_shown:
                sub_agents = sub_agents[:self.max_sub_agents_shown]
                logger.debug(f"[SubAgentContext] Limited to {self.max_sub_agents_shown} sub-agents")

            # What each one is doing, for the rows shown only: asking can mean a file read.
            sub_agents = [{**sub_agent, "status": await self.status_of(sub_agent)}
                          for sub_agent in sub_agents]

            # Build context message with phase-aware allowed agents
            context_content = self._build_context_message(sub_agents, phase_allowed_agents, current_phase)

            if previous is not None and previous.content == context_content:
                return HookResult(success=True, modified=False, context=context)

            messages.append(ChatMessage(
                role=DEVELOPER,
                content=context_content,
                injected_by=self.marker,
            ))
            context.messages = messages

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

    def _is_own_block(self, msg: Any) -> bool:
        """This instance's injected block, or an unmarked one persisted before the marker."""
        injected_by = getattr(msg, "injected_by", None)
        if injected_by is not None:
            return injected_by == self.marker
        content = getattr(msg, "content", None)
        return (getattr(msg, "role", None) == "system" and isinstance(content, str)
                and content.startswith(_LEGACY_HEADERS))

    def _get_current_phase(self, context: HookContext) -> Optional[str]:
        """Get the current workflow phase from session template vars.
        
        Args:
            context: Hook context with agent reference
            
        Returns:
            Current phase value or None if not set
        """
        if not self.phase_filtering_enabled:
            return None
            
        try:
            if context.agent and hasattr(context.agent, '_session_tracker'):
                session_vars = context.agent._session_tracker.get_session_template_vars(context.session_id)
                phase = session_vars.get(self.phase_variable)
                if phase:
                    logger.debug(f"[SubAgentContext] Current phase from session vars: {phase}")
                    return phase
            # The agent's configured default, as the create check falls back to it
            # (server._get_current_phase): without it the block listed every allowed
            # agent while create refused all but the phase's.
            template_vars = getattr(getattr(context.agent, 'agent_config', None), 'template_vars', None)
            if isinstance(template_vars, dict) and template_vars.get(self.phase_variable):
                return template_vars[self.phase_variable]
        except Exception as e:
            logger.debug(f"[SubAgentContext] Could not get phase from session vars: {e}")
        
        return None
    
    def _get_phase_allowed_agents(self, current_phase: Optional[str]) -> Optional[List[str]]:
        """Get list of allowed agents for the current phase.
        
        Args:
            current_phase: Current workflow phase value
            
        Returns:
            List of allowed agent types for this phase, or None if no filtering
        """
        if not self.phase_filtering_enabled or not current_phase:
            return None
            
        phase_agents = self.phase_agents.get(current_phase)
        if phase_agents:
            logger.debug(f"[SubAgentContext] Phase '{current_phase}' allows agents: {phase_agents}")
            return phase_agents
            
        # Check for default/fallback
        default_agents = self.phase_agents.get("_default")
        if default_agents:
            logger.debug(f"[SubAgentContext] Using _default agents for phase '{current_phase}'")
            return default_agents
            
        return None

    def _build_context_message(
        self, 
        sub_agents: List[Dict[str, Any]],
        phase_allowed_agents: Optional[List[str]] = None,
        current_phase: Optional[str] = None
    ) -> str:
        """Build the context message content from sub-agent metadata.

        Args:
            sub_agents: List of sub-agent metadata dicts
            phase_allowed_agents: Optional list of agents allowed in current phase
            current_phase: Current workflow phase for display

        Returns:
            Formatted context message string
        """
        if self.format == "markdown":
            return self._build_markdown_context(sub_agents, phase_allowed_agents, current_phase)
        else:
            return self._build_text_context(sub_agents)

    def _build_markdown_context(
        self, 
        sub_agents: List[Dict[str, Any]],
        phase_allowed_agents: Optional[List[str]] = None,
        current_phase: Optional[str] = None
    ) -> str:
        """Build compact Markdown table context message with phase-aware agent list."""
        lines = ["## Sub-Agents\n"]

        # Show phase-allowed agents if configured
        if phase_allowed_agents and current_phase:
            lines.append(f"**Phase `{current_phase}` - Available agents:** {', '.join(phase_allowed_agents)}\n")
        elif self.allowed_agents and '*' not in self.allowed_agents:
            # Fall back to static allowed_agents if no phase filtering
            lines.append(f"**Available agents:** {', '.join(self.allowed_agents)}\n")

        # No message count or time: they move on every continue, and each move is a new turn.
        lines.append("| Type | Instance ID | Status | Task |")
        lines.append("|------|-------------|--------|------|")

        for sub_agent in sub_agents:
            instance_id = sub_agent.get("instance_id", "unknown")
            agent_type = sub_agent.get("agent_type", "unknown")
            status = sub_agent.get("status", "unknown")
            task = " ".join((sub_agent.get("task_summary") or "").split()).replace("|", "\\|")

            lines.append(f"| {agent_type} | `{instance_id}` | {status} | {task} |")

        lines.append("")
        lines.append(_LEGEND)
        lines.append(f"Continue: `{self.server_name}_manage_sub_agent(operation='continue', instance_id='...', message='...')`")

        return "\n".join(lines)

    def _build_text_context(self, sub_agents: List[Dict[str, Any]]) -> str:
        """Build plain text context message."""
        lines = ["SUB-AGENTS:", ""]

        for i, sub_agent in enumerate(sub_agents, 1):
            instance_id = sub_agent.get("instance_id", "unknown")
            agent_type = sub_agent.get("agent_type", "unknown")
            status = sub_agent.get("status", "unknown")
            task_summary = sub_agent.get("task_summary", "No description")

            # No message count: it grows on every continue and would change the block.
            lines.extend([
                f"{i}. {agent_type} ({instance_id})",
                f"   Status: {status}",
                f"   Task: {task_summary}",
                ""
            ])

        lines.append(_LEGEND)
        lines.append(f"Use the {self.server_name}_manage_sub_agent tool with operation='continue' to resume conversations.")

        return "\n".join(lines)
