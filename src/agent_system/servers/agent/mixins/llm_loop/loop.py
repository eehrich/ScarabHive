"""Phase 2 of a run: the step loop, _execute_llm_loop, and the order of a step's phases.

_execute_llm_loop sets the run up (its structured output checked, its loop detector and stuck
escalator built, its state in a LoopState) and runs the steps, up to max_steps and one final call.
_run_step is the step, phase by phase: open it, choose its LLM, the pre-LLM hooks, the call with its
fallbacks, the answer recorded and shown, then what the answer asks for -- tool calls or a text
answer -- and the end of the step. A phase that ends the step early says so on the StepState
(StepEnd), where the one generator of old said ``continue`` or ``return``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from .....llm.models import LLMClient
from .....llm.structured_output import (
    STRUCTURED_OUTPUT_INVALID, STRUCTURED_OUTPUT_UNAVAILABLE, STRUCTURED_OUTPUT_UNSUPPORTED,
    InvalidResponseFormat, ResponseFormat,
    SchemaCheckerError, instruction_text, prepare_response_format,
    unsupported_message,
)
from .....tools.status import StatusScope
from ..run_phases import ConversationContext
from .answer import AnswerMixin
from .fallback import FallbackMixin
from .llm_call import LLMCallMixin
from .state import LoopState, StepEnd, StepState, takes_format
from .step import StepMixin
from .step_llm import StepLLMMixin
from .tool_step import ToolStepMixin

if TYPE_CHECKING:
    from ...server import Agent

logger = logging.getLogger(__name__)


class LLMLoopMixin(LLMCallMixin, StepLLMMixin, FallbackMixin, StepMixin, AnswerMixin, ToolStepMixin):
    """The step loop and the LLM call of a step (see the package docstring).

    Relies on nearly everything Agent.__init__ sets up (``llm``, ``agent_config``, ``system_config``,
    ``timeouts``, ``llm_profile_info``, ``_reasoning_loop_config``, the session tracker, the hook,
    tool execution and request managers, ``_step_llms``) and on the methods of the other mixins:
    the step's LLM (llm_selection.py), its prompts and notes (prompts.py), the live state
    (live_state.py), the saves (persistence.py) and the presence step (run.py).
    """

    async def _execute_llm_loop(
        self: Agent,
        context: ConversationContext,
        request_id: str,
        session_id: str,
        status_coordinator: StatusScope,
        status_worker: StatusScope,
        llm_override: Optional[LLMClient] = None,
        llm_profile_info_override: Optional[str] = None,
        use_advanced_model: bool = False,
        response_format: Optional[ResponseFormat] = None,
    ):
        """Execute the main LLM conversation loop with tool execution.

        Phase 2 of agent execution: Iterative LLM calls with tool execution.

        Loops up to max_steps, plus one final call that asks for the answer
        (see _step_budget_note) and goes through the same step machinery:
        1. Check cancellation
        2. Drain appended messages
        3. Call LLM with tools
        4. Handle thinking/status events
        5. Execute tool calls (if any)
        6. Check loop guards (empty responses, no tool calls)
        7. Update messages with results

        Args:
            context: Conversation context with messages and tools
            request_id: Request identifier
            session_id: Session identifier
            status_coordinator: Coordinator status scope
            status_worker: Worker status scope
            llm_override: Optional LLM client override
            llm_profile_info_override: Optional profile info for status

        Yields:
            Dict events: heartbeat, thinking, status, tool_*, final, error, cancelled

        Returns:
            Tuple of (messages, results, step) after loop completion
        """
        # Determine which LLM to use. Phase 1 already validated availability;
        # this guard keeps the invariant explicit for direct callers (and
        # narrows the type from LLMClient|None).
        active_llm = llm_override if llm_override is not None else self.llm
        if active_llm is None:
            raise RuntimeError("No LLM available; agent requires an LLM to run")

        display_profile_info = self.llm_profile_info
        max_block_seconds = float(self.agent_config.fallback_recovery_seconds
                                  if self.agent_config else 3600)

        # Extract from context
        messages = context.messages
        tools_schema = context.tools_schema
        tool_name_mapping = context.tool_name_mapping
        max_steps = context.max_steps
        main_token = context.main_token

        # Initialize results
        results: Dict[str, Any] = {"task": "", "calls": []}

        max_cut_off_notes = int(getattr(self.agent_config, "output_cap_notes", 0) or 0)

        # A format nobody checked yet (a caller that built it itself; openai_api prepares its own):
        # its schema goes through the worker's subset before anything runs, and the run works with
        # what the worker made of it.
        if response_format is not None and not response_format.checked:
            from .....core.request_context import get_request_user

            try:
                response_format = await prepare_response_format(response_format, owner=get_request_user(request_id))
            except (InvalidResponseFormat, SchemaCheckerError) as bad:
                unavailable = isinstance(bad, SchemaCheckerError)  # busy or broken: no verdict on the format
                error_msg = (f"Structured output: the format could not be checked, the checker is not available: {bad}"
                             if unavailable else f"Structured output: the requested format cannot be used: {bad}")
                logger.warning("[%s] %s", self.name, error_msg)
                results.setdefault("errors", []).append(error_msg)
                yield {"type": "error", "message": error_msg,
                       "error_type": STRUCTURED_OUTPUT_UNAVAILABLE if unavailable else STRUCTURED_OUTPUT_INVALID}
                return

        # The run's own model decides at the start, not at the first step that lands on it: a
        # walk around it while it is blocked would otherwise end the run mid-way, after its tool
        # steps, once the block lifts and the next step goes back to it.
        if not takes_format(response_format, active_llm):
            error_msg = unsupported_message(active_llm, response_format)
            logger.warning("[%s] %s", self.name, error_msg)
            results.setdefault("errors", []).append(error_msg)
            yield {"type": "error", "message": error_msg, "error_type": STRUCTURED_OUTPUT_UNSUPPORTED}
            return
        # The description as it goes into the history: a note that is there under its marker but
        # says something else (a compaction's placeholder) does not count as there.
        format_note_text = instruction_text(response_format) if response_format is not None else None

        # Create a request-scoped loop detector.
        # Each request gets its own detector so concurrent requests on the
        # same Agent singleton don't contaminate each other's history, and
        # history from a previous request on the same session doesn't
        # cause false positives at the start of a new request.
        loop_detector = self._create_loop_detector()

        # Per-request auto-escalation: swap in the advanced model for a few steps
        # when the run loop observes the agent is stuck (loop detector / repeated
        # tool errors). State is request-scoped (must not leak across requests on
        # this shared Agent singleton). Disabled unless configured and an advanced
        # profile exists and we're not already running advanced.
        escalator = self._create_stuck_escalator(
            already_advanced=(use_advanced_model or self._switches_model(llm_override)))
        escalate_error_streak = int(
            getattr(self.agent_config, "escalate_error_streak", 2)) if self.agent_config else 2

        # What lives across the steps (state.py); its fields say what each guard counts.
        run = LoopState(
            context=context,
            request_id=request_id,
            session_id=session_id,
            status_coordinator=status_coordinator,
            status_worker=status_worker,
            llm_override=llm_override,
            llm_profile_info_override=llm_profile_info_override,
            use_advanced_model=use_advanced_model,
            response_format=response_format,
            format_note_text=format_note_text,
            tools_schema=tools_schema,
            tool_name_mapping=tool_name_mapping,
            max_steps=max_steps,
            main_token=main_token,
            max_block_seconds=max_block_seconds,
            loop_detector=loop_detector,
            escalator=escalator,
            escalate_error_streak=escalate_error_streak,
            results=results,
            messages=messages,
            active_llm=active_llm,
            display_profile_info=display_profile_info,
            max_cut_off_notes=max_cut_off_notes,
        )

        # One iteration past the budget: the final call. It used to be a bare
        # chat_tools() after the loop, and so it skipped everything a step does
        # -- the pre-LLM hooks (message_validator dropped no orphaned tool call,
        # context_engineer capped nothing), the fallback chain, streaming, the
        # post-LLM hooks. As a step it has all of that; what differs is only
        # that no step follows it (see final_call below).
        for step in range(run.max_steps + 1):
            st = StepState(step=step, final_call=step == run.max_steps)
            async for event in self._run_step(run, st):
                yield event
            if st.end is StepEnd.RUN:
                return

        # Reached only when the final call ended without text and without a
        # tool call that ran: empty or blank, or its tool calls all blocked.
        # A step's cancel is reported at the top of the next iteration; there
        # is none after the final call.
        if self._is_cancelled(request_id):
            async for event in self._cancelled_events(run, run.max_steps):
                yield event
            return
        results.setdefault("errors", []).append("LLM planner reached max steps without final answer.")
        yield {"type": "error", "message": "LLM planner reached max steps without final answer."}

    async def _run_step(self: Agent, run: LoopState, st: StepState):
        """One step, phase by phase. A phase that ends the step sets ``st.end`` (st.ended()); the
        phases after it do not run (StepEnd.NEXT_STEP: on to the next step; StepEnd.RUN: the run
        is over)."""
        async for event in self._begin_step(run, st):  # step.py
            yield event
        if st.ended():
            return
        await self._choose_step_llm(run, st)  # step_llm.py
        async for event in self._run_pre_llm_hooks(run, st):  # step.py
            yield event
        await self._settle_step_llm(run, st)  # step_llm.py
        async for event in self._call_step_llm(run, st):  # fallback.py
            yield event
        if st.ended():
            return

        await self._record_answer(run, st)  # step.py
        async for event in self._run_post_llm_hooks(run, st):
            yield event
        async for event in self._show_answer(run, st):
            yield event
        self._check_truncation(run, st)
        async for event in self._handle_empty_answer(run, st):
            yield event
        if st.ended():
            return

        # Check if we have tool calls to execute
        if st.tool_calls:
            async for event in self._tool_step(run, st):  # tool_step.py
                yield event
            return
        async for event in self._text_step(run, st):  # answer.py
            yield event
        if st.ended():
            return
        await self._end_step(run, st)  # step.py
