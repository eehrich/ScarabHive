"""The LLM call of a step: _call_llm_with_streaming, its streaming and its polled path.

One call, one model, no retries -- the retries and the fallback chain are the loop's
(fallback.py). A streaming client hands out its deltas as they come, watched for a reasoning loop
and reported to llm_progress hooks; a client that cannot stream is polled while the call runs, so
that status events and heartbeats keep flowing. Both end on one thinking_complete event, with a
provider content filter turned into an error by the same rule (_content_filter_as_error).

The two paths are module functions, not methods: tests drive _call_llm_with_streaming with a
stand-in ``self`` that carries only ``name`` and ``_reasoning_loop_config``, and the paths get what
they need from it as arguments. A test that shortens the llm_progress tick patches
_REASONING_PROGRESS_TICK here, where it is looked up.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterable, List, Optional

from .....core.cancellation import CancellationToken
from .....llm.models import ChatMessage, LLMClient, abandon_report
from .....llm.structured_output import ResponseFormat
from .....tools.status import StatusScope
from ...reasoning_loop import ReasoningLoopError, build_detectors

if TYPE_CHECKING:
    from ...server import Agent

logger = logging.getLogger(__name__)


# llm_progress hooks fire every this many characters of thinking. A hook sets
# its own, coarser interval on top; this only bounds how often the loop pays
# for a hook dispatch while it streams.
_REASONING_PROGRESS_TICK = 2000


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


class _ThinkingWatch:
    """The thinking of one streaming call: the reasoning-loop detectors and the llm_progress ticks.

    Detectors per CALL: they hold this call's thinking, and a retry
    must start from empty windows. Two of them — a short window for
    short-period loops and a long one for the periods the short
    window cannot span; see build_detectors. A stream restart starts
    the detectors and the ticks over (reset).
    """

    def __init__(self, agent_name: str, llm: Any, reasoning_loop_config: Dict[str, Any],
                 watch_reasoning: bool,
                 on_reasoning_progress: Optional[Callable[[str, int, int], Awaitable[Any]]]):
        self._agent_name = agent_name
        self._llm = llm
        self._config = reasoning_loop_config
        self._watch = watch_reasoning
        self._on_progress = on_reasoning_progress
        self.reset()

    def reset(self) -> None:
        self.detectors = build_detectors(
            # Off for the retry the detector itself asked for: watching
            # the second attempt too would mean a second abort policy,
            # and there is nothing sensible left to do after it.
            enabled=bool(self._config["enabled"]) and self._watch,
            repetition_threshold=float(
                self._config["repetition_threshold"]))
        # Thinking of THIS call, for llm_progress hooks; a retry starts empty.
        self.parts: List[str] = []
        self.chars = 0
        self.ticked_at = 0

    async def record(self, delta: str) -> None:
        """One thinking delta: raises ReasoningLoopError on a loop, ticks the progress hooks."""
        # Watch the thinking for a loop. What arrives here is
        # whatever the client calls a thinking delta: raw
        # reasoning for most models, and for the OpenAI family a
        # SUMMARY of it — the threshold was calibrated on raw
        # reasoning, so for those models this guards the
        # degenerate case rather than measuring a known shape.
        for _detector in self.detectors:
            loop_reason = _detector.record(delta)
            if loop_reason:
                logger.warning(
                    "[%s] Aborting the call: %s (model=%s, %d characters "
                    "of thinking so far)",
                    self._agent_name, loop_reason, getattr(self._llm, "model", "?"),
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

        if self._on_progress is not None:
            self.parts.append(delta)
            self.chars += len(delta)
            if self.chars - self.ticked_at >= _REASONING_PROGRESS_TICK:
                try:
                    await self._on_progress(
                        "".join(self.parts), self.chars,
                        self.ticked_at)
                except Exception as exc:  # an observer never breaks the call
                    logger.warning("[%s] llm_progress hooks failed: %s",
                                   self._agent_name, exc)
                self.ticked_at = self.chars


async def _streamed_call(
    llm: LLMClient,
    messages: List[ChatMessage],
    tools_schema: List[Dict[str, Any]],
    cancellation_token: CancellationToken,
    step: int,
    yield_pending_status_fn: Callable[[], Iterable[dict]],
    status_scope: Optional[StatusScope],
    wire_format: Dict[str, Any],
    thinking: _ThinkingWatch,
    agent_name: str,
):
    """The streaming path: zero-overhead real-time tokens, status events between the chunks."""
    accumulated_content = []
    final_assistant = None
    final_usage = None  # Store usage data from final chunk
    final_finish_reason = None  # "length", "content_filter", ...

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

                await thinking.record(chunk["delta"])

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
                thinking.reset()
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
                agent_name, _model, len(final_assistant.get("content") or ""),
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


async def _polled_call(
    llm: LLMClient,
    messages: List[ChatMessage],
    tools_schema: List[Dict[str, Any]],
    cancellation_token: CancellationToken,
    step: int,
    yield_pending_status_fn: Callable[[], Iterable[dict]],
    status_scope: Optional[StatusScope],
    wire_format: Dict[str, Any],
    timeouts: Any,
    heartbeat_interval_seconds: float,
):
    """The non-streaming path: the call runs as a task, polled every 100ms for status events."""
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
    max_llm_iterations = timeouts.llm_task_max_iterations if timeouts else 864000  # 24h default
    llm_iteration_count = 0
    # Calculate heartbeat interval in iterations (config is in seconds, we poll every 0.1s)
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


class LLMCallMixin:
    """The LLM call of a step (see the module docstring).

    Relies on Agent.__init__ for ``name`` and ``_reasoning_loop_config`` (the streaming path) and
    ``timeouts`` and ``system_config`` (the polled path).
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
            call = _streamed_call(
                llm, messages, tools_schema, cancellation_token, step, yield_pending_status_fn,
                status_scope, wire_format,
                _ThinkingWatch(self.name, llm, self._reasoning_loop_config,
                               watch_reasoning, on_reasoning_progress),
                self.name)
        else:
            # Non-streaming LLM: Use polling with 100ms intervals
            call = _polled_call(
                llm, messages, tools_schema, cancellation_token, step, yield_pending_status_fn,
                status_scope, wire_format,
                self.timeouts, self.system_config.status.llm_heartbeat_interval)
        # Closed with this generator, as the one frame it was: a stream left early is
        # closed at once (see _streamed_call), not when GC finds the path.
        async with contextlib.aclosing(call) as events:
            async for event in events:
                yield event
