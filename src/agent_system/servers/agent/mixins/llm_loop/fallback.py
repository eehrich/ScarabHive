"""The LLM call of a step with its recoveries: the retry loop until the step has an answer.

_call_step_llm calls the step's LLM (_call_llm_with_streaming, llm_call.py) until it answers, and
decides what each failure means: an error in the body is asked once more on the same model, then
moves on; a reasoning loop is retried on the same model, unwatched; a rate limit, an exhausted quota
or a refused key blocks the LLM for every agent (llm/model_health.py) and moves on; a 5xx or a
transport error moves on without a block; running out of file descriptors moves nowhere. "Moves on"
is the next fallback of the step (step_llm.py), and when the chain is used up the error goes to the
caller. A cancel or a timeout ends the run. Its own module because what each error means is the one
place the rules of docs/_arch_agent_architecture.md ("What Triggers a Block") live in code.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import errno
import functools
import logging
from typing import TYPE_CHECKING, Any, Dict

import httpx

from .....llm.model_health import model_health
from .....llm.models import (
    LLMConnectionError,
    LLMQuotaExhaustedError,
    LLMRateLimitError,
    LLMServerError,
    abandon_report,
)
from .....llm.structured_output import (
    JSON_OBJECT, STRUCTURED_OUTPUT_UNSUPPORTED, supports_response_format, unsupported_message,
)
from ...reasoning_loop import ReasoningLoopError
from .llm_call import _name_the_model
from .state import LoopState, StepEnd, StepState

if TYPE_CHECKING:
    from ...server import Agent

logger = logging.getLogger(__name__)


#: ``injected_by`` of the note that describes a run's structured output to the model.
FORMAT_NOTE = "agent.structured_output"

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


class FallbackMixin:
    """The step's LLM call and its recoveries (see the module docstring).

    Relies on Agent.__init__ for ``name``, ``_hook_manager`` and ``_step_llms``, on step_llm.py for
    the fallback (``_take_fallback``, ``_switch_to_fallback``) and on step.py for the events a cancel
    or a timeout ends the run with.
    """

    async def _call_step_llm(self: Agent, run: LoopState, st: StepState):
        """Phase: the step's LLM call, retried and moved along the fallback chain until it answers.

        Leaves the answer in ``st.llm_out``; ends the run (``st.end``) when no LLM is left to ask,
        on a cancel and on a timeout. An error no fallback can help with goes up as it came.
        """
        # LLM call with streaming support and fallback handling (st.llm_out starts as None)
        while True:  # Retry loop for fallbacks (rate limits + upstream errors)
            async with contextlib.aclosing(self._prepare_llm_call(run, st)) as events:
                async for event in events:
                    yield event
            if st.ended():
                return
            st.call_started = asyncio.get_event_loop().time()
            st.health_asked_at = model_health.now()
            try:
                async with contextlib.aclosing(self._attempt_llm_call(run, st)) as events:
                    async for event in events:
                        yield event

                # Check for upstream error in response body BEFORE yielding to client.
                # Upstream errors (e.g. Qwen/Alibaba content filter) arrive as HTTP 200
                # with {"error": ...} in the body — not as exceptions.
                # Handling them here (inside the while-True retry loop) allows clean
                # retry with a fallback LLM without leaking the error event to the client.
                _assistant_check: Dict[str, Any] = st.llm_out.get("assistant", {}) if st.llm_out else {}
                if "error" in _assistant_check:
                    async with contextlib.aclosing(self._on_upstream_error(run, st, _assistant_check["error"])) as events:
                        async for event in events:
                            yield event
                    if st.ended():
                        return
                    continue

                # No error — yield the deferred thinking_complete and exit retry loop.
                # An answer lifts the LLM's block for every agent — one set
                # before this call went out.
                model_health.release(st.current_llm, asked_at=st.health_asked_at)
                if st.pending_thinking_complete:
                    yield _name_the_model(st.pending_thinking_complete, st.current_llm)
                break

            except ReasoningLoopError as e:
                await self._on_reasoning_loop(run, st, e)
                continue

            except (LLMRateLimitError, LLMQuotaExhaustedError) as e:
                if await self._fall_back_on_rate_limit(run, st, e):
                    continue  # Retry with fallback
                raise

            except LLMServerError as e:
                if await self._fall_back_on_server_error(run, st, e):
                    continue  # Retry with fallback (request-scoped)
                raise

            except (LLMConnectionError, httpx.TransportError,
                    httpx.HTTPStatusError) as e:
                if await self._fall_back_on_transport_error(run, st, e):
                    continue  # Retry with fallback
                raise

            except asyncio.CancelledError:
                # Streaming was cancelled - send proper status events and cancelled event
                async with contextlib.aclosing(self._cancelled_events(run, st.step, during_llm_call=True)) as events:
                    async for event in events:
                        yield event
                st.end = StepEnd.RUN
                return

            except asyncio.TimeoutError as e:
                # LLM task timed out (e.g., batch job taking too long)
                # This is different from user cancellation - report as timeout error
                timeout_msg = str(e) if str(e) else "LLM request timed out"
                logger.error(f"Request {run.request_id} timed out during LLM call at step {st.step + 1}: {timeout_msg}")
                async with contextlib.aclosing(self._stop_events(
                            run, st.step, "timeout",
                            worker_line=f"timeout at step {st.step + 1}: {timeout_msg}",
                            coordinator_line=f"timeout at step {st.step + 1}",
                            last_event={"type": "error", "request_id": run.request_id, "step": st.step + 1,
                                        "message": timeout_msg, "error_type": "timeout"})) as events:
                    async for event in events:
                        yield event
                st.end = StepEnd.RUN
                return

    async def _prepare_llm_call(self: Agent, run: LoopState, st: StepState):
        """Before each call of the retry loop: the session's step model, the structured output for
        this call's model, the schemas of deferred tools the history calls."""
        # For tools that size the context themselves (compact, summarize):
        # the model answering this session's step, see llm_for_session.
        self._step_llms[run.session_id] = st.current_llm
        # Structured output, decided per CALL: a fallback within the step is another
        # model. The field goes on every call of the run, the tool steps included --
        # constant through the run, it keeps the cached prefix (OpenAI puts the schema
        # into the rendered context, Anthropic invalidates the cache when it changes);
        # a field on the last call only would miss the cache exactly there.
        st.wire_format = None
        response_format = run.response_format
        if response_format is not None:
            if supports_response_format(st.current_llm, response_format):
                st.wire_format = response_format
            elif not response_format.prompt_fallback:
                # Unreachable while every switch of model asks takes_format (and the run's
                # own model is asked at the start): the guard that keeps a switch added
                # later from sending the request without its field.
                model_health.drop_probe(st.current_llm, run.request_id)
                error_msg = unsupported_message(st.current_llm, response_format)
                logger.warning("[%s] %s", self.name, error_msg)
                run.results.setdefault("errors", []).append(error_msg)
                yield {"type": "error", "message": error_msg,
                       "error_type": STRUCTURED_OUTPUT_UNSUPPORTED}
                st.end = StepEnd.RUN
                return
            # A schema-less JSON mode says nothing about the shape, and a model without
            # the field hears of the format only here. Once, as long as it stays in the
            # history, and before this step's budget note, which has to stay the last
            # thing the model reads.
            messages = run.messages
            if (st.wire_format is None or response_format.type == JSON_OBJECT) and not any(
                    getattr(m, "injected_by", None) == FORMAT_NOTE and m.content == run.format_note_text
                    for m in messages):
                note = self._structured_output_note(run.format_note_text, FORMAT_NOTE)
                if st.budget_note is not None and messages and messages[-1] is st.budget_note:
                    messages.insert(len(messages) - 1, note)
                else:
                    messages.append(note)
                run.context.messages = messages
        # A tool the history calls goes out with its schema: the hooks
        # may have written calls in since the run started (tool_preload),
        # as appended messages may. Loads nothing when nothing is new.
        if run.context.deferred_tools is not None:
            run.context.deferred_tools.restore(run.messages, run.tools_schema)

    async def _reasoning_progress(self: Agent, run: LoopState, st: StepState,
                                  text: str, chars: int, previous: int) -> None:
        # current_llm is read at call time: after a fallback switch the
        # hooks see the model that is actually thinking.
        await self._hook_manager.execute_llm_progress_hooks(
            reasoning_text=text, reasoning_chars=chars,
            previous_reasoning_chars=previous, step=st.step,
            request_id=run.request_id, session_id=run.session_id, llm=st.current_llm)

    async def _attempt_llm_call(self: Agent, run: LoopState, st: StepState):
        """One call on the step's current LLM: its events go out as they come, all but the
        thinking_complete, which is kept back (``st.pending_thinking_complete``) until it is
        settled that the answer is no error."""
        st.pending_thinking_complete = None
        async with contextlib.aclosing(self._call_llm_with_streaming(
                llm=st.current_llm,
                messages=run.messages,
                tools_schema=run.tools_schema,
                cancellation_token=run.main_token,
                step=st.step,
                yield_pending_status_fn=run.pending_status_events,
                status_scope=run.status_worker,
                watch_reasoning=st.current_llm is not st.reasoning_loop_llm,
                on_reasoning_progress=(functools.partial(self._reasoning_progress, run, st)
                                       if self._hook_manager.wants_llm_progress()
                                       else None),
                response_format=st.wire_format,
            )) as events:
            async for event in events:
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
                    llm_out: Dict[str, Any] = {"assistant": copy.deepcopy(event["assistant"])}
                    # Preserve usage data if present in event
                    if "usage" in event:
                        llm_out["usage"] = event["usage"]
                    # ...and finish_reason, which the truncation guard
                    # below reads off llm_out.
                    if event.get("finish_reason"):
                        llm_out["finish_reason"] = event["finish_reason"]
                    st.llm_out = llm_out
                    # Store event — DON'T yield yet, check for upstream errors first
                    st.pending_thinking_complete = event

    async def _on_upstream_error(self: Agent, run: LoopState, st: StepState, error_info: Dict[str, Any]):
        """An error in the body of an answer: the same model once more, then the next fallback;
        with the chain used up, the error ends the run. Retried unless ``st.end`` is set."""
        error_msg = error_info.get("message", "Unknown LLM error")
        error_type = error_info.get("type", "unknown")
        logger.warning(f"LLM returned upstream error: {error_type} - {error_msg}")
        # Billed all the same: the failed call's usage still
        # reaches the caller's sum of the turn -- its content does not.
        pending_thinking_complete = st.pending_thinking_complete
        if pending_thinking_complete and pending_thinking_complete.get("usage"):
            yield _name_the_model({
                "type": "thinking_complete",
                "step": pending_thinking_complete.get("step"),
                "assistant": {},
                "usage": pending_thinking_complete["usage"],
            }, st.current_llm)

        if (not str(error_type).startswith("content_filter")
                and not error_info.get("retried")
                and not any(c is st.current_llm for c in st.body_error_retried)):
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
            st.body_error_retried.append(st.current_llm)
            logger.warning(
                f"[{self.name}] Upstream error from LLM, "
                f"asking {getattr(st.current_llm, 'model', '?')} once more")
            await run.status_worker.progress(
                f"LLM error ({error_type}), retrying once",
                meta={"step": st.step + 1})
            return
        taken = self._take_fallback(run, st)
        if taken:
            fallback_profile, fallback_llm = taken
            logger.warning(
                f"[{self.name}] Upstream error from LLM, "
                f"switching to fallback: {fallback_profile}"
            )
            await run.status_worker.progress(
                f"LLM error ({error_type}), switching to {fallback_profile}",
                meta={"step": st.step + 1, "fallback": fallback_profile}
            )
            # No block, like the 5xx path it is the twin of: an
            # upstream error says the gateway stumbled, not that
            # this LLM is gone. Rescues THIS request; the next
            # starts on the original again.
            # Also swap the run's base LLM so hooks use the
            # fallback too — when the BASE failed. An escalation
            # model that failed says nothing about the base; only
            # this step is rescued.
            self._switch_to_fallback(run, st, fallback_profile, fallback_llm, swap_base=True)
            return  # Retry LLM call with fallback in same step
        # The chain is used up — hard error
        yield {"type": "error", "message": error_msg, "error_type": error_type}
        st.end = StepEnd.RUN

    async def _on_reasoning_loop(self: Agent, run: LoopState, st: StepState, e: ReasoningLoopError) -> None:
        """The thinking of the call went in circles: the same model once more, unwatched."""
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
        current_llm = st.current_llm
        st.reasoning_loop_llm = current_llm
        logger.warning(
            "[%s] Reasoning loop after %d characters of thinking "
            "(%s) — retrying the same model once: %s",
            self.name, e.characters, e.reason,
            getattr(current_llm, "model", "?"))
        await run.status_worker.progress(
            "Thinking went in circles, retrying once",
            meta={"step": st.step + 1, "reasoning_characters": e.characters})

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
                                - st.call_started) * 1000,
                "error": f"reasoning loop aborted: {e.reason}",
                "finish_reason": "reasoning_loop_aborted",
                "response_data": {"reasoning_loop": {
                    "characters": e.characters,
                    "reason": e.reason,
                }},
            })

    async def _fall_back_on_rate_limit(self: Agent, run: LoopState, st: StepState,
                                       e: LLMRateLimitError | LLMQuotaExhaustedError) -> bool:
        """A rate limit or an exhausted quota: the LLM is blocked, the step moves to the next
        fallback. False (logged) when none is left -- the error goes up."""
        is_quota_exhausted = isinstance(e, LLMQuotaExhaustedError)
        reason = "quota exhausted" if is_quota_exhausted else "rate limit hit"
        # A block of the LLM, not of this agent: every agent walks
        # around it until it runs out or the LLM answers someone. A
        # rate limit starts short and grows while the LLM keeps
        # failing; an exhausted quota does not get better in a minute.
        pause = model_health.block(
            st.current_llm, max_pause=run.max_block_seconds,
            rate_limit=not is_quota_exhausted, retry_after=e.retry_after,
            asked_at=st.health_asked_at, reason=f"{reason}, seen by {self.name}")
        taken = self._take_fallback(run, st)
        if taken:
            fallback_profile, fallback_llm = taken
            logger.warning(
                f"[{self.name}] {e.__class__.__name__}: {e}. "
                f"Switching to fallback profile: {fallback_profile}"
            )
            retry_in = f", retry in {pause:.0f}s" if pause and pause >= 1 else ""
            await run.status_worker.progress(
                f"{reason.capitalize()}, switching to {fallback_profile}{retry_in}",
                meta={"step": st.step + 1, "fallback": fallback_profile, "blocked_seconds": pause}
            )
            # No swap of the run's base: the next step asks the
            # blocks again and returns to the base once it is free.
            self._switch_to_fallback(run, st, fallback_profile, fallback_llm, swap_base=False)
            return True
        logger.error(f"[{self.name}] No fallback profiles available, {reason}")
        return False

    async def _fall_back_on_server_error(self: Agent, run: LoopState, st: StepState,
                                         e: LLMServerError) -> bool:
        """A 5xx: the step moves to the next fallback, no block. False (logged) when none is left."""
        # 5xx server errors (e.g. DeepSeek 504) — try fallback, but no block.
        # Server errors are transient outages; the primary LLM should be retried next time.
        taken = self._take_fallback(run, st)
        if taken:
            fallback_profile, fallback_llm = taken
            logger.warning(
                f"[{self.name}] Server error {e.status_code} from {e.model}: {e}. "
                f"Switching to fallback profile: {fallback_profile}"
            )
            await run.status_worker.progress(
                f"Server error {e.status_code}, switching to {fallback_profile}",
                meta={"step": st.step + 1, "fallback": fallback_profile}
            )
            # Request-scoped swap when the BASE failed, like its
            # twins (upstream error, connection error): without it
            # every following step started on the failing base
            # again, its hooks sizing the context for a model the
            # call never reached.
            self._switch_to_fallback(run, st, fallback_profile, fallback_llm, swap_base=True)
            return True
        logger.error(f"[{self.name}] No fallback profiles available, server error unrecoverable")
        return False

    async def _fall_back_on_transport_error(self: Agent, run: LoopState, st: StepState,
                                            e: Exception) -> bool:
        """A transport error or a 4xx: the step moves to the next fallback, an endpoint-level 4xx
        blocks the LLM first. False (logged) when none is left, and when this machine ran out of
        file descriptors, which no profile can help with."""
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
            return False
        kind = (f"HTTP {status_code}" if status_code
                else "Connection/transport error")
        if endpoint_level:
            model_health.block(
                st.current_llm, max_pause=run.max_block_seconds, rate_limit=False,
                asked_at=st.health_asked_at, reason=f"{kind}, seen by {self.name}")
        taken = self._take_fallback(run, st)
        if taken:
            fallback_profile, fallback_llm = taken
            logger.warning(
                f"[{self.name}] {kind} from LLM: {e}. "
                f"Switching to fallback profile: {fallback_profile}"
            )
            await run.status_worker.progress(
                f"Connection error, switching to {fallback_profile}",
                meta={"step": st.step + 1, "fallback": fallback_profile}
            )
            # Request-scoped swap when the BASE failed (like the
            # upstream-error and 5xx paths): without it, EVERY
            # following step retries the dead endpoint first
            # (~connect timeout x retries per step).
            self._switch_to_fallback(run, st, fallback_profile, fallback_llm, swap_base=True)
            return True
        logger.error(f"[{self.name}] No fallback profiles available, connection error unrecoverable")
        return False
