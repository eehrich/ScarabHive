"""Phase 2 of a run: the step loop (_execute_llm_loop) and the LLM call of one step.

_execute_llm_loop runs the steps of a request: drains appended messages, chooses the step's LLM
(fallbacks, blocks, escalation), calls it (_call_llm_with_streaming, streaming or polled), runs the
hooks around the call and the tool calls the answer asks for, and ends with the final answer, an
error or the max-steps end. Moved out of server.py as they were, with the module-level helpers only
they use; run.py drives them between Phase 1 and Phase 3 (run_phases.py).
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import errno
import functools
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import httpx

from ....core.cancellation import CancellationToken
from ....utils.json_utils import history_safe_tool_calls
from ....utils.reasoning_artifacts import (
    strip_all_reasoning_artifacts,
    strip_foreign_reasoning_artifacts,
)
from ....llm.message_roles import DEVELOPER, leading_instructions, role_of
from ....llm.model_health import model_health
from ....llm.models import ChatMessage, LLMClient, LLMRateLimitError, LLMQuotaExhaustedError, LLMServerError, LLMConnectionError, abandon_report
from ....llm import schema_worker
from ....llm.structured_output import (
    JSON_OBJECT, STRUCTURED_OUTPUT_INVALID, STRUCTURED_OUTPUT_UNAVAILABLE, STRUCTURED_OUTPUT_UNSUPPORTED,
    InvalidResponseFormat, ResponseFormat,
    SchemaCheckerError, instruction_text, prepare_response_format, repair_text,
    supports_response_format, unsupported_message,
)
from ....tools.status import StatusScope
from ..components.tool_execution import tool_message_never_ran
from ..reasoning_loop import ReasoningLoopError, build_detectors
from .run_phases import ConversationContext

if TYPE_CHECKING:
    from ..server import Agent

logger = logging.getLogger(__name__)


#: ``injected_by`` of the note that describes a run's structured output to the model.
FORMAT_NOTE = "agent.structured_output"


# llm_progress hooks fire every this many characters of thinking. A hook sets
# its own, coarser interval on top; this only bounds how often the loop pays
# for a hook dispatch while it streams.
_REASONING_PROGRESS_TICK = 2000

# Local descriptor exhaustion. httpx reports it as a ConnectError, which is
# indistinguishable from an unreachable endpoint unless the cause chain is
# inspected — see _is_local_resource_exhaustion.
_LOCAL_EXHAUSTION_ERRNOS = frozenset({errno.EMFILE, errno.ENFILE})


def _is_local_resource_exhaustion(exc: BaseException) -> bool:
    """Is this transport failure OUR machine running out of descriptors?

    A provider being unreachable and this process being unable to open a socket
    both surface as httpx.ConnectError, but they call for opposite responses:
    the first is what fallback profiles exist for, the second cannot be helped
    by any profile — the next client hits the same wall. On 2026-08-30 a
    descriptor leak made the writer host do exactly that: 52 fallback switches
    onto pricier models, none of which could have succeeded.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, OSError) and cur.errno in _LOCAL_EXHAUSTION_ERRNOS:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _content_filter_as_error(assistant: Any, finish_reason: Optional[str], llm: Any) -> None:
    """Mark a provider content-filter stop as an upstream error, in place.

    The one rule for both paths of _call_llm_with_streaming. The filter is
    deterministic per content: nudging the same model with "Continue" asks
    the same question again, and a partial answer it cut is not a complete
    one -- moving on is exactly what the fallback chain is for, and the
    "error" key is what engages it.

    Left alone: an answer that already carries an error, and one that carries
    tool calls. Gemini reports a non-standard finish_reason while STILL
    returning usable tool calls; httpx _format_response keeps those, and
    erroring here would throw away a perfectly good turn.
    """
    if (finish_reason != "content_filter" or not isinstance(assistant, dict)
            or "error" in assistant or assistant.get("tool_calls")):
        return
    assistant["error"] = {
        "message": (f"Provider content filter blocked the response "
                    f"(model={getattr(llm, 'model', '?')})"),
        "type": "content_filter",
    }


def _name_the_model(event: dict, llm: Any) -> dict:
    """Name on a thinking_complete the model that ran the call, in place.

    A fallback or a walk around a blocked LLM runs on a client the caller never
    handed in, and a surface pricing the call needs its model.
    """
    model = getattr(llm, "model", None)
    if isinstance(model, str) and model:
        event["model"] = model
        # Who answered, not what the client is: a batch client's sync fallback bills in full.
        event["batch"] = getattr(llm, "last_was_batch", None) is True
    return event


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


