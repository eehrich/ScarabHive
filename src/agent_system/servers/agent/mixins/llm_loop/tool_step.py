"""A step whose answer calls tools: loop detection, the tools, and the history after them.

The calls are checked against the run's tool-call loop detector first (an intervention note, an
escalation window, calls blocked); the rest run through the tool execution manager, their events
streamed as they come. A step whose calls all failed counts towards the stuck escalation. The
results then join the history -- or a tool replaced the history (set_compacted_messages) and it is
rebuilt around them -- and the turn is saved. On the final call the tools run once and the run ends
with an error, since no step follows to read their results.
"""
from __future__ import annotations

import contextlib
import functools
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .....llm.message_roles import DEVELOPER, leading_instructions, role_of
from .....llm.models import ChatMessage
from .....utils.json_utils import history_safe_tool_calls
from ...components.tool_execution import tool_message_never_ran
from .state import LoopState, StepEnd, StepState

if TYPE_CHECKING:
    from ...server import Agent

logger = logging.getLogger(__name__)


def _instruction_head(messages: list, rebuilt: list) -> list:
    """The leading instruction block, minus whatever ``rebuilt`` already has.

    Both history rebuilds below are "the head + the conversation a hook handed
    back", and what that hook hands back differs per hook. context_engineer
    strips the prompts and returns only its own compaction system messages
    (the archive pointers, the prune breadcrumb); context_summarizer returns
    the WHOLE list, prompts included, and says so in a comment. So there is no
    fixed set to subtract -- the only rule that holds for both is: do not put
    back what is already there.

    Prepending blindly sent the breadcrumb twice on one hook and both prompts
    twice on the other, and the persist right after wrote the copies to disk;
    the next prune then read a doubled total. The version before that took
    ``messages[0]`` alone, which duplicated the first prompt and dropped the
    second one entirely.
    """
    def key(msg):
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        return role_of(msg), content if isinstance(content, str) else repr(content)

    # id() as well as the key: a hook usually hands the SAME objects back, and
    # two different prompts could in principle share a text.
    seen_ids = {id(msg) for msg in rebuilt}
    seen = {key(msg) for msg in rebuilt}
    return [msg for msg in leading_instructions(messages)
            if id(msg) not in seen_ids and key(msg) not in seen]


