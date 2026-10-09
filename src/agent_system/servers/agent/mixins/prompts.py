"""What the run tells the model besides the conversation: the system prompt and the run's notes.

The system prompt (rendered by prompt_strategies.py with the plugins and MCP servers switched on, or
by a subclass's get_custom_system_prompt); the notes a run writes into the conversation as the
developer -- the step budget, an answer cut off at the output cap, the structured output's format and
what is wrong with an answer -- with the check of a structured answer; and the pick between the
messages before and after the pre-LLM hooks. Its own module: the texts and rules a step sends
besides the conversation, in one place.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ....llm.message_roles import DEVELOPER
from ....llm.models import ChatMessage
from ....llm.structured_output import STRUCTURED_OUTPUT_UNAVAILABLE, ResponseFormat, check_answer
from ..prompt_strategies import PromptRenderer, PromptContext

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)


class PromptsMixin:
    """The system prompt and the run's notes (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``agent_config``, ``system_config``, ``registry``,
    ``_session_tracker`` and ``_tool_integration_manager``.
    """

    # ------------------------------------------------------------------
    # Prompt customization hook
    # ------------------------------------------------------------------
    def get_custom_system_prompt(self: Agent, context: Dict[str, Any]) -> Optional[str]:  # pragma: no cover - default noop
        """Subclass hook: return a fully rendered system prompt string or None.

        If a subclass returns a non-empty string here, that prompt is used as the
        primary system prompt and configuration driven template / raw prompt
        logic is skipped (except tools prompt injection which still occurs).

        Args:
            context: Dict containing keys like 'tools', 'max_steps', plus any
                     auto datetime context if enabled.

        Returns:
            The custom system prompt string or None to fall back to config logic.
        """
        return None

    #: Agents with fewer allowed steps get no step-budget note: a judge or
    #: extractor that answers in one call would read "finish now" as part of
    #: its task.
    _STEP_BUDGET_NOTE_MIN_STEPS = 5
    #: The note rides on this many of the last steps.
    _STEP_BUDGET_NOTE_LAST_STEPS = 2

    @staticmethod
    def _structured_output_note(text: str, marker: str) -> ChatMessage:
        """What the run tells a model about its structured answer: the format (to a model that
        does not get it as a field), or what is wrong with the answer it gave. The RUN speaks, as
        for the step budget, and injected_by keeps it from counting as a turn."""
        return ChatMessage(role=DEVELOPER, content=text, timestamp=datetime.now(timezone.utc),
                           injected_by=marker)

    @staticmethod
    async def _check_structured_answer(content: str, response_format: ResponseFormat, request_id: str) -> Any:
        """The answer's check, in the schema worker's process under its deadline (structured_output.
        check_answer), in the lane of the run's user: the schema is the caller's, and this process never
        runs jsonschema or regex on it."""
        from ....core.request_context import get_request_user

        return await check_answer(content, response_format, owner=get_request_user(request_id))

    @staticmethod
    def _structured_output_unavailable(checked: Any) -> dict:
        """The error event of a run whose answer the checker could not look at (busy, broken): no verdict."""
        return {"type": "error", "error_type": STRUCTURED_OUTPUT_UNAVAILABLE,
                "message": "Structured output: the answer could not be checked, the checker is not available: "
                           + "; ".join(checked.errors)}

    @staticmethod
    def _structured_output_failure(errors: List[str], *, corrected: bool) -> str:
        when = " after one correction" if corrected else ""
        return (f"Structured output: the final answer does not match the requested format{when}: "
                + "; ".join(errors))

    @staticmethod
    def _output_cap_note(completion_tokens: Optional[int]) -> ChatMessage:
        """What the run tells a model whose answer the output cap cut off.

        The RUN speaks (developer), as for the step budget: a person did not
        write this, and injected_by keeps it from counting as a turn.
        """
        at = f" ({completion_tokens} tokens)" if completion_tokens else ""
        return ChatMessage(
            role=DEVELOPER,
            content=(f"Your last answer was cut off at the output limit{at}. Everything after the cut is "
                     "lost, including any tool call you were writing -- nothing of it was executed. Do not "
                     "send it again in one piece: write a large file in parts (create it, then add section "
                     "by section), or keep the answer shorter."),
            timestamp=datetime.now(timezone.utc),
            injected_by="agent.output_cap",
        )

    @classmethod
    def _step_budget_note(cls, step: int, max_steps: int) -> Optional[ChatMessage]:
        """A note on the steps left, for the last steps of a run; else None.

        The step count used to sit in the system prompt ("Current step: 3/30").
        That prompt is re-rendered before every step and is the start of the
        prefix the provider caches, so every call re-billed the conversation
        behind it. The note goes into the history instead, like a loop
        intervention: sent only at the tail of one call, it would be missing
        from the next call's prefix, and a provider that caches at the last
        message (Anthropic) would find nothing to read back.

        step == max_steps is the final call after the budget, and it gets the
        max-steps request whatever the budget: it was sent to every agent
        before that call became a step of its own.
        """
        if step >= max_steps:
            return ChatMessage(
                role=DEVELOPER,
                content=(
                    f"You have reached the maximum number of steps ({max_steps}). "
                    "Please provide your final answer NOW based on the information you have gathered. "
                    "Do NOT use any tools in this response - just give me your best answer or summary of what you've accomplished."
                ),
                timestamp=datetime.now(timezone.utc),
                injected_by="agent.max_steps",
            )
        steps_left = max_steps - (step + 1)
        if max_steps < cls._STEP_BUDGET_NOTE_MIN_STEPS or steps_left >= cls._STEP_BUDGET_NOTE_LAST_STEPS:
            return None
        if steps_left == 0:
            note = (f"This is step {max_steps} of {max_steps}, the last one. Finish now with what "
                    f"your task requires, the final answer or the closing tool call, using what "
                    f"you have, and say what is still missing.")
        else:
            note = (f"Step {step + 1} of {max_steps}: {steps_left} step left after this one. "
                    f"Start wrapping up and do not begin new lines of work.")
        # Marked: hooks that look for the last message a person wrote (OKF seeds,
        # scripted follow-ups, tool preloads, compaction) must not take this one.
        return ChatMessage(role=DEVELOPER, content=note, timestamp=datetime.now(timezone.utc),
                           injected_by="agent.step_budget")

    # ------------------------------------------------------------------
    # Pre-LLM Message Selection
    # ------------------------------------------------------------------
    @staticmethod
    def _select_llm_messages(
        pre_hook_messages: List[ChatMessage],
        modified_messages: Optional[List[ChatMessage]],
    ) -> List[ChatMessage]:
        """Pick the final message list to send to the LLM.

        A hook that wants to change what the model sees returns a NEW list;
        list identity is the whole signal, and a hook mutating in place is not
        seen (tool_preload's own comment names this rule). The returned list
        carries every leading system message — the agent's own plus the ones
        hooks inject (pinned forum context, restoration hints, sub-agent
        context) — and the conversation, compacted if a hook compacted it.

        There used to be a second signal here: the ``compacted_messages``
        marker on the session tracker, rebuilt as ``[leading systems from
        pre-hook] + compacted``. That reconstruction is wrong whenever a hook
        injects a system block — it is built from the messages BEFORE the
        hooks ran, so it drops them; that is how the v5b synopsis moderator
        once looped 100× on ``context_engineer.recall()``. It was also
        unreachable: whoever sets that marker inside a hook returns a new list
        in the same round, and the tool path clears it in its own step.
        """
        if modified_messages is not None and modified_messages is not pre_hook_messages:
            return modified_messages
        return pre_hook_messages

    # ------------------------------------------------------------------
    # Central prompt rendering utilities (using strategy pattern)
    # ------------------------------------------------------------------
    def _render_prompts(
        self: Agent, 
        usable_tools: List[str], 
        max_steps: int, 
        current_step: int,
        session_id: Optional[str] = None
    ) -> tuple[str, Optional[str]]:
        """
        Render (system_prompt, tools_prompt) using strategy pattern.

        Args:
            usable_tools: List of tool names available to the agent
            max_steps: Maximum steps allowed for the agent
            current_step: Current step number (1-indexed, for dynamic per-step rendering)
            session_id: Optional session ID for session-scoped template vars

        Order of precedence:
          1. Subclass hook `get_custom_system_prompt`
          2. In-memory raw `agent_config.system_prompt`
          3. File/template based `agent_config.system_template`
          4. Default fallback

        Returns:
            (system_prompt, tools_prompt_or_None)
        """
        # Get session-scoped template vars if session_id provided
        session_template_vars = None
        if session_id and hasattr(self, '_session_tracker') and self._session_tracker:
            session_template_vars = self._session_tracker.get_session_template_vars(session_id)
        
        renderer = PromptRenderer()
        context = PromptContext(
            agent_name=self.name,
            agent_config=self.agent_config,
            system_config=self.system_config,
            available_tools=usable_tools,
            max_steps=max_steps,
            current_step=current_step,
            agent_instance=self,  # Pass self for hook access
            session_template_vars=session_template_vars,  # Session-scoped vars (override agent_config)
            plugins=self._enabled_plugin_types(),
            mcp_servers=self._enabled_mcp_servers(),
        )
        return renderer.render(context)

    def _enabled_plugin_types(self: Agent) -> list[str]:
        """Plugin types installed and switched on (see ToolServerRegistry)."""
        plugin_types = getattr(getattr(self, "registry", None), "plugin_types", None)
        return plugin_types() if callable(plugin_types) else []

    def _enabled_mcp_servers(self: Agent) -> list[str]:
        """External MCP servers switched on, sorted -- not which are connected:
        a connection comes and goes, and the prompt must not change with it."""
        manager = getattr(self, "_tool_integration_manager", None)
        integration = getattr(manager, "tool_integration", None)
        if integration is None:
            return []
        return sorted(integration.configured_external_servers)

    async def get_current_system_prompt(self: Agent) -> str:
        """Async: render current system prompt (diagnostics endpoint)."""
        try:
            # Expanded as the run expands it, or `tools` in the prompt reads
            # server names here and tool names in every step that is sent.
            _schemas, usable_tools = await self._schemas_for(*await self.list_usable_tools())
        except Exception as e:
            logger.warning(f"Failed to list usable tools for system prompt: {e}", exc_info=True)
            usable_tools = []
        max_steps = max(1, int(getattr(self.agent_config, "max_steps", 6)))
        system_msg, _ = self._render_prompts(usable_tools, max_steps, current_step=0)
        return system_msg