class LLMLoopMixin:
    """The step loop and the LLM call of a step (see the module docstring).

    Relies on nearly everything Agent.__init__ sets up (``llm``, ``agent_config``, ``system_config``,
    ``timeouts``, ``llm_profile_info``, ``_reasoning_loop_config``, the session tracker, the hook,
    tool execution and request managers, ``_step_llms``) and on the methods of the other mixins:
    the step's LLM (llm_selection.py), its prompts and notes (prompts.py), the live state
    (live_state.py), the saves (persistence.py) and the presence step (run.py).
    """

    async def _call_llm_with_streaming(
        self: Agent,
        llm: LLMClient,
        messages: List[ChatMessage],
        tools_schema: List[Dict[str, Any]],
        cancellation_token: CancellationToken,
        step: int,
        yield_pending_status_fn,
        status_scope: Optional[StatusScope] = None,
        watch_reasoning: bool = True,
        on_reasoning_progress=None,
        response_format: Optional[ResponseFormat] = None,
    ):
        """Call LLM with streaming support and interleaved status events.

        This method uses chat_tools_streaming() when available, yielding token deltas
        and checking status events between chunks. For non-streaming LLMs, falls back
        to regular chat_tools() with periodic status polling.

        Args:
            llm: LLM client instance
            messages: Conversation messages
            tools_schema: Available tools schema
            cancellation_token: Cancellation token for interruption
            step: Current step number
            yield_pending_status_fn: Function that yields pending status events
            status_scope: Optional status scope for LLM to report progress (batch status, etc.)
            on_reasoning_progress: Optional ``async (text, chars, previous_chars)``,
                awaited every _REASONING_PROGRESS_TICK characters of thinking
            response_format: The structured output to put on the wire, only ever one the LLM said it
                takes. Handed on only when set: a call without one is the call it always was, and a
                client that never wired the keyword is never given it.

        Yields:
            - {"type": "thinking_delta", "step": int, "delta": str, "accumulated": str}
            - {"type": "status", ...}
            - {"type": "thinking_complete", "assistant": {...}}
        """
        wire_format = {"response_format": response_format} if response_format is not None else {}
        if llm.supports_streaming():
            # Streaming LLM: zero-overhead real-time tokens
            accumulated_content = []
            final_assistant = None
            final_usage = None  # Store usage data from final chunk
            final_finish_reason = None  # "length", "content_filter", ...
            # Detectors per CALL: they hold this call's thinking, and a retry
            # must start from empty windows. Two of them — a short window for
            # short-period loops and a long one for the periods the short
            # window cannot span; see build_detectors.
            reasoning_detectors = build_detectors(
                # Off for the retry the detector itself asked for: watching
                # the second attempt too would mean a second abort policy,
                # and there is nothing sensible left to do after it.
                enabled=bool(self._reasoning_loop_config["enabled"]) and watch_reasoning,
                repetition_threshold=float(
                    self._reasoning_loop_config["repetition_threshold"]))
            # Thinking of THIS call, for llm_progress hooks; a retry starts empty.
            reasoning_parts: List[str] = []
            reasoning_chars = 0
            reasoning_ticked_at = 0

            # Closed when the loop is left early (a ReasoningLoopError, a cancel):
            # the client reports that call's end at once, not when GC finds it.
            async with contextlib.aclosing(llm.chat_tools_streaming(
                messages, tools_schema,
                cancellation_token=cancellation_token,
                status_scope=status_scope,
                **wire_format,
            )) as stream:
                async for chunk in stream:
                    chunk_type = chunk.get("type")

                    if chunk_type == "thinking_delta":
                        # Gemini reasoning/thinking tokens (not content)
                        yield {"type": "reasoning_delta", "step": step + 1, "delta": chunk["delta"]}

                        # Watch the thinking for a loop. What arrives here is
                        # whatever the client calls a thinking delta: raw
                        # reasoning for most models, and for the OpenAI family a
                        # SUMMARY of it — the threshold was calibrated on raw
                        # reasoning, so for those models this guards the
                        # degenerate case rather than measuring a known shape.
                        for _detector in reasoning_detectors:
                            loop_reason = _detector.record(chunk["delta"])
                            if loop_reason:
                                logger.warning(
                                    "[%s] Aborting the call: %s (model=%s, %d characters "
                                    "of thinking so far)",
                                    self.name, loop_reason, getattr(llm, "model", "?"),
                                    _detector.characters_seen)
                                # The client reports the stream it is left with;
                                # its row takes the reason (see the handler).
                                abandon_report.set({"reported": False, "fields": {
                                    "error": f"reasoning loop aborted: {loop_reason}",
                                    "finish_reason": "reasoning_loop_aborted",
                                    "response_data": {"reasoning_loop": {
                                        "characters": _detector.characters_seen,
                                        "reason": loop_reason}}}})
                                raise ReasoningLoopError(
                                    loop_reason,
                                    characters=_detector.characters_seen)

                        if on_reasoning_progress is not None:
                            reasoning_parts.append(chunk["delta"])
                            reasoning_chars += len(chunk["delta"])
                            if reasoning_chars - reasoning_ticked_at >= _REASONING_PROGRESS_TICK:
                                try:
                                    await on_reasoning_progress(
                                        "".join(reasoning_parts), reasoning_chars,
                                        reasoning_ticked_at)
                                except Exception as exc:  # an observer never breaks the call
                                    logger.warning("[%s] llm_progress hooks failed: %s",
                                                   self.name, exc)
                                reasoning_ticked_at = reasoning_chars

                        # Check status events after each token (zero overhead)
                        for status_event in yield_pending_status_fn():
                            yield status_event

                    elif chunk_type == "content_delta":
                        # Yield token delta for real-time display
                        yield {"type": "thinking_delta", "step": step + 1, "delta": chunk["delta"], "accumulated": chunk["accumulated"]}
                        accumulated_content.append(chunk["delta"])

                        # Check status events after each token (zero overhead)
                        for status_event in yield_pending_status_fn():
                            yield status_event

                    elif chunk_type == "stream_restart":
                        # The client retries from scratch after deltas went out:
                        # this call's buffers and loop windows start over, and the
                        # chat empties the step's thinking (the answer re-renders
                        # from `accumulated` by itself).
                        accumulated_content = []
                        reasoning_detectors = build_detectors(
                            enabled=bool(self._reasoning_loop_config["enabled"]) and watch_reasoning,
                            repetition_threshold=float(
                                self._reasoning_loop_config["repetition_threshold"]))
                        reasoning_parts, reasoning_chars, reasoning_ticked_at = [], 0, 0
                        yield {"type": "reasoning_reset", "step": step + 1}

                    elif chunk_type == "tool_call_delta":
                        # Tool calls are accumulated server-side, we can skip yielding deltas for now
                        # Future: could yield tool_call_delta events for UI to show "Calling get_weather..."
                        # Still yield status events to prevent delays
                        for status_event in yield_pending_status_fn():
                            yield status_event

                    elif chunk_type == "final":
                        final_assistant = chunk["assistant"]
                        # Preserve usage data from final chunk
                        if "usage" in chunk:
                            final_usage = chunk["usage"]
                        if chunk.get("finish_reason"):
                            final_finish_reason = chunk["finish_reason"]

            # Yield any remaining status events after streaming completes
            for status_event in yield_pending_status_fn():
                yield status_event

            # The streaming assembler builds its assistant dict itself and
            # never produces the "error" key that httpx _format_response sets
            # for a content filter -- so the fallback-profile switch was
            # unreachable while streaming. A filter is deterministic per
            # content: asking the same model again repeats the refusal, so it is
            # an error. A missing [DONE] (below) is a hiccup, so it is not.
            _content_filter_as_error(final_assistant, final_finish_reason, llm)
            if final_assistant is not None and "error" not in final_assistant:
                _model = getattr(llm, "model", "?")
                if final_finish_reason == "incomplete_stream":
                    # Deliberately NOT an error, not even when empty. An error
                    # here moves the rest of the run onto the fallback profile
                    # -- and for an agent with no fallback chain it ends the run
                    # outright, where the existing empty-response guard would
                    # simply have retried. Far too heavy a hammer for what is
                    # usually a transient network hiccup. Say it and move on.
                    logger.warning(
                        "[%s] Stream ended without a [DONE] marker; the answer may be "
                        "truncated (model=%s, chars=%d, tool_calls=%d)",
                        self.name, _model, len(final_assistant.get("content") or ""),
                        len(final_assistant.get("tool_calls") or []),
                    )

            # Yield final response with usage data
            if final_assistant:
                result = {"type": "thinking_complete", "step": step + 1, "assistant": final_assistant}
                if final_usage:
                    result["usage"] = final_usage
                # Without carrying it here the truncation guard below never
                # sees a "length" and silently accepts a cut-off answer.
                if final_finish_reason:
                    result["finish_reason"] = final_finish_reason
                yield result
            else:
                yield {"type": "thinking_complete", "step": step + 1, "assistant": {"role": "assistant", "content": "".join(accumulated_content)}}

        else:
            # Non-streaming LLM: Use polling with 100ms intervals
            # Create task for LLM call
            llm_task = asyncio.create_task(llm.chat_tools(
                messages, tools_schema, 
                cancellation_token=cancellation_token,
                status_scope=status_scope,
                **wire_format,
            ))

            # Poll for status events while waiting
            # For batch mode: The batch queue manager has its own timeout (max_wait_hours in llm.yaml)
            # so we don't need an agent-side timeout. We use a very high limit (24h) as safety net.
            # For sync mode: The HTTPX client has its own request_timeout.
            # The agent-side limit is just a safety net for truly stuck calls.
            max_llm_iterations = self.timeouts.llm_task_max_iterations if self.timeouts else 864000  # 24h default
            llm_iteration_count = 0
            # Calculate heartbeat interval in iterations (config is in seconds, we poll every 0.1s)
            heartbeat_interval_seconds = self.system_config.status.llm_heartbeat_interval
            heartbeat_interval = int(heartbeat_interval_seconds / 0.1)  # Convert seconds to iterations
            
            while not llm_task.done():
                llm_iteration_count += 1
                if llm_iteration_count > max_llm_iterations:
                    timeout_seconds = max_llm_iterations * 0.1
                    logger.error(
                        "LLM task polling exceeded max iterations (%d = %.0fs), forcing exit",
                        max_llm_iterations, timeout_seconds
                    )
                    llm_task.cancel()
                    await asyncio.sleep(0.1)
                    # Raise timeout error so it can be caught and handled appropriately
                    # This is different from CancelledError (user cancellation)
                    raise asyncio.TimeoutError(
                        f"LLM task timed out after {timeout_seconds:.0f} seconds "
                        f"(max_iterations={max_llm_iterations})"
                    )

                # Check for status events
                for status_event in yield_pending_status_fn():
                    yield status_event

                # Send heartbeat event periodically to prevent SSE connection timeout
                # This keeps the connection alive during long LLM calls (30+ seconds)
                if llm_iteration_count % heartbeat_interval == 0:
                    yield {
                        "type": "heartbeat",
                        "step": step + 1,
                        "timestamp": asyncio.get_event_loop().time()
                    }

                # Wait 100ms before next poll
                try:
                    await asyncio.wait_for(asyncio.shield(llm_task), timeout=0.1)
                except asyncio.TimeoutError:
                    pass  # Continue polling

            # Get result
            llm_out = await llm_task
            result = {"type": "thinking_complete", "step": step + 1, "assistant": llm_out.get("assistant", {})}
            # Preserve usage data if present
            if "usage" in llm_out:
                result["usage"] = llm_out["usage"]
            if llm_out.get("finish_reason"):
                result["finish_reason"] = llm_out["finish_reason"]
            # Not every blocking client turns the filter into an error itself
            # (openai_responses only reports the reason).
            _content_filter_as_error(result["assistant"], llm_out.get("finish_reason"), llm)
            yield result

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

        # Request-LOCAL display label: the agent's configured profile at request
        # start, or the last in-run swap of the base -- not re-derived per step
        # (a 5xx run keeps showing the fallback label until the request ends).
        # A step that walks around a blocked LLM is labelled by the pick.
        display_profile_info = self.llm_profile_info
        # The longest block of an LLM this agent sets; a quota or a refused key
        # blocks this long at once.
        max_block_seconds = float(self.agent_config.fallback_recovery_seconds
                                  if self.agent_config else 3600)
        # The profile a request-scoped fallback swap made this run's base
        # (active_llm): later steps leave it out of their fallback chain, or the
        # fallback that fails next would be retried as its own fallback.
        base_profile: Optional[str] = None

        # Extract from context
        messages = context.messages
        tools_schema = context.tools_schema
        tool_name_mapping = context.tool_name_mapping
        max_steps = context.max_steps
        main_token = context.main_token

        # Initialize results
        results: Dict[str, Any] = {"task": "", "calls": []}

        # Add safeguards against infinite loops
        consecutive_no_tool_calls = 0
        consecutive_empty_responses = 0
        consecutive_tool_error_steps = 0  # steps whose tool calls ALL errored (stuck signal)
        prev_step_all_errored = False     # was the IMMEDIATELY preceding step an all-error tool step?
        max_consecutive_no_tools = 3  # Break after 3 consecutive responses without tool calls
        max_consecutive_empty = 2    # Break after 2 consecutive empty responses
        # Text answers cut off at the output cap in a row, each sent back with a
        # note (see _output_cap_note) -- only where the agent opted in
        # (agent_config.output_cap_notes): for an agent whose product is its
        # text the cut-off text is still the reply, and a model that loops
        # until a 120k cap must not be sent back for two more rounds of it.
        consecutive_cut_off = 0
        max_cut_off_notes = int(getattr(self.agent_config, "output_cap_notes", 0) or 0)
        # Structured output (response_format): a final answer that does not match is sent back
        # once. The note that describes the format to a model without the field is looked for
        # before every call, not remembered: a compaction may have taken it out of the history.
        format_repaired = False

        def _takes_format(client: Any) -> bool:
            """Whether *client* may answer a step of this run. Any client, when the run has no
            format or allows the prompt fallback; else only one that puts the field on the wire.
            Asked where the run CHOOSES another model -- an escalation, a walk around a blocked
            LLM, a failover: that choice must not end the run over a format the chosen model
            cannot take while another one could."""
            return (response_format is None or response_format.prompt_fallback
                    or supports_response_format(client, response_format))

        # A format nobody checked yet (a caller that built it itself; openai_api prepares its own):
        # its schema goes through the worker's subset before anything runs, and the run works with
        # what the worker made of it.
        if response_format is not None and not response_format.checked:
            from ....core.request_context import get_request_user

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
        if not _takes_format(active_llm):
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

        # Helper function to yield any pending status events from per-request forwarder
        def yield_pending_status_events():
            for event in context.status_forwarder.get_pending_events():
                yield event

        async def cancelled_events(step):
            logger.info("Request %s cancelled at step %d", request_id, step + 1)
            # Signal cancellation using status contexts FIRST (so events are queued)
            await status_worker.error(f"cancelled at step {step + 1}",
                                      meta={"step": step + 1, "reason": "cancelled"})
            await status_coordinator.error(f"cancelled at step {step + 1}",
                                           meta={"step": step + 1, "reason": "cancelled"})
            # Give status events a moment to be captured by forwarder
            await asyncio.sleep(0.01)
            # Yield all pending status events before cancelled event
            for status_event in context.status_forwarder.get_pending_events():
                yield status_event
            yield {"type": "cancelled", "request_id": request_id, "step": step + 1}

        # One iteration past the budget: the final call. It used to be a bare
        # chat_tools() after the loop, and so it skipped everything a step does
        # -- the pre-LLM hooks (message_validator dropped no orphaned tool call,
        # context_engineer capped nothing), the fallback chain, streaming, the
        # post-LLM hooks. As a step it has all of that; what differs is only
        # that no step follows it (see final_call below).
        for step in range(max_steps + 1):
            final_call = step == max_steps
            if final_call:
                logger.warning(
                    f"Max steps ({max_steps}) reached. Agent may not have completed the task. "
                    f"Making one final LLM call to attempt completion."
                )
            # Error-streak bookkeeping (auto-escalation): the streak counts
            # CONSECUTIVE all-error tool steps. Any other step type — text-only,
            # empty response, blocked-tools, cancelled/timeout, or a step where
            # some tool succeeded — breaks the run. Deciding this from the
            # previous step's flag at the top of the loop makes it robust to the
            # many `continue`/`break` paths below (they can't skip a reset here).
            if not prev_step_all_errored:
                consecutive_tool_error_steps = 0
            prev_step_all_errored = False

            # Drain any appended user messages before each step
            messages = await self._drain_appended_messages(request_id, messages)
            # Sync context.messages after draining
            context.messages = messages

            # Check for cancellation at the start of each step
            if self._is_cancelled(request_id):
                async for event in cancelled_events(step):
                    yield event
                return

            # Progress heartbeat using status_coordinator
            await status_coordinator.progress(
                "final call after the step budget" if final_call else f"step {step + 1}/{max_steps}",
                meta={"step": step + 1, "max_steps": max_steps}
            )

            # Yield a heartbeat for UI responsiveness (non-blocking)
            yield {"type": "heartbeat", "step": step + 1, "max_steps": max_steps}

            # Yield pending status events before LLM call
            for status_event in yield_pending_status_events():
                yield status_event

            # Re-render the system message (session-scoped template vars may have
            # changed). Keep it free of per-step values: it is the cached prefix.
            # The step count reaches the model through _step_budget_note.
            updated_system_msg, _ = self._render_prompts(
                context.available_tools, max_steps, current_step=step + 1, session_id=context.session_id
            )
            messages[0] = ChatMessage(role="system", content=updated_system_msg)

            # Emit thinking event before LLM call (for UI step display)
            yield {"type": "thinking", "step": step + 1}

            # Before the hooks: the input they hand over needs no wake at the end.
            self._presence_step(session_id, request_id)

            # Pick the model that answers this step BEFORE the hooks run: they
            # size the context by context.llm (context_engineer's arrival cap,
            # context_summarizer's trigger, context_usage_tracker's percentage).
            # Picked after them, a fallback with a smaller window was sized by
            # the original's window, and a switch back to a freed LLM stripped
            # the history the hooks had already worked on.

            # Auto-escalation: run this step on the advanced model when a window
            # is open and its LLM is not blocked. The budget round is only spent
            # once it is settled that the advanced client answers.
            # Not the final call: it only asks for the answer, and the old
            # post-loop call never escalated either.
            escalated_this_step = escalator.active and not final_call

            def _pick_step_llm(escalate: bool):
                """(client, fallback chain, escalated, fallback profile or None)
                for this step. Spends no budget.

                The wanted client — the escalation, the override or the run's
                base — when its LLM is not blocked (llm/model_health.py); else
                the first unblocked profile of the chain; else the wanted one
                all the same: a blocked LLM beats no LLM. Coming back once the
                block is lifted is a switch like any other, stripped below.
                """
                llm = active_llm
                # Profil des TATSÄCHLICH aktiven Modells, wenn es vom Config-
                # Primär abweicht: Eskalations-Swap oder explizites Override
                # (llm_profile_info_override = "profil:provider/model"). Wird
                # aus der Fallback-Kette exkludiert, sonst würde das gerade
                # fehlschlagende Modell als sein eigener Fallback erneut laufen.
                active_profile_override = None
                if base_profile is not None:
                    active_profile_override = base_profile
                elif llm_override is not None and llm_profile_info_override:
                    active_profile_override = llm_profile_info_override.split(":", 1)[0]
                if escalate:
                    escalation_llm = self._get_escalation_llm()
                    if escalation_llm is None or not _takes_format(escalation_llm):
                        # Advanced client couldn't be built — ran on standard.
                        # Disable escalation for this run so we don't retry the
                        # build every step (the window would never close).
                        # Same for one that cannot take the run's structured
                        # output: it will not learn to within the run.
                        escalate = False
                        escalator.disable()
                    elif model_health.available(escalation_llm, request_id):
                        llm = escalation_llm
                        active_profile_override = (
                            self.agent_config.advanced_llm_profile
                            if self.agent_config else None
                        )
                    else:
                        # Blocked for now: this step runs on standard, the
                        # window stays open for a step after the block.
                        escalate = False
                # Ketten-Semantik: llm_profile = [primär, fallback1, ...],
                # llm_profile_advanced analog. fallback_chain() liefert die
                # passende Reihenfolge (advanced-Kette zuerst, dann die
                # normale Kette als letztes Sicherheitsnetz).
                profiles = (
                    self.agent_config.fallback_chain(
                        use_advanced_model, exclude=active_profile_override)
                    if self.agent_config else []
                )
                if base_profile is not None and llm_override is not None and llm_profile_info_override:
                    # The override the swap replaced failed too: not a fallback.
                    failed_override = llm_profile_info_override.split(":", 1)[0]
                    profiles = [p for p in profiles if p != failed_override]
                if llm_override is not None and self.agent_config:
                    # On an override the agent's own primary is not the run's
                    # base: fallback_chain() leaves it out as the active model,
                    # yet it is the first fallback -- with a one-entry chain the
                    # only one.
                    own_primary = (self.agent_config.advanced_llm_profile
                                   if use_advanced_model and self.agent_config.advanced_llm_profile
                                   else self.agent_config.default_llm_profile)
                    override_profile = (llm_profile_info_override.split(":", 1)[0]
                                        if llm_profile_info_override
                                        else getattr(llm_override, "profile_name", None))
                    if own_primary and own_primary not in profiles and own_primary not in (
                            active_profile_override, override_profile):
                        profiles.insert(0, own_primary)
                if use_advanced_model and profiles:
                    logger.debug(
                        f"[{self.name}] use_advanced_model=True — fallback "
                        f"chain: {profiles}"
                    )
                if not model_health.available(llm, request_id):
                    blocked = getattr(llm, "model", "?")
                    for index, profile in enumerate(profiles):
                        client = self._fallback_client(profile)
                        # _takes_format first: asking model_health makes this
                        # request the prober of an LLM it would then not call.
                        if (client is not None and _takes_format(client)
                                and model_health.available(client, request_id)):
                            logger.info(
                                f"[{self.name}] LLM {blocked} is blocked for "
                                f"{model_health.remaining(llm):.0f}s more; this step runs on {profile}")
                            # The blocked profiles walked past stay in the
                            # chain: if this one fails, a blocked LLM beats none.
                            return client, profiles[:index] + profiles[index + 1:], escalate, profile
                    logger.warning(
                        f"[{self.name}] LLM {blocked} is blocked and no fallback is free: calling it anyway")
                return llm, profiles, escalate, None

            def _base_label() -> str:
                if base_profile is not None:
                    return base_profile
                if llm_override is not None and llm_profile_info_override:
                    return llm_profile_info_override.split(":", 1)[0]
                return (display_profile_info or "base").split(":", 1)[0]

            def _take_fallback():
                """(label, client) to retry a failed call on, each taken once
                per step: the first unblocked one of this step's chain, with the
                run's base LLM as a member — the first after a failed
                escalation, the last after a failed walk around a blocked base;
                else the first one left all the same — a blocked LLM beats
                none. None when all are used up."""
                candidates = [(profile, None) for profile in fallback_profiles]
                if escalated_this_step:
                    # A failed escalation says nothing about the base: back to it
                    # before the chain moves the step to another model.
                    candidates.insert(0, (_base_label(), active_llm))
                else:
                    candidates.append((_base_label(), active_llm))
                first_blocked = None
                for index, (label, client) in enumerate(candidates):
                    if index in fallback_taken:
                        continue
                    if client is None:
                        client = self._fallback_client(label)
                    if client is None or client is current_llm or not _takes_format(client):
                        fallback_taken.add(index)
                        continue
                    if model_health.available(client, request_id):
                        fallback_taken.add(index)
                        return label, client
                    if first_blocked is None:
                        first_blocked = (index, label, client)
                if first_blocked is None:
                    return None
                index, label, client = first_blocked
                fallback_taken.add(index)
                return label, client

            async def _announce_llm():
                # Signal LLM call start — for the picked client, so an escalation
                # whose client did not build is not announced as one.
                if escalated_this_step:
                    llm_display = " (advanced — escalated: stuck)"
                elif step_profile:
                    llm_display = f" ({step_profile}:fallback)"
                elif llm_profile_info_override and base_profile is None:
                    llm_display = f" ({llm_profile_info_override})"
                else:
                    llm_display = f" ({display_profile_info})" if display_profile_info else " (unknown LLM)"
                await status_worker.progress(f"Calling LLM{llm_display}", meta={"step": step + 1})

            health_seen = model_health.version
            previous_step_llm = self._step_llms.get(session_id)
            current_llm, fallback_profiles, escalated_this_step, step_profile = (
                _pick_step_llm(escalated_this_step))
            fallback_taken: set = set()
            # Clients this step already asked a second time after an error in
            # the body -- each gets that once, see the upstream-error branch.
            # The clients themselves, not id()s: a dropped fallback client's id
            # can come back on the next one built.
            body_error_retried: list = []
            if previous_step_llm is not None and current_llm is not previous_step_llm:
                # A switch between steps: into or out of an escalation, around a
                # blocked LLM or back to it. The history carries the previous
                # model's reasoning.
                strip_all_reasoning_artifacts(messages)
            # A switch since the previous request (or restart) left no step
            # model behind; the artifacts name the model that produced them.
            strip_foreign_reasoning_artifacts(messages, getattr(current_llm, "model", None))
            # Before the hooks too: tool_preload runs tools inside them, and a
            # tool that sizes the context asks llm_for_session.
            self._step_llms[session_id] = current_llm
            await _announce_llm()

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
                        messages=messages,
                        step=step,
                        request_id=request_id,
                        session_id=session_id,
                        llm=current_llm,
                        cancellation_token=main_token
                    )
                )

                # Stream status events while hook is running
                # NOTE: this polling loop has no deadline of its own. Each hook
                # is bounded by its per-hook timeout (asyncio.wait_for in
                # hooks/registry.py; a timed-out hook is logged and skipped), so
                # a long-running hook (e.g. context_summarizer) needs a timeout
                # configured high enough for it.
                while not hook_task.done():
                    for status_event in yield_pending_status_events():
                        yield status_event
                    await asyncio.sleep(0.1)  # Poll every 100ms

                # Get hook result
                modified_messages = await hook_task

                # Yield any final status events from hook execution
                for status_event in yield_pending_status_events():
                    yield status_event

                # Select the message list that will go to the LLM. The
                # selection logic is extracted to ``_select_llm_messages``
                # so it can be unit-tested in isolation (the surrounding
                # step-loop is generator-based and hard to test directly).
                messages = self._select_llm_messages(
                    pre_hook_messages=messages,
                    modified_messages=modified_messages,
                )
                context.messages = messages
                # Whatever a hook staged now stands in `messages`. What is left
                # in the marker is spent, and it is poisonous from here on: the
                # code after tool execution reads a still-set marker as "a tool
                # rewrote the history mid-request" and rebuilds around it,
                # dropping the assistant tool-call message just appended.
                self._session_tracker.clear_compacted_messages(session_id)
            except Exception as e:
                logger.warning(f"Pre-LLM hooks failed: {e}", exc_info=True)

            # AFTER the hooks, so the run keeps the last word. Plugins append
            # their blocks in pre_llm_call, and the max-steps request ("answer
            # NOW, do NOT use any tools") only does its job as the last thing
            # the model reads -- a todo list with open items behind it sends
            # the model back to the tools. Outside the try on purpose: a hook
            # chain that fell over must not also cost the run its step budget.
            budget_note = self._step_budget_note(step, max_steps)
            if budget_note is not None:
                messages.append(budget_note)
                context.messages = messages

            # The blocks are shared by every agent, and the hooks can take
            # minutes (context_summarizer): another request may have blocked
            # this step's LLM, or an answer lifted the block this step walked
            # around, meanwhile. Pick again rather than call an LLM known to be
            # limited, or leave a free one unused. The hooks are not re-run —
            # they are not idempotent (tool_preload runs tools); the next step's
            # hooks see the new model.
            if model_health.version != health_seen:
                previous_llm = current_llm
                current_llm, fallback_profiles, escalated_this_step, step_profile = (
                    _pick_step_llm(escalated_this_step))
                fallback_taken = set()
                self._step_llms[session_id] = current_llm
                if current_llm is not previous_llm:
                    # The first pick may have made this request the prober of
                    # the LLM it now leaves: hand the probe back.
                    model_health.drop_probe(previous_llm, request_id)
                    # A model switch either way: the history the hooks worked
                    # on carries the previous model's reasoning items.
                    strip_all_reasoning_artifacts(messages)
                    await _announce_llm()

            # Spent only now that it is settled which model answers: a re-pick
            # onto a fallback would otherwise have spent a round on no advanced call.
            if escalated_this_step:
                escalator.consume()

            # LLM call with streaming support and fallback handling
            llm_out = None

            # WHICH client was told to try again after its thinking looped —
            # not merely THAT one was. A fallback switch later in this step
            # replaces current_llm, and the new model has earned no exemption:
            # comparing the client re-arms the watchdog by itself, where a
            # plain flag would leave an innocent model unwatched.
            reasoning_loop_llm = None

            async def _reasoning_progress(text, chars, previous):
                # current_llm is read at call time: after a fallback switch the
                # hooks see the model that is actually thinking.
                await self._hook_manager.execute_llm_progress_hooks(
                    reasoning_text=text, reasoning_chars=chars,
                    previous_reasoning_chars=previous, step=step,
                    request_id=request_id, session_id=session_id, llm=current_llm)

            while True:  # Retry loop for fallbacks (rate limits + upstream errors)
                # For tools that size the context themselves (compact, summarize):
                # the model answering this session's step, see llm_for_session.
                self._step_llms[session_id] = current_llm
                # Structured output, decided per CALL: a fallback within the step is another
                # model. The field goes on every call of the run, the tool steps included --
                # constant through the run, it keeps the cached prefix (OpenAI puts the schema
                # into the rendered context, Anthropic invalidates the cache when it changes);
                # a field on the last call only would miss the cache exactly there.
                wire_format = None
                if response_format is not None:
                    if supports_response_format(current_llm, response_format):
                        wire_format = response_format
                    elif not response_format.prompt_fallback:
                        # Unreachable while every switch of model asks _takes_format (and the run's
                        # own model is asked at the start): the guard that keeps a switch added
                        # later from sending the request without its field.
                        model_health.drop_probe(current_llm, request_id)
                        error_msg = unsupported_message(current_llm, response_format)
                        logger.warning("[%s] %s", self.name, error_msg)
                        results.setdefault("errors", []).append(error_msg)
                        yield {"type": "error", "message": error_msg,
                               "error_type": STRUCTURED_OUTPUT_UNSUPPORTED}
                        return
                    # A schema-less JSON mode says nothing about the shape, and a model without
                    # the field hears of the format only here. Once, as long as it stays in the
                    # history, and before this step's budget note, which has to stay the last
                    # thing the model reads.
                    if (wire_format is None or response_format.type == JSON_OBJECT) and not any(
                            getattr(m, "injected_by", None) == FORMAT_NOTE and m.content == format_note_text
                            for m in messages):
                        note = self._structured_output_note(format_note_text, FORMAT_NOTE)
                        if budget_note is not None and messages and messages[-1] is budget_note:
                            messages.insert(len(messages) - 1, note)
                        else:
                            messages.append(note)
                        context.messages = messages
                # A tool the history calls goes out with its schema: the hooks
                # may have written calls in since the run started (tool_preload),
                # as appended messages may. Loads nothing when nothing is new.
                if context.deferred_tools is not None:
                    context.deferred_tools.restore(messages, tools_schema)
                pending_thinking_complete = None
                _llm_call_started = asyncio.get_event_loop().time()
                health_asked_at = model_health.now()
                try:
                    async for event in self._call_llm_with_streaming(
                        llm=current_llm,
                        messages=messages,
                        tools_schema=tools_schema,
                        cancellation_token=main_token,
                        step=step,
                        yield_pending_status_fn=yield_pending_status_events,
                        status_scope=status_worker,
                        watch_reasoning=current_llm is not reasoning_loop_llm,
                        on_reasoning_progress=(_reasoning_progress
                                               if self._hook_manager.wants_llm_progress()
                                               else None),
                        response_format=wire_format,
                    ):
                        event_type = event.get("type")

                        if event_type in ("reasoning_delta", "reasoning_reset"):
                            # Yield Gemini reasoning/thinking tokens to WebUI; a
                            # reset empties the step's thinking after a stream restart.
                            yield event
                        elif event_type == "thinking_delta":
                            # Yield real-time token deltas to WebUI
                            yield event
                        elif event_type in ("status", "sub_run"):
                            # What the forwarder collected meanwhile: status lines, and
                            # the events of sub-agents working while this call waits.
                            yield event
                        elif event_type == "thinking_complete":
                            # A copy: the event goes on to every reader of the run, and the
                            # message kept in the session must not change with what one of them does
                            llm_out = {"assistant": copy.deepcopy(event["assistant"])}
                            # Preserve usage data if present in event
                            if "usage" in event:
                                llm_out["usage"] = event["usage"]
                            # ...and finish_reason, which the truncation guard
                            # below reads off llm_out.
                            if event.get("finish_reason"):
                                llm_out["finish_reason"] = event["finish_reason"]
                            # Store event — DON'T yield yet, check for upstream errors first
                            pending_thinking_complete = event

                    # Check for upstream error in response body BEFORE yielding to client.
                    # Upstream errors (e.g. Qwen/Alibaba content filter) arrive as HTTP 200
                    # with {"error": ...} in the body — not as exceptions.
                    # Handling them here (inside the while-True retry loop) allows clean
                    # retry with a fallback LLM without leaking the error event to the client.
                    _assistant_check = llm_out.get("assistant", {}) if llm_out else {}
                    if "error" in _assistant_check:
                        error_info = _assistant_check["error"]
                        error_msg = error_info.get("message", "Unknown LLM error")
                        error_type = error_info.get("type", "unknown")
                        logger.warning(f"LLM returned upstream error: {error_type} - {error_msg}")
                        # Billed all the same: the failed call's usage still
                        # reaches the caller's sum of the turn -- its content does not.
                        if pending_thinking_complete and pending_thinking_complete.get("usage"):
                            yield _name_the_model({
                                "type": "thinking_complete",
                                "step": pending_thinking_complete.get("step"),
                                "assistant": {},
                                "usage": pending_thinking_complete["usage"],
                            }, current_llm)

                        if (not str(error_type).startswith("content_filter")
                                and not error_info.get("retried")
                                and not any(c is current_llm for c in body_error_retried)):
                            # Once more on the SAME model first. Measured
                            # 22.09.2026: the same request shape went through
                            # 405 times, and the two invalid_prompt bodies came
                            # 3 s apart -- a gateway hiccup, not the request.
                            # Straight to the fallback moved those runs onto
                            # it for good. An unknown deterministic error costs
                            # one call more, then switches. Straight on: a
                            # content filter (content_filter, httpx's
                            # content_filter_<native>) -- the same model blocks
                            # the same text again -- and an error its client
                            # marks "retried", which already went through a
                            # whole retry cycle with backoff.
                            body_error_retried.append(current_llm)
                            logger.warning(
                                f"[{self.name}] Upstream error from LLM, "
                                f"asking {getattr(current_llm, 'model', '?')} once more")
                            await status_worker.progress(
                                f"LLM error ({error_type}), retrying once",
                                meta={"step": step + 1})
                            continue
                        taken = _take_fallback()
                        if taken:
                            fallback_profile, fallback_llm = taken
                            logger.warning(
                                f"[{self.name}] Upstream error from LLM, "
                                f"switching to fallback: {fallback_profile}"
                            )
                            await status_worker.progress(
                                f"LLM error ({error_type}), switching to {fallback_profile}",
                                meta={"step": step + 1, "fallback": fallback_profile}
                            )
                            # No block, like the 5xx path it is the twin of: an
                            # upstream error says the gateway stumbled, not that
                            # this LLM is gone. Rescues THIS request; the next
                            # starts on the original again.
                            self._strip_for_switch(fallback_profile, messages)
                            # Also swap the run's base LLM so hooks use the
                            # fallback too — when the BASE failed. An escalation
                            # model that failed says nothing about the base; only
                            # this step is rescued.
                            if current_llm is active_llm:
                                display_profile_info = f"{fallback_profile}:fallback"
                                active_llm = fallback_llm
                                base_profile = fallback_profile
                            current_llm = fallback_llm
                            continue  # Retry LLM call with fallback in same step
                        # The chain is used up — hard error
                        yield {"type": "error", "message": error_msg, "error_type": error_type}
                        return

                    # No error — yield the deferred thinking_complete and exit retry loop.
                    # An answer lifts the LLM's block for every agent — one set
                    # before this call went out.
                    model_health.release(current_llm, asked_at=health_asked_at)
                    if pending_thinking_complete:
                        yield _name_the_model(pending_thinking_complete, current_llm)
                    break
                    
                except ReasoningLoopError as e:
                    # The model walked into a circle inside its own thinking.
                    # Nothing is wrong with the provider, the model or the
                    # request — the sampling was unlucky — so this is the one
                    # recovery here that does NOT switch profiles: a profile
                    # switch would punish a healthy model for one bad roll.
                    #
                    # The exemption belongs to the CLIENT, not to the step: the
                    # retry runs unwatched, but a fallback switch afterwards
                    # brings a different client, and that one is watched again
                    # — it can abort here too, in the same step. What bounds
                    # this is the fallback chain, which is finite and shrinks
                    # with every switch; no client is ever watched twice.
                    reasoning_loop_llm = current_llm
                    logger.warning(
                        "[%s] Reasoning loop after %d characters of thinking "
                        "(%s) — retrying the same model once: %s",
                        self.name, e.characters, e.reason,
                        getattr(current_llm, "model", "?"))
                    await status_worker.progress(
                        "Thinking went in circles, retrying once",
                        meta={"step": step + 1, "reasoning_characters": e.characters})

                    # The aborted attempt WAS produced, so it must leave a
                    # trace. The client's own post-response notification sits
                    # after the stream, which this abort never reaches, so
                    # without this the message debugger keeps a request with
                    # no response — and every later recalibration of the
                    # threshold reads that same database and would be blind to
                    # exactly the calls this guard aborted.
                    #
                    # What it can and cannot say: the reasoning characters and
                    # the score are known and travel in response_data, so a
                    # later measurement can use these rows. The TOKENS are not
                    # — usage arrives with the completed response, which this
                    # call never produced — so the cost report still misses
                    # what the abort spent. Naming that beats implying the
                    # entry closes it.
                    #
                    # Reaching into the client's notifier is a deliberate
                    # layer crossing: there is no public equivalent, and an
                    # entry carrying "error" is the shape it already uses for
                    # its own failed attempts, which is why it skips the
                    # latency stash and leaves cost attribution untouched.
                    #
                    # A client that reports every ending already wrote the row
                    # when the stream closed -- with its usage, and with the
                    # reason set at the abort (abandon_report). Then this one
                    # would be a second row for the same call.
                    pending = abandon_report.get()
                    abandon_report.set(None)
                    notify = (None if pending is not None and pending.get("reported")
                              else getattr(current_llm, "_notify_post_response", None))
                    if notify is not None:
                        await notify({
                            # Most clients name themselves in their own
                            # notifications but carry no _PROVIDER attribute;
                            # the class name keeps the row attributable
                            # instead of filing it under "unknown".
                            "provider": (getattr(current_llm, "_PROVIDER", None)
                                         or type(current_llm).__name__),
                            "model": getattr(current_llm, "model", "?"),
                            "url": "", "is_streaming": True,
                            "duration_ms": (asyncio.get_event_loop().time()
                                            - _llm_call_started) * 1000,
                            "error": f"reasoning loop aborted: {e.reason}",
                            "finish_reason": "reasoning_loop_aborted",
                            "response_data": {"reasoning_loop": {
                                "characters": e.characters,
                                "reason": e.reason,
                            }},
                        })
                    continue

                except (LLMRateLimitError, LLMQuotaExhaustedError) as e:
                    is_quota_exhausted = isinstance(e, LLMQuotaExhaustedError)
                    reason = "quota exhausted" if is_quota_exhausted else "rate limit hit"
                    # A block of the LLM, not of this agent: every agent walks
                    # around it until it runs out or the LLM answers someone. A
                    # rate limit starts short and grows while the LLM keeps
                    # failing; an exhausted quota does not get better in a minute.
                    pause = model_health.block(
                        current_llm, max_pause=max_block_seconds,
                        rate_limit=not is_quota_exhausted, retry_after=e.retry_after,
                        asked_at=health_asked_at, reason=f"{reason}, seen by {self.name}")
                    taken = _take_fallback()
                    if taken:
                        fallback_profile, fallback_llm = taken
                        logger.warning(
                            f"[{self.name}] {e.__class__.__name__}: {e}. "
                            f"Switching to fallback profile: {fallback_profile}"
                        )
                        retry_in = f", retry in {pause:.0f}s" if pause and pause >= 1 else ""
                        await status_worker.progress(
                            f"{reason.capitalize()}, switching to {fallback_profile}{retry_in}",
                            meta={"step": step + 1, "fallback": fallback_profile, "blocked_seconds": pause}
                        )
                        self._strip_for_switch(fallback_profile, messages)
                        # No swap of the run's base: the next step asks the
                        # blocks again and returns to the base once it is free.
                        current_llm = fallback_llm
                        continue  # Retry with fallback
                    logger.error(f"[{self.name}] No fallback profiles available, {reason}")
                    raise

                except LLMServerError as e:
                    # 5xx server errors (e.g. DeepSeek 504) — try fallback, but no block.
                    # Server errors are transient outages; the primary LLM should be retried next time.
                    taken = _take_fallback()
                    if taken:
                        fallback_profile, fallback_llm = taken
                        logger.warning(
                            f"[{self.name}] Server error {e.status_code} from {e.model}: {e}. "
                            f"Switching to fallback profile: {fallback_profile}"
                        )
                        await status_worker.progress(
                            f"Server error {e.status_code}, switching to {fallback_profile}",
                            meta={"step": step + 1, "fallback": fallback_profile}
                        )
                        self._strip_for_switch(fallback_profile, messages)
                        # Request-scoped swap when the BASE failed, like its
                        # twins (upstream error, connection error): without it
                        # every following step started on the failing base
                        # again, its hooks sizing the context for a model the
                        # call never reached.
                        if current_llm is active_llm:
                            display_profile_info = f"{fallback_profile}:fallback"
                            active_llm = fallback_llm
                            base_profile = fallback_profile
                        current_llm = fallback_llm
                        continue  # Retry with fallback (request-scoped)
                    logger.error(f"[{self.name}] No fallback profiles available, server error unrecoverable")
                    raise

                except (LLMConnectionError, httpx.TransportError,
                        httpx.HTTPStatusError) as e:
                    # Transport errors (connect/read timeout, network failure) — the
                    # endpoint is unreachable, there is no HTTP response. Try the next
                    # profile, no block (same reasoning as LLMServerError above).
                    # Raw httpx.TransportError covers clients that re-raise transport
                    # failures untyped (e.g. the OpenAI responses client).
                    #
                    # httpx.HTTPStatusError is the 4xx case (429/5xx arrive as typed
                    # errors before this). For a CHAIN it means: this provider refuses
                    # this request. Since 2026-08-20 the primary of 186 chains is an
                    # OpenRouter profile with a direct-API fallback behind it; without
                    # this clause a 4xx killed the run without ever trying the
                    # fallback the chain exists for.
                    #
                    # The status decides HOW to fall back (review finding: lumping
                    # them made an expired key look like a network error and re-probed
                    # it on every step):
                    #   400/413/422  request-shaped (too long, cap exceeded) — a
                    #                different request may pass: no block.
                    #   401/402/403/404  key-, credit- or model-level; holds for every
                    #                request on this endpoint. The LLM is BLOCKED for
                    #                every agent, so the dead endpoint is not
                    #                re-probed max_steps times. A cross-provider
                    #                chain member has its own key and still rescues
                    #                the run.
                    status_code = getattr(getattr(e, "response", None), "status_code", None)
                    endpoint_level = status_code in (401, 402, 403, 404)
                    if status_code is None and _is_local_resource_exhaustion(e):
                        # No profile can rescue this: the next client cannot open
                        # a socket either. Walking the chain would only burn the
                        # fallbacks — onto more expensive models — and hide the
                        # real cause behind a provider-shaped error message.
                        logger.error(
                            f"[{self.name}] Out of file descriptors while calling "
                            f"the LLM ({e}). This is a local resource limit, not a "
                            f"provider failure — not switching profiles. Check the "
                            f"process's open descriptors against LimitNOFILE."
                        )
                        raise
                    kind = (f"HTTP {status_code}" if status_code
                            else "Connection/transport error")
                    if endpoint_level:
                        model_health.block(
                            current_llm, max_pause=max_block_seconds, rate_limit=False,
                            asked_at=health_asked_at, reason=f"{kind}, seen by {self.name}")
                    taken = _take_fallback()
                    if taken:
                        fallback_profile, fallback_llm = taken
                        logger.warning(
                            f"[{self.name}] {kind} from LLM: {e}. "
                            f"Switching to fallback profile: {fallback_profile}"
                        )
                        await status_worker.progress(
                            f"Connection error, switching to {fallback_profile}",
                            meta={"step": step + 1, "fallback": fallback_profile}
                        )
                        self._strip_for_switch(fallback_profile, messages)
                        # Request-scoped swap when the BASE failed (like the
                        # upstream-error and 5xx paths): without it, EVERY
                        # following step retries the dead endpoint first
                        # (~connect timeout x retries per step).
                        if current_llm is active_llm:
                            display_profile_info = f"{fallback_profile}:fallback"
                            active_llm = fallback_llm
                            base_profile = fallback_profile
                        current_llm = fallback_llm
                        continue  # Retry with fallback
                    logger.error(f"[{self.name}] No fallback profiles available, connection error unrecoverable")
                    raise

                except asyncio.CancelledError:
                    # Streaming was cancelled - send proper status events and cancelled event
                    logger.info(f"Request {request_id} cancelled during LLM call at step {step + 1}")
                    await status_worker.error(f"cancelled at step {step + 1}",
                                          meta={"step": step + 1, "reason": "cancelled"})
                    await status_coordinator.error(f"cancelled at step {step + 1}",
                                                meta={"step": step + 1, "reason": "cancelled"})
                    await asyncio.sleep(0.01)
                    for status_event in context.status_forwarder.get_pending_events():
                        yield status_event
                    yield {"type": "cancelled", "request_id": request_id, "step": step + 1}
                    return
                
                except asyncio.TimeoutError as e:
                    # LLM task timed out (e.g., batch job taking too long)
                    # This is different from user cancellation - report as timeout error
                    timeout_msg = str(e) if str(e) else "LLM request timed out"
                    logger.error(f"Request {request_id} timed out during LLM call at step {step + 1}: {timeout_msg}")
                    await status_worker.error(f"timeout at step {step + 1}: {timeout_msg}",
                                          meta={"step": step + 1, "reason": "timeout"})
                    await status_coordinator.error(f"timeout at step {step + 1}",
                                                meta={"step": step + 1, "reason": "timeout"})
                    await asyncio.sleep(0.01)
                    for status_event in context.status_forwarder.get_pending_events():
                        yield status_event
                    yield {"type": "error", "request_id": request_id, "step": step + 1, 
                           "message": timeout_msg, "error_type": "timeout"}
                    return

            # Signal LLM call completion
            await status_worker.progress("LLM (chat) response received", meta={"step": step + 1})

            assistant = llm_out.get("assistant", {}) if llm_out else {}

            content = assistant.get("content")
            tool_calls = assistant.get("tool_calls", [])
            # "length" = the model hit its output cap. With no content that is a
            # TRUNCATION, not an empty answer — see the empty-response guard below.
            finish_reason = llm_out.get("finish_reason") if llm_out else None
            # Provider-side encrypted thinking blocks (Gemini 3.x thought_signature
            # via OpenRouter's reasoning_details). MUST be carried through to the
            # next request or upstream returns MALFORMED_FUNCTION_CALL.
            reasoning_details = assistant.get("reasoning_details")
            answering_model = getattr(current_llm, "model", None)

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
                content=content or "",
                tool_calls=history_safe_tool_calls(tool_calls) if tool_calls else None,
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
                step=step + 1,
                timestamp=datetime.now(timezone.utc)
            )
            messages.append(assistant_msg)
            # Only append to context.messages if it's a different list
            if context.messages is not messages:
                context.messages.append(assistant_msg)

            # Execute post-LLM hooks to transform the response
            # NOTE: Using same polling pattern as pre_llm_hooks to support
            # future hooks that may emit status messages during execution.
            # Init per step: the continuation check below reads hook_metadata even
            # when the hook block fails — without this a first-step hook failure
            # raises NameError, and later steps would reuse the PREVIOUS step's
            # metadata (stale continuation signal).
            hook_metadata: Dict[str, Any] = {}
            try:
                # Create async task for hook execution
                hook_task = asyncio.create_task(
                    self._hook_manager.execute_post_llm_hooks(
                        messages=messages,
                        llm_response=llm_out,  # Pass full LLM response including usage data
                        step=step,
                        request_id=request_id,
                        session_id=session_id,
                        # The client that produced this response — after a
                        # fallback switch in the retry loop, not the run's base.
                        llm=current_llm
                    )
                )

                # Stream status events while hook is running
                # NOTE: this polling loop has no deadline of its own; each hook
                # is bounded by its per-hook timeout (hooks/registry.py).
                while not hook_task.done():
                    for status_event in yield_pending_status_events():
                        yield status_event
                    await asyncio.sleep(0.1)  # Poll every 100ms

                # Get hook result
                modified_response, hook_metadata = await hook_task

                # Yield any final status events
                for status_event in yield_pending_status_events():
                    yield status_event

                if modified_response is not None:
                    # Extract assistant data from modified response
                    modified_assistant = modified_response.get("assistant", {})
                    new_content = modified_assistant.get("content")
                    new_tool_calls = modified_assistant.get("tool_calls")

                    # Update content and tool_calls if hooks modified them
                    if new_content is not None:
                        content = new_content
                        assistant_msg.content = content or ""
                    if new_tool_calls is not None:
                        tool_calls = new_tool_calls
                        assistant_msg.tool_calls = history_safe_tool_calls(tool_calls) if tool_calls else None

                    # Set content_format from hook metadata (e.g., 'html', 'markdown', 'text')
                    if "content_format" in hook_metadata:
                        assistant_msg.content_format = hook_metadata["content_format"]
            except Exception as e:
                logger.warning(f"Post-LLM hooks failed: {e}", exc_info=True)

            # The answer goes out as the model wrote it -- Markdown, which the chat and agent-cli draw
            # themselves. A structured run's answer is JSON, and its events say so.
            content_format = getattr(assistant_msg, 'content_format', 'text')  # Default to 'text' if not set by hooks
            if response_format is not None and not tool_calls:
                content_format = "json"

            # Emit thinking event with LLM response (for UI to show assistant reasoning)
            yield {"type": "thinking", "step": step + 1, "assistant": {"content": content, "tool_calls": tool_calls, "content_format": content_format}}

            # Also emit simplified thinking event if we have content and no tool calls (final answer)
            if content and not tool_calls:
                yield {"type": "thinking", "content": content, "content_format": content_format}

            # Yield pending status events after LLM response
            for status_event in yield_pending_status_events():
                yield status_event

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
            if finish_reason != "length":
                consecutive_cut_off = 0
            if finish_reason == "length" and (content or tool_calls):
                logger.warning(
                    "[%s] Answer truncated at the output cap (finish_reason=length, "
                    "model=%s, chars=%d, tool_calls=%d) — it is NOT complete. "
                    "Raise max_tokens or lower the reasoning level if this recurs.",
                    self.name, getattr(current_llm, "model", "?"),
                    len(content or ""), len(tool_calls or []),
                )

            # Infinite loop guard: Track consecutive empty responses FIRST
            # (before checking tool calls, to catch completely empty responses)
            if not content and not tool_calls:
                consecutive_empty_responses += 1
                
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
                if finish_reason == "length":
                    usage = (llm_out or {}).get("usage") or {}
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
                    results.setdefault("errors", []).append(error_msg)
                    yield {"type": "error", "message": error_msg}
                    return

                # Not on the final call: no step follows to read the nudge, and
                # the session would keep it as the conversation's last word.
                if consecutive_empty_responses >= max_consecutive_empty and not final_call:
                    logger.warning(f"Empty response #{consecutive_empty_responses}: Injecting 'Continue' note to prompt LLM")
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
                    if consecutive_empty_responses >= max_consecutive_empty + 3:
                        logger.warning(f"Breaking loop: {consecutive_empty_responses} consecutive empty responses even after 'Continue' prompts")
                        error_msg = "LLM returned empty responses repeatedly despite continue prompts"
                        results.setdefault("errors", []).append(error_msg)
                        yield {"type": "error", "message": error_msg}
                        return
                    # Continue to next iteration with the injected message
                    continue
            else:
                consecutive_empty_responses = 0  # Reset counter
                
                # Persist session after each valid (non-empty) LLM response to preserve progress on cancellation
                # NOTE: We only persist non-empty responses to avoid accumulating useless empty messages
                await self._persist_conversation(
                    session_id, messages, to_disk=False,
                    note=f"after LLM response (step {step})")

            # Check if we have tool calls to execute
            if tool_calls:
                # Reset no-tool-calls counter
                consecutive_no_tool_calls = 0
                
                # ===== TOOL CALL LOOP DETECTION =====
                # Check for repeated tool call patterns that indicate the agent is stuck
                loop_result = loop_detector.record_batch_and_check(tool_calls, step)
                
                pending_intervention_msg: Optional[ChatMessage] = None

                if loop_result.is_loop:
                    # Log the detection
                    logger.warning(
                        f"[{self.name}] Tool loop detected at step {step}: "
                        f"type={loop_result.loop_type}, tool={loop_result.tool_name}, "
                        f"count={loop_result.repetition_count}"
                    )
                    
                    # Prepare intervention message to nudge the LLM
                    pending_intervention_msg = ChatMessage(
                        role=DEVELOPER,
                        content=loop_result.intervention,
                        timestamp=datetime.now(timezone.utc),
                        injected_by="agent.loop_intervention",
                    )
                    
                    # Emit status event for visibility
                    await status_worker.progress(
                        f"Loop detected: {loop_result.tool_name} ({loop_result.repetition_count}x)",
                        meta={"step": step + 1, "loop_type": loop_result.loop_type}
                    )

                    # Objective stuck signal → open an escalation window so the
                    # NEXT few steps run on the advanced model (budget permitting).
                    # No step follows the final call to run on it.
                    esc_reason = None if final_call else escalator.trigger(
                        f"tool-call loop ({loop_result.tool_name})")
                    if esc_reason:
                        logger.warning(
                            "[%s] auto-escalating to advanced model at step %d: %s "
                            "(budget used %d/%d)", self.name, step + 1, esc_reason,
                            escalator.calls_used, escalator.max_calls)
                        await status_worker.progress(
                            f"auto-escalating to advanced model ({esc_reason})",
                            meta={"step": step + 1})

                    # If tool should be blocked, filter it out
                    if loop_result.should_block_tool:
                        blocked_tools = loop_result.blocked_tools
                        original_count = len(tool_calls)
                        tool_calls = [
                            tc for tc in tool_calls 
                            if tc.get("function", {}).get("name") not in blocked_tools
                        ]
                        if len(tool_calls) < original_count:
                            logger.warning(
                                f"[{self.name}] Blocked {original_count - len(tool_calls)} tool calls "
                                f"due to loop detection. Blocked tools: {blocked_tools}"
                            )
                            if final_call:
                                # A blocked call gets no result. After a step the
                                # next call's message_validator drops it from the
                                # history; after the final call no call follows,
                                # and the session would keep it unanswered.
                                assistant_msg.tool_calls = (
                                    history_safe_tool_calls(tool_calls) if tool_calls else None)
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
                                if not final_call:
                                    messages.append(pending_intervention_msg)
                                    context.messages = messages
                                elif content and content.strip():
                                    # What is left is a text answer on the final
                                    # call: delivered like the no-tool answer below.
                                    if response_format is not None:
                                        # No step is left to ask for a correction in.
                                        checked = await self._check_structured_answer(
                                            content, response_format, request_id)
                                        if checked.checker_failed:
                                            event = self._structured_output_unavailable(checked)
                                            results.setdefault("errors", []).append(event["message"])
                                            yield event
                                            return
                                        if not checked.ok:
                                            error_msg = self._structured_output_failure(
                                                checked.errors, corrected=format_repaired)
                                            logger.warning("[%s] %s", self.name, error_msg)
                                            results.setdefault("errors", []).append(error_msg)
                                            yield {"type": "error", "message": error_msg,
                                                   "error_type": STRUCTURED_OUTPUT_INVALID}
                                            return
                                        content = assistant_msg.content = checked.text
                                        content_format = assistant_msg.content_format = "json"
                                    results["summary"] = content
                                    self._set_live_messages(session_id, messages.copy())
                                    final_event = {"type": "final", "summary": content,
                                                   "content_format": content_format}
                                    if llm_out and "usage" in llm_out:
                                        final_event["usage"] = llm_out["usage"]
                                    yield final_event
                                    return
                                continue
                
                # Signal tool execution start
                await status_worker.progress(f"Executing Tools ({len(tool_calls)} total)", meta={"step": step + 1})

                # Assistant message with tool calls was already added above before post_llm hooks

                # Update tracked messages
                self._set_live_messages(session_id, messages.copy())

                # Execute all tools using streaming to get real-time status events from sub-agents
                tool_messages = []
                tool_results = []

                user_id = self.tool_user(request_id, session_id)

                # CRITICAL: Pass per-request status_forwarder as parameter to avoid race conditions
                # when multiple requests share the same agent instance (e.g., parent + sub-agent)
                # DO NOT set self._tool_execution_manager._status_forwarder - that causes race conditions!

                async for item in self._tool_execution_manager.execute_tools_streaming(
                    tool_calls=tool_calls,
                    tool_name_mapping=tool_name_mapping,
                    available_tools=context.available_tools,
                    step=step,
                    request_id=request_id,
                    session_id=session_id,
                    user_id=user_id,
                    status_forwarder=context.status_forwarder,
                    assistant_message=assistant_msg,
                    # What this run was switched to, for the sub-agents its
                    # tools start (agent_config.inherit_parent_llm).
                    llm_profile=self._profile_to_hand_down(llm_override),
                    intercept=(functools.partial(context.deferred_tools.intercept,
                                                 tools_schema=tools_schema)
                               if context.deferred_tools is not None else None),
                ):
                    if item.get("type") == "status":
                        # Yield status events in real-time during tool execution
                        yield item["event"]
                    elif item.get("type") == "tool_events":
                        # Yield tool execution events
                        for event in item["events"]:
                            yield event
                    elif item.get("type") == "complete":
                        # Store final results
                        tool_messages = item["messages"]
                        tool_results = item["results"]

                # Add tool results to the results dictionary
                results["calls"].extend(tool_results)

                # Stuck signal: a step whose tool calls ALL returned an error.
                # Catches the near-loops the exact-match detector misses (same
                # tool retried with slightly varied wrong args). N in a row →
                # open an escalation window. Only calls that ran count: a call a
                # hook blocked (a policy, a person saying no) is no sign that a
                # stronger model is needed, nor is a deferred tool called before
                # it was loaded -- a step of nothing but such calls neither grows
                # the streak nor breaks it.
                ran_messages = [m for m in tool_messages if not tool_message_never_ran(m)]
                if tool_messages and not ran_messages:
                    prev_step_all_errored = True
                elif ran_messages and all(self._tool_message_is_error(m) for m in ran_messages):
                    consecutive_tool_error_steps += 1
                    prev_step_all_errored = True  # keep the streak alive next step
                    if consecutive_tool_error_steps >= escalate_error_streak and not final_call:
                        esc_reason = escalator.trigger(
                            f"{consecutive_tool_error_steps} all-error tool steps")
                        if esc_reason:
                            logger.warning(
                                "[%s] auto-escalating to advanced model at step %d: "
                                "%s (budget used %d/%d)", self.name, step + 1,
                                esc_reason, escalator.calls_used, escalator.max_calls)
                            await status_worker.progress(
                                f"auto-escalating to advanced model ({esc_reason})",
                                meta={"step": step + 1})
                # A non-all-error tool step leaves prev_step_all_errored False,
                # so the streak resets at the top of the next iteration.

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

                if pending_intervention_msg is not None and not final_call:
                    messages.append(pending_intervention_msg)

                # Sync context.messages with the updated messages list
                context.messages = messages

                # Update tracked messages after tool execution
                self._set_live_messages(session_id, messages.copy())

                # Persist session after complete turn (tool calls + results processed).
                # Saving only at the turn boundary (not after each tool) avoids
                # orphaned tool calls; the disk save preserves progress even if
                # no SSE client is connected.
                await self._persist_conversation(
                    session_id, messages, to_disk=True,
                    note=f"after completing turn (step {step})")

                # Yield pending status events after tool execution
                for status_event in yield_pending_status_events():
                    yield status_event

                if final_call:
                    # A step's cancel is reported by the check at the top of the
                    # next iteration; after the final call there is none.
                    if self._is_cancelled(request_id):
                        async for event in cancelled_events(step):
                            yield event
                        return
                    # Tools on the final call run once: an agent that delivers
                    # through a tool (the v6 auditors) would otherwise lose the
                    # delivery its whole run was for. The budget stays a cap --
                    # no call follows to read the results -- and the run says so.
                    ran = ", ".join(
                        f"{m.name} ({'error' if self._tool_message_is_error(m) else 'ok'})"
                        for m in tool_messages) or "none"
                    logger.warning(
                        f"[{self.name}] Agent returned tool calls after max_steps limit and they "
                        f"ran without a further LLM call: {ran}. Increase max_steps or simplify the task."
                    )
                    # No final event: callers that read only final take it as
                    # success (the writer dispatch records ok=True, the job
                    # manager marks the run answered), whatever the tools
                    # returned. The results stay in the session.
                    error_msg = (f"Agent incomplete: max steps ({max_steps}) reached; "
                                 f"the final call ran tool calls ({ran}) that no step followed.")
                    results.setdefault("errors", []).append(error_msg)
                    yield {"type": "error", "message": error_msg}
                    return

                # Continue to next iteration to let LLM respond to tool results
                continue

            # No tool calls - check if we should treat this as the final answer
            # Track consecutive responses without tool calls
            consecutive_no_tool_calls += 1
            # (prev_step_all_errored stays False → the error streak resets at the
            #  top of the next iteration; handled centrally, see loop top.)

            # === CONTINUATION HOOK SIGNAL ===
            # A post_llm_call hook (e.g. agent_continuation) may set
            # metadata["continue"] = True to prevent treating a text-only
            # response as the final answer.  This allows autonomous agents
            # to keep working when they emit intermediate status reports.
            # Not on the final call: no step is left to continue in.
            if hook_metadata.get("continue") and content and content.strip() and not final_call:
                cont_count = hook_metadata.get("continuation_count", "?")
                cont_reason = hook_metadata.get("continuation_reason", "hook signal")
                logger.info(
                    f"[{self.name}] Continuation #{cont_count} at step {step}: {cont_reason}"
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
                messages.append(continuation_msg)
                context.messages = messages
                await status_worker.progress(
                    f"Auto-continue #{cont_count}: {cont_reason}",
                    meta={"step": step + 1, "continuation": True},
                )
                # Yield event so frontend can display the injected message
                yield {
                    "type": "continuation",
                    "message": continuation_msg.content,
                    "count": cont_count,
                    "reason": cont_reason,
                    "step": step + 1,
                }
                consecutive_no_tool_calls = 0  # Reset — hook evaluated this
                continue

            # A user message may have been injected while the LLM produced this
            # response (mid-run append). Never finalize past fresh user input —
            # continue the loop so the next LLM call reacts to it. The interim
            # content was already surfaced via the thinking events above.
            # On the final call no step is left: the message stays in the
            # history behind the answer, persisted for the session's next run.
            pre_drain_count = len(messages)
            messages = await self._drain_appended_messages(request_id, messages)
            if len(messages) > pre_drain_count and not final_call:
                context.messages = messages
                self._set_live_messages(session_id, messages.copy())
                consecutive_no_tool_calls = 0
                continue

            # Cut off at the output cap with no tool call left: not an answer.
            # The call the model was writing is lost, and the text before it --
            # "now the engine, the big file:" -- is an announcement. Measured in a
            # coder session: three calls in a row stopped at 16384 tokens, each
            # ended the run as if that were its reply, and the user had to push
            # three times. The run goes on with a note saying what happened --
            # where the agent opted in (output_cap_notes), not on the final
            # call, and not past that many in a row.
            if (finish_reason == "length" and content and content.strip() and not final_call
                    and consecutive_cut_off < max_cut_off_notes):
                consecutive_cut_off += 1
                usage = (llm_out or {}).get("usage") or {}
                messages.append(self._output_cap_note(usage.get("completion_tokens")))
                context.messages = messages
                await status_worker.progress(
                    "answer cut off at the output limit -- asked to continue in parts",
                    meta={"step": step + 1, "cut_off": consecutive_cut_off})
                consecutive_no_tool_calls = 0
                continue

            # If we have content AND it's not just whitespace, treat as final answer
            if content and content.strip():
                if response_format is not None:
                    checked = await self._check_structured_answer(content, response_format, request_id)
                    if checked.checker_failed:
                        event = self._structured_output_unavailable(checked)
                        logger.warning("[%s] %s", self.name, event["message"])
                        results.setdefault("errors", []).append(event["message"])
                        yield event
                        return
                    if not checked.ok and not checked.schema_failed and not format_repaired and not final_call:
                        # Once: the model sees its answer and what is wrong with it, and
                        # writes it again. Appended like every loop note, so the cached
                        # prefix stays; its answer stays too, the note refers to it.
                        format_repaired = True
                        logger.warning("[%s] Final answer does not match the requested format, asking "
                                       "once for a correction: %s", self.name, "; ".join(checked.errors))
                        messages.append(self._structured_output_note(
                            repair_text(checked.errors), "agent.structured_output_repair"))
                        context.messages = messages
                        self._set_live_messages(session_id, messages.copy())
                        await status_worker.progress(
                            "final answer does not match the JSON format -- asked once to correct it",
                            meta={"step": step + 1})
                        consecutive_no_tool_calls = 0
                        continue
                    if not checked.ok:
                        error_msg = self._structured_output_failure(checked.errors, corrected=format_repaired)
                        logger.warning("[%s] %s", self.name, error_msg)
                        results.setdefault("errors", []).append(error_msg)
                        yield {"type": "error", "message": error_msg, "error_type": STRUCTURED_OUTPUT_INVALID}
                        return
                    # Delivered as checked: a fence around the whole answer is gone, in the
                    # session too -- the caller parses what the session keeps (openai_api).
                    content = assistant_msg.content = checked.text
                    content_format = assistant_msg.content_format = "json"
                # Assistant message was already added above before post_llm hooks
                results["summary"] = content
                # Update tracked messages with final response
                self._set_live_messages(session_id, messages.copy())

                final_event = {"type": "final", "summary": content, "content_format": content_format}
                # Include usage data if available from last LLM call
                if llm_out and "usage" in llm_out:
                    final_event["usage"] = llm_out["usage"]
                yield final_event
                return
            
            # No tool calls AND (no content OR empty content)
            # Check consecutive no-tool-calls limit to avoid infinite loop
            # Not on the final call: an empty answer there is a spent budget,
            # which the error after the loop reports, not an empty success.
            if consecutive_no_tool_calls >= max_consecutive_no_tools and not final_call:
                logger.warning(f"Breaking loop: {consecutive_no_tool_calls} consecutive responses without tool calls (empty or no content)")
                if response_format is not None:
                    # An empty answer is no JSON: not a final one for a structured run.
                    error_msg = self._structured_output_failure(
                        schema_worker.parse_answer(content)[2], corrected=format_repaired)
                    results.setdefault("errors", []).append(error_msg)
                    yield {"type": "error", "message": error_msg, "error_type": STRUCTURED_OUTPUT_INVALID}
                    return
                # Treat whatever content we have as final (even if empty)
                results["summary"] = content or ""
                self._set_live_messages(session_id, messages.copy())
                final_event = {"type": "final", "summary": content or "", "content_format": content_format}
                if llm_out and "usage" in llm_out:
                    final_event["usage"] = llm_out["usage"]
                yield final_event
                return

            if final_call and not (content and content.strip()):
                # A blank answer to the final call is no answer: the error after
                # the loop reports the spent budget, and the session must not
                # end on a blank assistant turn. Drained input may follow it.
                for history in {id(messages): messages, id(context.messages): context.messages}.values():
                    for i in range(len(history or []) - 1, -1, -1):
                        if history[i] is assistant_msg:
                            del history[i]
                            break

            # Update tracked messages at end of each step (per-session)
            self._set_live_messages(session_id, messages.copy())

            # Drain any final appended messages before next step
            messages = await self._drain_appended_messages(request_id, messages)
            # Sync context.messages after draining
            context.messages = messages

        # Reached only when the final call ended without text and without a
        # tool call that ran: empty or blank, or its tool calls all blocked.
        # A step's cancel is reported at the top of the next iteration; there
        # is none after the final call.
        if self._is_cancelled(request_id):
            async for event in cancelled_events(max_steps):
                yield event
            return
        results.setdefault("errors", []).append("LLM planner reached max steps without final answer.")
        yield {"type": "error", "message": "LLM planner reached max steps without final answer."}
