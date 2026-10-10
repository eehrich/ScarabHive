"""A step whose answer calls no tool: is it the run's final answer, or does the run go on?

It goes on when a post-LLM hook asks to continue (agent_continuation), when a user message arrived
while the model wrote (never finalize past fresh input), and when the answer was cut off at the
output cap (output_cap_notes). Otherwise a text answer is the final one -- checked first against the
run's structured output, which is asked for once more when it does not match -- and repeated answers
without a tool call and without text end the run as they are. The final event itself
(_final_answer) is built here for every path that delivers one, the tool step's included.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict

from .....llm import schema_worker
from .....llm.models import ChatMessage
from .....llm.structured_output import STRUCTURED_OUTPUT_INVALID, repair_text
from .state import LoopState, StepEnd, StepState

if TYPE_CHECKING:
    from ...server import Agent

logger = logging.getLogger(__name__)


class AnswerMixin:
    """A step's text answer (see the module docstring).

    Relies on Agent.__init__ for ``name``, and on the other mixins for the drain and the live state
    (live_state.py) and the structured-output check and notes (prompts.py).
    """

    async def _text_step(self: Agent, run: LoopState, st: StepState):
        """Phase: an answer without tool calls -- go on, deliver it, or end the run without one."""
        # No tool calls - check if we should treat this as the final answer
        # Track consecutive responses without tool calls
        run.consecutive_no_tool_calls += 1
        # (prev_step_all_errored stays False → the error streak resets at the
        #  top of the next iteration; handled centrally, see loop top.)

        async with contextlib.aclosing(self._continue_on_hook_signal(run, st)) as events:
            async for event in events:
                yield event
        if st.ended():
            return

        content = st.content
        # A user message may have been injected while the LLM produced this
        # response (mid-run append). Never finalize past fresh user input —
        # continue the loop so the next LLM call reacts to it. The interim
        # content was already surfaced via the thinking events above.
        # On the final call no step is left: the message stays in the
        # history behind the answer, persisted for the session's next run.
        pre_drain_count = len(run.messages)
        run.messages = await self._drain_appended_messages(run.request_id, run.messages)
        if len(run.messages) > pre_drain_count and not st.final_call:
            run.context.messages = run.messages
            self._set_live_messages(run.session_id, run.messages.copy())
            run.consecutive_no_tool_calls = 0
            st.end = StepEnd.NEXT_STEP
            return

        # Cut off at the output cap with no tool call left: not an answer.
        # The call the model was writing is lost, and the text before it --
        # "now the engine, the big file:" -- is an announcement. Measured in a
        # coder session: three calls in a row stopped at 16384 tokens, each
        # ended the run as if that were its reply, and the user had to push
        # three times. The run goes on with a note saying what happened --
        # where the agent opted in (output_cap_notes), not on the final
        # call, and not past that many in a row.
        if (st.finish_reason == "length" and content and content.strip() and not st.final_call
                and run.consecutive_cut_off < run.max_cut_off_notes):
            run.consecutive_cut_off += 1
            usage = (st.llm_out or {}).get("usage") or {}
            run.messages.append(self._output_cap_note(usage.get("completion_tokens")))
            run.context.messages = run.messages
            await run.status_worker.progress(
                "answer cut off at the output limit -- asked to continue in parts",
                meta={"step": st.step + 1, "cut_off": run.consecutive_cut_off})
            run.consecutive_no_tool_calls = 0
            st.end = StepEnd.NEXT_STEP
            return

        # If we have content AND it's not just whitespace, treat as final answer
        if content and content.strip():
            async with contextlib.aclosing(self._deliver_text_answer(run, st)) as events:
                async for event in events:
                    yield event
            return

        # No tool calls AND (no content OR empty content)
        # Check consecutive no-tool-calls limit to avoid infinite loop
        # Not on the final call: an empty answer there is a spent budget,
        # which the error after the loop reports, not an empty success.
        if run.consecutive_no_tool_calls >= run.max_consecutive_no_tools and not st.final_call:
            logger.warning(f"Breaking loop: {run.consecutive_no_tool_calls} consecutive responses without tool calls (empty or no content)")
            if run.response_format is not None:
                # An empty answer is no JSON: not a final one for a structured run.
                error_msg = self._structured_output_failure(
                    schema_worker.parse_answer(content)[2], corrected=run.format_repaired)
                run.results.setdefault("errors", []).append(error_msg)
                yield {"type": "error", "message": error_msg, "error_type": STRUCTURED_OUTPUT_INVALID}
                st.end = StepEnd.RUN
                return
            # Treat whatever content we have as final (even if empty)
            yield self._final_answer(run, st, content or "")
            st.end = StepEnd.RUN
            return

        if st.final_call and not (content and content.strip()):
            # A blank answer to the final call is no answer: the error after
            # the loop reports the spent budget, and the session must not
            # end on a blank assistant turn. Drained input may follow it.
            for history in {id(run.messages): run.messages, id(run.context.messages): run.context.messages}.values():
                for i in range(len(history or []) - 1, -1, -1):
                    if history[i] is st.assistant_msg:
                        del history[i]
                        break

    async def _continue_on_hook_signal(self: Agent, run: LoopState, st: StepState):
        """A text answer a post-LLM hook does not take as the final one: its scripted turn follows."""
        content = st.content
        hook_metadata = st.hook_metadata
        # === CONTINUATION HOOK SIGNAL ===
        # A post_llm_call hook (e.g. agent_continuation) may set
        # metadata["continue"] = True to prevent treating a text-only
        # response as the final answer.  This allows autonomous agents
        # to keep working when they emit intermediate status reports.
        # Not on the final call: no step is left to continue in.
        if hook_metadata.get("continue") and content and content.strip() and not st.final_call:
            cont_count = hook_metadata.get("continuation_count", "?")
            cont_reason = hook_metadata.get("continuation_reason", "hook signal")
            logger.info(
                f"[{self.name}] Continuation #{cont_count} at step {st.step}: {cont_reason}"
            )
            # Stays a `user` turn, unlike the four notes the loop writes
            # itself. What a hook puts here is a SCRIPTED TURN -- the
            # `followups:` list in an agent's YAML is written to be said to
            # the agent, the way a person would say it -- and two readers
            # take it as one: v4's prose recovery reads the follow-up back
            # out of the stored transcript and matches it by its configured
            # TEXT (the info tool hands out no injected_by), and the
            # watchdog's judge needs it in the picture. As a volatile note
            # it would be neither a user turn nor stored at all.
            continuation_msg = ChatMessage(
                role="user",
                content=hook_metadata.get(
                    "continue_message",
                    "Continue with your task.",
                ),
                timestamp=datetime.now(timezone.utc),
                # Marks the message as not typed by a person; a hook that
                # scripts several turns counts its own messages by this.
                # A hook that names no marker still gets one: unmarked, the
                # nudge would count as a human turn.
                injected_by=hook_metadata.get("continue_injected_by") or "post_llm_call_hook",
            )
            run.messages.append(continuation_msg)
            run.context.messages = run.messages
            await run.status_worker.progress(
                f"Auto-continue #{cont_count}: {cont_reason}",
                meta={"step": st.step + 1, "continuation": True},
            )
            # Yield event so frontend can display the injected message
            yield {
                "type": "continuation",
                "message": continuation_msg.content,
                "count": cont_count,
                "reason": cont_reason,
                "step": st.step + 1,
            }
            run.consecutive_no_tool_calls = 0  # Reset — hook evaluated this
            st.end = StepEnd.NEXT_STEP
        elif hook_metadata.get("continue") and content and content.strip():
            # final_call: the hook asked for a step that does not exist.
            logger.info(
                f"[{self.name}] Continuation from "
                f"{hook_metadata.get('continue_injected_by') or 'post_llm_call_hook'} "
                f"dropped: final call after the step budget"
            )

    async def _deliver_text_answer(self: Agent, run: LoopState, st: StepState, *,
                                   log_unavailable: bool = True):
        """A text answer as the run's final one: checked against the structured output first, and
        asked for once more when it does not match (not on the final call). Ends the step.

        *log_unavailable*: whether a checker that could not look at the answer is logged too; the
        tool step's final call, which delivers through here as well, has never logged it.
        """
        assistant_msg = st.assistant_msg
        assert assistant_msg is not None
        if run.response_format is not None:
            checked = await self._check_structured_answer(st.content, run.response_format, run.request_id)
            if checked.checker_failed:
                event = self._structured_output_unavailable(checked)
                if log_unavailable:
                    logger.warning("[%s] %s", self.name, event["message"])
                run.results.setdefault("errors", []).append(event["message"])
                yield event
                st.end = StepEnd.RUN
                return
            if not checked.ok and not checked.schema_failed and not run.format_repaired and not st.final_call:
                # Once: the model sees its answer and what is wrong with it, and
                # writes it again. Appended like every loop note, so the cached
                # prefix stays; its answer stays too, the note refers to it.
                run.format_repaired = True
                logger.warning("[%s] Final answer does not match the requested format, asking "
                               "once for a correction: %s", self.name, "; ".join(checked.errors))
                run.messages.append(self._structured_output_note(
                    repair_text(checked.errors), "agent.structured_output_repair"))
                run.context.messages = run.messages
                self._set_live_messages(run.session_id, run.messages.copy())
                await run.status_worker.progress(
                    "final answer does not match the JSON format -- asked once to correct it",
                    meta={"step": st.step + 1})
                run.consecutive_no_tool_calls = 0
                st.end = StepEnd.NEXT_STEP
                return
            if not checked.ok:
                error_msg = self._structured_output_failure(checked.errors, corrected=run.format_repaired)
                logger.warning("[%s] %s", self.name, error_msg)
                run.results.setdefault("errors", []).append(error_msg)
                yield {"type": "error", "message": error_msg, "error_type": STRUCTURED_OUTPUT_INVALID}
                st.end = StepEnd.RUN
                return
            # Delivered as checked: a fence around the whole answer is gone, in the
            # session too -- the caller parses what the session keeps (openai_api).
            st.content = assistant_msg.content = checked.text
            st.content_format = assistant_msg.content_format = "json"
        yield self._final_answer(run, st, st.content)
        st.end = StepEnd.RUN

    def _final_answer(self: Agent, run: LoopState, st: StepState, summary: Any) -> Dict[str, Any]:
        """The final event of the run, with *summary* in its results and the live state updated."""
        # Assistant message was already added above before post_llm hooks
        run.results["summary"] = summary
        # Update tracked messages with final response
        self._set_live_messages(run.session_id, run.messages.copy())

        final_event = {"type": "final", "summary": summary, "content_format": st.content_format}
        # Include usage data if available from last LLM call
        if st.llm_out and "usage" in st.llm_out:
            final_event["usage"] = st.llm_out["usage"]
        return final_event
