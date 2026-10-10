"""The phases of a step around its LLM call, up to where the answer decides the step's course.

Before the call: the step opens (appended messages drained, the cancel check, the heartbeat, the
system prompt rendered again, the presence step) and the pre-LLM hooks run, with the step budget
note after them. After it: the answer becomes the assistant message, the post-LLM hooks may rewrite
it, it goes out as thinking events, a cut-off is logged and an empty answer is handled. What a step
does with tool calls is tool_step.py, with a text answer answer.py; loop.py runs the phases in order.
Here too the events a cancel or a timeout ends the run with (_stop_events).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict

from .....llm.message_roles import DEVELOPER
from .....llm.models import ChatMessage
from .....utils.json_utils import history_safe_tool_calls
from .state import LoopState, StepEnd, StepState

if TYPE_CHECKING:
    from ...server import Agent

logger = logging.getLogger(__name__)


class StepMixin:
    """The phases of a step around its LLM call (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``_hook_manager`` and ``_session_tracker``, and on the
    other mixins for the drain and the cancel check (live_state.py), the prompts (prompts.py), the
    presence step (run.py) and the saves (persistence.py).
    """

    async def _stop_events(self: Agent, run: LoopState, step: int, reason: str, *,
                           worker_line: str, coordinator_line: str, last_event: Dict[str, Any]):
        """The end of a run stopped at *step* (a cancel, a timeout): both status scopes report it,
        then the status events still pending, then *last_event*."""
        # Signal cancellation using status contexts FIRST (so events are queued)
        await run.status_worker.error(worker_line,
                                      meta={"step": step + 1, "reason": reason})
        await run.status_coordinator.error(coordinator_line,
                                           meta={"step": step + 1, "reason": reason})
        # Give status events a moment to be captured by forwarder
        await asyncio.sleep(0.01)
        # Yield all pending status events before cancelled event
        for status_event in run.pending_status_events():
            yield status_event
        yield last_event

    async def _cancelled_events(self: Agent, run: LoopState, step: int, *, during_llm_call: bool = False):
        """The end of a run cancelled at *step*: status lines, pending status, the cancelled event."""
        if during_llm_call:
            logger.info(f"Request {run.request_id} cancelled during LLM call at step {step + 1}")
        else:
            logger.info("Request %s cancelled at step %d", run.request_id, step + 1)
        async with contextlib.aclosing(self._stop_events(
                    run, step, "cancelled",
                    worker_line=f"cancelled at step {step + 1}",
                    coordinator_line=f"cancelled at step {step + 1}",
                    last_event={"type": "cancelled", "request_id": run.request_id, "step": step + 1})) as events:
            async for event in events:
                yield event

    async def _begin_step(self: Agent, run: LoopState, st: StepState):
        """Phase: open the step -- error-streak bookkeeping, appended messages, the cancel check,
        the heartbeat, the system prompt rendered again, the thinking event, the presence step."""
        step = st.step
        if st.final_call:
            logger.warning(
                f"Max steps ({run.max_steps}) reached. Agent may not have completed the task. "
                f"Making one final LLM call to attempt completion."
            )
        # Error-streak bookkeeping (auto-escalation): the streak counts
        # CONSECUTIVE all-error tool steps. Any other step type — text-only,
        # empty response, blocked-tools, cancelled/timeout, or a step where
        # some tool succeeded — breaks the run. Deciding this from the
        # previous step's flag at the top of the loop makes it robust to the
        # many `continue`/`break` paths below (they can't skip a reset here).
        if not run.prev_step_all_errored:
            run.consecutive_tool_error_steps = 0
        run.prev_step_all_errored = False

        # Drain any appended user messages before each step
        run.messages = await self._drain_appended_messages(run.request_id, run.messages)
        # Sync context.messages after draining
        run.context.messages = run.messages

        # Check for cancellation at the start of each step
        if self._is_cancelled(run.request_id):
            async with contextlib.aclosing(self._cancelled_events(run, step)) as events:
                async for event in events:
                    yield event
            st.end = StepEnd.RUN
            return

        # Progress heartbeat using status_coordinator
        await run.status_coordinator.progress(
            "final call after the step budget" if st.final_call else f"step {step + 1}/{run.max_steps}",
            meta={"step": step + 1, "max_steps": run.max_steps}
        )

        # Yield a heartbeat for UI responsiveness (non-blocking)
        yield {"type": "heartbeat", "step": step + 1, "max_steps": run.max_steps}

        # Yield pending status events before LLM call
        for status_event in run.pending_status_events():
            yield status_event

        # Re-render the system message (session-scoped template vars may have
        # changed). Keep it free of per-step values: it is the cached prefix.
        # The step count reaches the model through _step_budget_note.
        updated_system_msg, _ = self._render_prompts(
            run.context.available_tools, run.max_steps, current_step=step + 1,
            session_id=run.context.session_id
        )
        run.messages[0] = ChatMessage(role="system", content=updated_system_msg)

        # Emit thinking event before LLM call (for UI step display)
        yield {"type": "thinking", "step": step + 1}

        # Before the hooks: the input they hand over needs no wake at the end.
        self._presence_step(run.session_id, run.request_id)

    async def _run_pre_llm_hooks(self: Agent, run: LoopState, st: StepState):
        """Phase: the pre-LLM hooks, status events streamed while they run; then the budget note."""
        # Execute pre-LLM hooks with real-time status streaming
        # NOTE: Hooks execute synchronously from this generator's perspective,
        # so we use asyncio.create_task() + polling to stream status events
        # during hook execution. This pattern is only needed for hooks that
        # emit status messages (currently only context_summarizer).
        # Other hook types (post_llm, session_start, session_end) don't need
        # this pattern as they don't emit status events.
        try:
            # Create async task for hook execution
            hook_task = asyncio.create_task(
                self._hook_manager.execute_pre_llm_hooks(
                    messages=run.messages,
                    step=st.step,
                    request_id=run.request_id,
                    session_id=run.session_id,
                    llm=st.current_llm,
                    cancellation_token=run.main_token,
                    max_steps=run.max_steps,
                    final_call=st.final_call,
                )
            )

            # Stream status events while hook is running
            # NOTE: this polling loop has no deadline of its own. Each hook
            # is bounded by its per-hook timeout (asyncio.wait_for in
            # hooks/registry.py; a timed-out hook is logged and skipped), so
            # a long-running hook (e.g. context_summarizer) needs a timeout
            # configured high enough for it.
            while not hook_task.done():
                for status_event in run.pending_status_events():
                    yield status_event
                await asyncio.sleep(0.1)  # Poll every 100ms

            # Get hook result
            modified_messages = await hook_task

            # Yield any final status events from hook execution
            for status_event in run.pending_status_events():
                yield status_event

            # Select the message list that will go to the LLM. The
            # selection logic is extracted to ``_select_llm_messages``
            # so it can be unit-tested in isolation (the surrounding
            # step-loop is generator-based and hard to test directly).
            run.messages = self._select_llm_messages(
                pre_hook_messages=run.messages,
                modified_messages=modified_messages,
            )
            run.context.messages = run.messages
            # Whatever a hook staged now stands in `messages`. What is left
            # in the marker is spent, and it is poisonous from here on: the
            # code after tool execution reads a still-set marker as "a tool
            # rewrote the history mid-request" and rebuilds around it,
            # dropping the assistant tool-call message just appended.
            self._session_tracker.clear_compacted_messages(run.session_id)
        except Exception as e:
            logger.warning(f"Pre-LLM hooks failed: {e}", exc_info=True)

        # AFTER the hooks, so the run keeps the last word. Plugins append
        # their blocks in pre_llm_call, and the max-steps request ("answer
        # NOW, do NOT use any tools") only does its job as the last thing
        # the model reads -- a todo list with open items behind it sends
        # the model back to the tools. Outside the try on purpose: a hook
        # chain that fell over must not also cost the run its step budget.
        st.budget_note = self._step_budget_note(st.step, run.max_steps)
        if st.budget_note is not None:
            run.messages.append(st.budget_note)
            run.context.messages = run.messages

    async def _record_answer(self: Agent, run: LoopState, st: StepState) -> None:
        """Phase: the answer of the call becomes the step's assistant message, in the history."""
        # Signal LLM call completion
        await run.status_worker.progress("LLM (chat) response received", meta={"step": st.step + 1})

        llm_out = st.llm_out
        assistant = llm_out.get("assistant", {}) if llm_out else {}

        st.content = assistant.get("content")
        st.tool_calls = assistant.get("tool_calls", [])
        # "length" = the model hit its output cap. With no content that is a
        # TRUNCATION, not an empty answer — see the empty-response guard below.
        st.finish_reason = llm_out.get("finish_reason") if llm_out else None
        # Provider-side encrypted thinking blocks (Gemini 3.x thought_signature
        # via OpenRouter's reasoning_details). MUST be carried through to the
        # next request or upstream returns MALFORMED_FUNCTION_CALL.
        reasoning_details = assistant.get("reasoning_details")
        answering_model = getattr(st.current_llm, "model", None)

        # Create assistant message and add it BEFORE post_llm hooks
        # so message debugger can capture the complete conversation
        # DeepSeek thinking mode: with `tools` in the request, the assistant's
        # reasoning_content MUST be passed back on every subsequent turn
        # (api-docs.deepseek.com/guides/thinking_mode#tool-call). Dropping it
        # here made _postprocess_messages_for_provider send an empty string,
        # so the model lost its chain of thought after every tool call and
        # re-derived it from scratch — reasoning grew with the conversation
        # until it hit the 65536-token cap (measured: 2.6s/103 reasoning
        # tokens on turn 1, 650s/65536 once tool results had accumulated).
        # Agents without tool calls (v4 pipeline) were never affected, which
        # is why this only showed up on the tool-heavy coding agents.
        assistant_msg = ChatMessage(
            role="assistant",
            content=st.content or "",
            tool_calls=history_safe_tool_calls(st.tool_calls) if st.tool_calls else None,
            reasoning_content=assistant.get("reasoning_content"),
            reasoning_details=reasoning_details,
            reasoning_model=(answering_model if reasoning_details
                             and isinstance(answering_model, str) else None),
            # Anthropic thinking blocks (+ the model that signed them).
            # Same contract as reasoning_content above: with tool use they
            # must be echoed back complete and unmodified, so they have to
            # survive on the message.
            thinking_blocks=assistant.get("thinking_blocks"),
            thinking_model=assistant.get("thinking_model"),
            # OpenRouter backend of this turn: the next request pins to it.
            served_by=assistant.get("served_by"),
            # the step as the live events number it (ChatMessage.step)
            step=st.step + 1,
            timestamp=datetime.now(timezone.utc)
        )
        st.assistant_msg = assistant_msg
        run.messages.append(assistant_msg)
        # Only append to context.messages if it's a different list
        if run.context.messages is not run.messages:
            run.context.messages.append(assistant_msg)

    async def _run_post_llm_hooks(self: Agent, run: LoopState, st: StepState):
        """Phase: the post-LLM hooks, which may rewrite the answer and set its content format."""
        assistant_msg = st.assistant_msg
        assert assistant_msg is not None
        # Execute post-LLM hooks to transform the response
        # NOTE: Using same polling pattern as pre_llm_hooks to support
        # future hooks that may emit status messages during execution.
        try:
            # Create async task for hook execution
            hook_task = asyncio.create_task(
                self._hook_manager.execute_post_llm_hooks(
                    messages=run.messages,
                    llm_response=st.llm_out,  # Pass full LLM response including usage data
                    step=st.step,
                    request_id=run.request_id,
                    session_id=run.session_id,
                    # The client that produced this response — after a
                    # fallback switch in the retry loop, not the run's base.
                    llm=st.current_llm,
                    # A continuation on the final call is dropped (answer.py);
                    # the hook reads this before it counts a nudge.
                    max_steps=run.max_steps,
                    final_call=st.final_call,
                )
            )

            # Stream status events while hook is running
            # NOTE: this polling loop has no deadline of its own; each hook
            # is bounded by its per-hook timeout (hooks/registry.py).
            while not hook_task.done():
                for status_event in run.pending_status_events():
                    yield status_event
                await asyncio.sleep(0.1)  # Poll every 100ms

            # Get hook result
            modified_response, st.hook_metadata = await hook_task

            # Yield any final status events
            for status_event in run.pending_status_events():
                yield status_event

            if modified_response is not None:
                # Extract assistant data from modified response
                modified_assistant = modified_response.get("assistant", {})
                new_content = modified_assistant.get("content")
                new_tool_calls = modified_assistant.get("tool_calls")

                # Update content and tool_calls if hooks modified them
                if new_content is not None:
                    st.content = new_content
                    assistant_msg.content = st.content or ""
                if new_tool_calls is not None:
                    st.tool_calls = new_tool_calls
                    assistant_msg.tool_calls = history_safe_tool_calls(st.tool_calls) if st.tool_calls else None

                # Set content_format from hook metadata (e.g., 'html', 'markdown', 'text')
                if "content_format" in st.hook_metadata:
                    assistant_msg.content_format = st.hook_metadata["content_format"]
        except Exception as e:
            logger.warning(f"Post-LLM hooks failed: {e}", exc_info=True)

    async def _show_answer(self: Agent, run: LoopState, st: StepState):
        """Phase: the answer goes out as thinking events, in the format it is delivered in."""
        content, tool_calls = st.content, st.tool_calls
        # The answer goes out as the model wrote it -- Markdown, which the chat and agent-cli draw
        # themselves. A structured run's answer is JSON, and its events say so.
        st.content_format = getattr(st.assistant_msg, 'content_format', 'text')  # Default to 'text' if not set by hooks
        if run.response_format is not None and not tool_calls:
            st.content_format = "json"

        # Emit thinking event with LLM response (for UI to show assistant reasoning)
        yield {"type": "thinking", "step": st.step + 1, "assistant": {"content": content, "tool_calls": tool_calls, "content_format": st.content_format}}

        # Also emit simplified thinking event if we have content and no tool calls (final answer)
        if content and not tool_calls:
            yield {"type": "thinking", "content": content, "content_format": st.content_format}

        # Yield pending status events after LLM response
        for status_event in run.pending_status_events():
            yield status_event

    def _check_truncation(self: Agent, run: LoopState, st: StepState) -> None:
        """Phase: count answers cut off at the output cap in a row, and say so of one with content."""
        # Truncated but NOT empty. The guard below only covers "the model
        # produced nothing at all", so a cut-off answer WITH content fell
        # through as if it were complete — a scene ending mid-sentence, or
        # a tool call whose arguments JSON is half-written (which upstream
        # then rejects on the next turn as invalid_prompt).
        # Deliberately a warning and not an error: an error moves the run
        # onto the fallback profile and discards output that is
        # usually still usable — the same trade-off the incomplete_stream
        # branch settles the same way.
        # "In a row" means answers: one that ended on its own -- a tool call
        # of a model now writing in parts included -- starts the count again.
        if st.finish_reason != "length":
            run.consecutive_cut_off = 0
        if st.finish_reason == "length" and (st.content or st.tool_calls):
            logger.warning(
                "[%s] Answer truncated at the output cap (finish_reason=length, "
                "model=%s, chars=%d, tool_calls=%d) — it is NOT complete. "
                "Raise max_tokens or lower the reasoning level if this recurs.",
                self.name, getattr(st.current_llm, "model", "?"),
                len(st.content or ""), len(st.tool_calls or []),
            )

    async def _handle_empty_answer(self: Agent, run: LoopState, st: StepState):
        """Phase: an answer with neither content nor tool calls -- an error when the output cap
        took it all, a 'Continue' note when they repeat; a valid answer resets the count and is
        saved."""
        messages = run.messages
        context = run.context
        # Infinite loop guard: Track consecutive empty responses FIRST
        # (before checking tool calls, to catch completely empty responses)
        if not st.content and not st.tool_calls:
            run.consecutive_empty_responses += 1

            # Remove the empty assistant message we just added - it serves no purpose
            # and will just accumulate in the session causing validation issues
            if messages and messages[-1].role == "assistant" and not messages[-1].content and not messages[-1].tool_calls:
                messages.pop()
                if context.messages is not messages and context.messages and context.messages[-1].role == "assistant":
                    context.messages.pop()
                logger.debug("Removed empty assistant message from conversation history")

            # Output cap exhausted with nothing to show: the model spent its
            # whole budget (typically on reasoning) and was cut off. This is
            # NOT "the model had nothing to say", and a 'Continue' nudge just
            # replays the same runaway — observed as 4 x ~11 min and ~260k
            # reasoning tokens burned for zero output. Fail fast and say why.
            if st.finish_reason == "length":
                usage = (st.llm_out or {}).get("usage") or {}
                reasoning_tokens = (
                    (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                )
                error_msg = (
                    "LLM hit its output token limit without producing any content "
                    f"(finish_reason=length, completion_tokens="
                    f"{usage.get('completion_tokens', '?')}"
                    + (f", of which reasoning={reasoning_tokens}" if reasoning_tokens else "")
                    + "). The model exhausted its budget before answering — lower the "
                    "thinking/reasoning level, raise max_tokens, or use a different model."
                )
                logger.error(error_msg)
                run.results.setdefault("errors", []).append(error_msg)
                yield {"type": "error", "message": error_msg}
                st.end = StepEnd.RUN
                return

            # Not on the final call: no step follows to read the nudge, and
            # the session would keep it as the conversation's last word.
            if run.consecutive_empty_responses >= run.max_consecutive_empty and not st.final_call:
                logger.warning(f"Empty response #{run.consecutive_empty_responses}: Injecting 'Continue' note to prompt LLM")
                # Instead of breaking, nudge the LLM. The RUN asks for this,
                # not a person -- the note used to arrive as a user message
                # ("mimics the user typing weiter"), and stayed in the
                # session afterwards as if someone had.
                #
                # The role stays `developer` although a prompt ENDING on one
                # cannot be answered (measured 21.09.2026: 6/6 empty, and
                # Google refuses such a request outright). That is a wire
                # problem and it is fixed on the wire -- the stored message
                # keeps saying who spoke, and the client lowers the LAST
                # developer item to the user rung. Doing it here instead
                # would put a user-role loop note back in the transcript,
                # which is what test_agent_step_budget_note forbids.
                continue_message = ChatMessage(role=DEVELOPER, content="Continue with your task.",
                                               timestamp=datetime.now(timezone.utc),
                                               injected_by="agent.empty_response")
                messages.append(continue_message)
                # Don't reset counter - if we get another empty response after this, we'll inject again
                # But cap at a reasonable limit to prevent truly infinite loops
                if run.consecutive_empty_responses >= run.max_consecutive_empty + 3:
                    logger.warning(f"Breaking loop: {run.consecutive_empty_responses} consecutive empty responses even after 'Continue' prompts")
                    error_msg = "LLM returned empty responses repeatedly despite continue prompts"
                    run.results.setdefault("errors", []).append(error_msg)
                    yield {"type": "error", "message": error_msg}
                    st.end = StepEnd.RUN
                    return
                # Continue to next iteration with the injected message
                st.end = StepEnd.NEXT_STEP
                return
        else:
            run.consecutive_empty_responses = 0  # Reset counter

            # Persist session after each valid (non-empty) LLM response to preserve progress on cancellation
            # NOTE: We only persist non-empty responses to avoid accumulating useless empty messages
            await self._persist_conversation(
                run.session_id, messages, to_disk=False,
                note=f"after LLM response (step {st.step})")

    async def _end_step(self: Agent, run: LoopState, st: StepState) -> None:
        """Phase: a step that ended without a ``continue`` -- live state, late messages drained."""
        # Update tracked messages at end of each step (per-session)
        self._set_live_messages(run.session_id, run.messages.copy())

        # Drain any final appended messages before next step
        run.messages = await self._drain_appended_messages(run.request_id, run.messages)
        # Sync context.messages after draining
        run.context.messages = run.messages