class ToolStepMixin:
    """A step's tool calls (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``_tool_execution_manager`` and ``_session_tracker``, and
    on the other mixins for the tool user (access.py), the sub-agents' profile and the error check
    (llm_selection.py), the live state (live_state.py) and the saves (persistence.py).
    """

    async def _tool_step(self: Agent, run: LoopState, st: StepState):
        """Phase: an answer with tool calls. Ends the step: on to the next one, which reads the
        results, or -- after the final call -- the end of the run."""
        # Reset no-tool-calls counter
        run.consecutive_no_tool_calls = 0

        # ===== TOOL CALL LOOP DETECTION =====
        async with contextlib.aclosing(self._police_tool_loop(run, st)) as events:
            async for event in events:
                yield event
        if st.ended():
            return

        # Signal tool execution start
        await run.status_worker.progress(f"Executing Tools ({len(st.tool_calls)} total)", meta={"step": st.step + 1})

        # Assistant message with tool calls was already added above before post_llm hooks

        # Update tracked messages
        self._set_live_messages(run.session_id, run.messages.copy())

        async with contextlib.aclosing(self._run_tool_calls(run, st)) as events:
            async for event in events:
                yield event

        # Add tool results to the results dictionary
        run.results["calls"].extend(st.tool_results)

        await self._count_all_error_step(run, st)

        self._merge_tool_results(run, st)

        # Update tracked messages after tool execution
        self._set_live_messages(run.session_id, run.messages.copy())

        # Persist session after complete turn (tool calls + results processed).
        # Saving only at the turn boundary (not after each tool) avoids
        # orphaned tool calls; the disk save preserves progress even if
        # no SSE client is connected.
        await self._persist_conversation(
            run.session_id, run.messages, to_disk=True,
            note=f"after completing turn (step {st.step})")

        # Yield pending status events after tool execution
        for status_event in run.pending_status_events():
            yield status_event

        if st.final_call:
            async with contextlib.aclosing(self._end_after_final_tools(run, st)) as events:
                async for event in events:
                    yield event
            return

        # Continue to next iteration to let LLM respond to tool results
        st.end = StepEnd.NEXT_STEP

    async def _police_tool_loop(self: Agent, run: LoopState, st: StepState):
        """Check the calls for repeated patterns that say the agent is stuck: an intervention note,
        an escalation window, and the calls of a blocked tool dropped. Ends the step when none is
        left to run."""
        assistant_msg = st.assistant_msg
        assert assistant_msg is not None
        content = st.content
        # Check for repeated tool call patterns that indicate the agent is stuck
        loop_result = run.loop_detector.record_batch_and_check(st.tool_calls, st.step)

        st.pending_intervention_msg = None

        if loop_result.is_loop:
            # Log the detection
            logger.warning(
                f"[{self.name}] Tool loop detected at step {st.step}: "
                f"type={loop_result.loop_type}, tool={loop_result.tool_name}, "
                f"count={loop_result.repetition_count}"
            )

            # Prepare intervention message to nudge the LLM
            st.pending_intervention_msg = ChatMessage(
                role=DEVELOPER,
                content=loop_result.intervention,
                timestamp=datetime.now(timezone.utc),
                injected_by="agent.loop_intervention",
            )

            # Emit status event for visibility
            await run.status_worker.progress(
                f"Loop detected: {loop_result.tool_name} ({loop_result.repetition_count}x)",
                meta={"step": st.step + 1, "loop_type": loop_result.loop_type}
            )

            # Objective stuck signal → open an escalation window so the
            # NEXT few steps run on the advanced model (budget permitting).
            # No step follows the final call to run on it.
            esc_reason = None if st.final_call else run.escalator.trigger(
                f"tool-call loop ({loop_result.tool_name})")
            if esc_reason:
                logger.warning(
                    "[%s] auto-escalating to advanced model at step %d: %s "
                    "(budget used %d/%d)", self.name, st.step + 1, esc_reason,
                    run.escalator.calls_used, run.escalator.max_calls)
                await run.status_worker.progress(
                    f"auto-escalating to advanced model ({esc_reason})",
                    meta={"step": st.step + 1})

            # If tool should be blocked, filter it out
            if loop_result.should_block_tool:
                blocked_tools = loop_result.blocked_tools
                original_count = len(st.tool_calls)
                st.tool_calls = [
                    tc for tc in st.tool_calls
                    if tc.get("function", {}).get("name") not in blocked_tools
                ]
                tool_calls = st.tool_calls
                if len(tool_calls) < original_count:
                    logger.warning(
                        f"[{self.name}] Blocked {original_count - len(tool_calls)} tool calls "
                        f"due to loop detection. Blocked tools: {blocked_tools}"
                    )
                    if st.final_call:
                        # A blocked call gets no result. After a step the
                        # next call's message_validator drops it from the
                        # history; after the final call no call follows,
                        # and the session would keep it unanswered.
                        assistant_msg.tool_calls = (
                            history_safe_tool_calls(tool_calls) if tool_calls else None)
                        messages, context = run.messages, run.context
                        if (not tool_calls and not (content and content.strip())
                                and messages[-1] is assistant_msg):
                            messages.pop()
                            if (context.messages is not messages and context.messages
                                    and context.messages[-1] is assistant_msg):
                                context.messages.pop()
                    # If all tools were blocked, continue to next iteration
                    # The intervention message will prompt the LLM to try something else
                    if not tool_calls:
                        # No tool results will follow, so inject now to keep the loop warning.
                        # Not after the final call, like the empty-response nudge.
                        if not st.final_call:
                            run.messages.append(st.pending_intervention_msg)
                            run.context.messages = run.messages
                        elif content and content.strip():
                            # What is left is a text answer on the final
                            # call: delivered like the no-tool answer below.
                            # No step is left to ask for a correction in.
                            async with contextlib.aclosing(self._deliver_text_answer(run, st, log_unavailable=False)) as events:
                                async for event in events:
                                    yield event
                            return
                        st.end = StepEnd.NEXT_STEP

    async def _run_tool_calls(self: Agent, run: LoopState, st: StepState):
        """Run the step's tool calls, their events streamed as they come; the tool messages and
        results land on the step."""
        # Execute all tools using streaming to get real-time status events from sub-agents
        st.tool_messages = []
        st.tool_results = []

        user_id = self.tool_user(run.request_id, run.session_id)

        # CRITICAL: Pass per-request status_forwarder as parameter to avoid race conditions
        # when multiple requests share the same agent instance (e.g., parent + sub-agent)
        # DO NOT set self._tool_execution_manager._status_forwarder - that causes race conditions!

        context = run.context
        async with contextlib.aclosing(self._tool_execution_manager.execute_tools_streaming(
                tool_calls=st.tool_calls,
                tool_name_mapping=run.tool_name_mapping,
                available_tools=context.available_tools,
                step=st.step,
                request_id=run.request_id,
                session_id=run.session_id,
                user_id=user_id,
                status_forwarder=context.status_forwarder,
                assistant_message=st.assistant_msg,
                # What this run was switched to, for the sub-agents its
                # tools start (agent_config.inherit_parent_llm).
                llm_profile=self._profile_to_hand_down(run.llm_override),
                intercept=(functools.partial(context.deferred_tools.intercept,
                                             tools_schema=run.tools_schema)
                           if context.deferred_tools is not None else None),
            )) as items:
            async for item in items:
                if item.get("type") == "status":
                    # Yield status events in real-time during tool execution
                    yield item["event"]
                elif item.get("type") == "tool_events":
                    # Yield tool execution events
                    for event in item["events"]:
                        yield event
                elif item.get("type") == "complete":
                    # Store final results
                    st.tool_messages = item["messages"]
                    st.tool_results = item["results"]

    async def _count_all_error_step(self: Agent, run: LoopState, st: StepState) -> None:
        """The stuck signal of failing tools: N steps in a row whose calls all failed open an
        escalation window."""
        # Stuck signal: a step whose tool calls ALL returned an error.
        # Catches the near-loops the exact-match detector misses (same
        # tool retried with slightly varied wrong args). N in a row →
        # open an escalation window. Only calls that ran count: a call a
        # hook blocked (a policy, a person saying no) is no sign that a
        # stronger model is needed, nor is a deferred tool called before
        # it was loaded -- a step of nothing but such calls neither grows
        # the streak nor breaks it.
        tool_messages = st.tool_messages
        ran_messages = [m for m in tool_messages if not tool_message_never_ran(m)]
        if tool_messages and not ran_messages:
            run.prev_step_all_errored = True
        elif ran_messages and all(self._tool_message_is_error(m) for m in ran_messages):
            run.consecutive_tool_error_steps += 1
            run.prev_step_all_errored = True  # keep the streak alive next step
            if run.consecutive_tool_error_steps >= run.escalate_error_streak and not st.final_call:
                esc_reason = run.escalator.trigger(
                    f"{run.consecutive_tool_error_steps} all-error tool steps")
                if esc_reason:
                    logger.warning(
                        "[%s] auto-escalating to advanced model at step %d: "
                        "%s (budget used %d/%d)", self.name, st.step + 1,
                        esc_reason, run.escalator.calls_used, run.escalator.max_calls)
                    await run.status_worker.progress(
                        f"auto-escalating to advanced model ({esc_reason})",
                        meta={"step": st.step + 1})
        # A non-all-error tool step leaves prev_step_all_errored False,
        # so the streak resets at the top of the next iteration.

    def _merge_tool_results(self: Agent, run: LoopState, st: StepState) -> None:
        """The tool results join the history -- or the history a tool replaced, rebuilt around
        them -- followed by the loop intervention, if any."""
        session_id = run.session_id
        messages = run.messages
        tool_messages = st.tool_messages
        # CRITICAL: Check if ANY tool modified the session messages during execution.
        # Tools can set modified messages via session_tracker.set_compacted_messages()
        # This is a generic mechanism - any tool can use it to replace the message history.
        #
        # Examples of tools that use this:
        # - context_engineer.compact() - Compresses messages to save tokens
        # - context_summarizer - Summarizes old conversation history
        # - Any custom tool that wants to modify conversation state
        #
        # The session tracker stores conversation messages WITHOUT system message.
        compacted_messages = self._session_tracker.get_compacted_messages(session_id)

        if compacted_messages is not None:
            # A tool replaced the message history - use the new messages
            local_conversation = [m for m in messages if m.role != "system"]
            logger.info(
                f"Tool modified session: {len(compacted_messages)} new msgs replacing "
                f"{len(local_conversation)} old msgs. Reconstructing conversation."
            )

            # Reconstruct: the leading instruction block + the modified
            # conversation + tool results. It used to be messages[0]
            # alone, so the tools prompt — a second system message —
            # and anything else standing at the head was dropped.
            messages = (_instruction_head(messages, compacted_messages)
                        + list(compacted_messages) + tool_messages)

            # Clear compacted messages - they've been applied
            self._session_tracker.clear_compacted_messages(session_id)

            # Update session storage to match
            self._session_tracker.set_session_messages(session_id, list(compacted_messages))
        else:
            # Normal case: no tool modified messages, just extend with tool results
            messages.extend(tool_messages)

        if st.pending_intervention_msg is not None and not st.final_call:
            messages.append(st.pending_intervention_msg)

        # Sync context.messages with the updated messages list
        run.messages = messages
        run.context.messages = messages

    async def _end_after_final_tools(self: Agent, run: LoopState, st: StepState):
        """The final call's tools ran: the run ends -- cancelled, or with the error that no step
        read their results."""
        # A step's cancel is reported by the check at the top of the
        # next iteration; after the final call there is none.
        if self._is_cancelled(run.request_id):
            async with contextlib.aclosing(self._cancelled_events(run, st.step)) as events:
                async for event in events:
                    yield event
            st.end = StepEnd.RUN
            return
        # Tools on the final call run once: an agent that delivers
        # through a tool (the v6 auditors) would otherwise lose the
        # delivery its whole run was for. The budget stays a cap --
        # no call follows to read the results -- and the run says so.
        ran = ", ".join(
            f"{m.name} ({'error' if self._tool_message_is_error(m) else 'ok'})"
            for m in st.tool_messages) or "none"
        logger.warning(
            f"[{self.name}] Agent returned tool calls after max_steps limit and they "
            f"ran without a further LLM call: {ran}. Increase max_steps or simplify the task."
        )
        # No final event: callers that read only final take it as
        # success (the writer dispatch records ok=True, the job
        # manager marks the run answered), whatever the tools
        # returned. The results stay in the session.
        error_msg = (f"Agent incomplete: max steps ({run.max_steps}) reached; "
                     f"the final call ran tool calls ({ran}) that no step followed.")
        run.results.setdefault("errors", []).append(error_msg)
        yield {"type": "error", "message": error_msg}
        st.end = StepEnd.RUN
