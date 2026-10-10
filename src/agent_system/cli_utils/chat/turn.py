"""One turn of the chat: the task through the agent, and every way it ends.

run_chat_turn feeds the agent's event stream into the renderer and adds up
what its calls cost. _execute_turn runs it on the REPL's loop, claimed for
the session and with the type-ahead beside it; _cancel_turn is the two-stage
Ctrl-C -- a graceful cancel through the agent first, a hard one when the
grace runs out. _render_answer shows what came back.

Its own module because the turn is the one part of the chat that runs the
agent; the REPL (repl.py) decides WHAT runs and reads the result.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Any, Optional

from ...core.session_presence import note_stop
from ...llm.pricing import normalize_usage
from ..common import show_answer
from . import context, interruptible, token_usage, typeahead

if TYPE_CHECKING:
    from .context import _ChatContext
    from .display import ChatRenderer
    from .prompt_input import _PromptEditor

logger = logging.getLogger(__name__)


# Graceful-cancel budget after Ctrl-C before the turn task is cancelled hard.
_CANCEL_GRACE_S = 15.0


async def run_chat_turn(
    agent: Any,
    task: Any,
    session_id: str,
    renderer: ChatRenderer,
    *,
    show_status: bool = True,
    llm_override: Any = None,
    llm_profile_info: Optional[str] = None,
    state: Optional[dict] = None,
) -> dict:
    """Run one task through the agent, feeding all output into the renderer.

    `state` is a caller-owned dict: the request_id is written into it as soon
    as the start event arrives, so a Ctrl-C handler outside this coroutine can
    cancel the in-flight request even though this coroutine never returned.
    """
    from ...tools.status import status_bus

    result: dict[str, Any] = {"summary": None, "cancelled": False, "errors": [],
                              "usage": {}}
    last_call_usage: Any = None  # to spot the final event repeating it
    call_key: Optional[tuple[Optional[str], bool]] = None  # who answered it
    if state is None:
        state = {}
    # The same dict: a turn cancelled from outside never returns, and what
    # its calls cost has to reach the session total anyway.
    state["usage"] = result["usage"]

    queue = await status_bus.subscribe() if show_status else None
    consumer: Optional[asyncio.Task] = None

    if queue is not None:
        async def _consume() -> None:
            while True:
                event = await queue.get()
                try:
                    renderer.handle_status(event)
                    # what the run asks the person at the terminal (_execute_turn says whether it may)
                    questions = state.get("questions")
                    if questions is not None:
                        questions.see(event)
                except Exception:
                    logger.debug("Renderer failed on status event", exc_info=True)

        consumer = asyncio.create_task(_consume())

    try:
        async for ev in agent.run_events(
            task,
            request_id=state.get("request_id"),
            session_id=session_id,
            llm_override=llm_override,
            llm_profile_info_override=llm_profile_info,
        ):
            t = ev.get("type")
            if t == "start":
                state["request_id"] = ev.get("request_id")
            elif t in ("thinking_delta", "reasoning_delta"):
                # Gated like the one-shot's token stream: --no-status means
                # stdout carries answers only.
                if show_status:
                    renderer.thinking_delta()
            elif t == "thinking_complete":
                renderer.thinking_done()
                assistant = ev.get("assistant") or {}
                content = assistant.get("content")
                # Only intermediate steps (with tool calls) are narrated here;
                # the last step's content arrives again as the final event and
                # would show twice otherwise.
                if show_status and assistant.get("tool_calls") and isinstance(content, str):
                    renderer.narration(content)
                # Usage rides on thinking_complete per LLM call; sum them so a
                # multi-step turn reports the whole turn, not just the last call.
                call_usage = ev.get("usage")
                if call_usage is not None:
                    # Only a call that REPORTED usage may set the key: the
                    # server also emits thinking_complete without usage (and
                    # with its own model on it), and a later step back on the
                    # base model would then re-label the escalated call's
                    # tokens -- and size the context bar with the wrong window.
                    call_key = token_usage._call_pricing_key(agent, llm_override, ev)
                    token_usage._accumulate_usage(result["usage"], call_usage, *call_key)
                # Only a call that REPORTED usage becomes the reference. The
                # server emits thinking_complete without it (server.py: the
                # empty-assistant branch, and both `if usage` guards), and
                # letting that reset the reference to None cost twice: the
                # context fill fell back to nothing, and the final event no
                # longer recognized itself as a repeat -- so the last call
                # was billed a second time.
                if call_usage is not None:
                    last_call_usage = call_usage
            elif t == "heartbeat":
                if show_status:
                    renderer.thinking_tick()
            elif t == "final":
                result["summary"] = ev.get("summary") or ""
                # The final event usually REPEATS the last LLM call's usage
                # (server.py: final_event["usage"] = llm_out["usage"]), which
                # already arrived as thinking_complete -- summing both counted
                # that call twice and inflated every turn.
                # A final whose usage no thinking_complete reported is a call of
                # its own and must count (the max-steps call was one, before it
                # became a regular step). Same payload => the repeat; anything
                # else => a real extra call.
                final_usage = ev.get("usage")
                if final_usage is not None and final_usage != last_call_usage:
                    # The final event names no model; the last call's does.
                    token_usage._accumulate_usage(result["usage"], final_usage, *(
                        call_key or token_usage._call_pricing_key(agent, llm_override, ev)))
                    last_call_usage = final_usage
            elif t == "error":
                message = str(ev.get("message") or "unknown error")
                result["errors"].append(message)
                renderer.error_line(f"ERROR: {message}")
                from ...servers.agent.server import refused_before_the_run

                if refused_before_the_run(ev):  # it ran nothing: the chat saves nothing after it
                    # in the state too: a Ctrl-C that cancels the turn drops this result (_cancel_turn)
                    result["refused"] = state["refused"] = ev["error_type"]
            elif t == "cancelled":
                result["cancelled"] = True
            elif t == "end":
                break
            # Everything else (thinking step markers, continuation, ...) is
            # progress bookkeeping without display value in this mode.
    finally:
        if queue is not None:
            # Drain deterministically: the final PHASE_END may be queued but
            # not yet consumed when run_events finishes (same race the
            # one-shot path guards against).
            try:
                while not queue.empty():
                    renderer.handle_status(queue.get_nowait())
            except Exception:
                logger.debug("Error draining status queue", exc_info=True)
            if consumer is not None:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            try:
                status_bus.unsubscribe(queue)
            except Exception:
                logger.debug("Failed to unsubscribe status queue", exc_info=True)
        renderer.close()

    # How full the window is after this turn: the LAST call's prompt (the whole
    # history as the model saw it) plus what it answered. Summing every call
    # would report the turn's throughput instead -- a multi-step turn sends the
    # same history again and again.
    if last_call_usage is not None:
        call = normalize_usage(last_call_usage)
        result["context_tokens"] = call.prompt_tokens + call.completion_tokens
    # The window of the client the turn ran on: after --llm or /model that is
    # the override, and the fill was computed against the old model's size.
    # Only when that client is also the one that ANSWERED: a fallback runs on
    # a third client whose window nobody here knows, and dividing its tokens
    # by this one states a fill that is not true. Without a window the footer
    # shows the tokens alone, which is what we actually know.
    client = llm_override or getattr(agent, "llm", None)
    window = getattr(client, "context_window", None)
    answered_by = call_key[0] if call_key else None
    if (isinstance(window, int) and window > 0
            and (answered_by is None or answered_by == getattr(client, "model", None))):
        result["context_window"] = window

    return result


def _execute_turn(loop: asyncio.AbstractEventLoop, ctx: _ChatContext,
                  task: str, renderer: ChatRenderer,
                  editor: Optional["_PromptEditor"] = None) -> dict:
    """One turn on the persistent loop, with two-stage Ctrl-C handling.

    ``editor`` travels in the turn state so that a line typed AHEAD, which is
    delivered to the running agent instead of going through the prompt, still
    reaches the input history.
    """
    # Named and claimed before it starts: a Ctrl-C before its run takes the
    # session stops it all the same.
    from ...utils.id import short_id
    from ...core.request_context import release_run_attended, set_run_attended
    from ..questions import TurnQuestions
    state: dict[str, Any] = {"editor": editor, "request_id": short_id()}
    named = state["request_id"]
    claimed = context._claim_turn(ctx, state["request_id"])
    # Type-ahead needs the live region to place its input line, so it rides
    # along with ANSI mode.
    reader = typeahead._KeyReader(active=renderer.ansi)
    # A person at the terminal: what the run asks them (ask_user, tool_approval)
    # is shown on a status line and answered with the next line typed -- so the
    # run is attended where both work, as the web chat's runs are.
    attended = reader.enabled and ctx.show_status
    if attended:
        state["questions"] = TurnQuestions(named, ctx.session_user, renderer.println, agent=ctx.entry_name)
    set_run_attended(named, attended)
    turn = loop.create_task(run_chat_turn(
        ctx.agent, task, ctx.session_id, renderer,
        show_status=ctx.show_status,
        llm_override=ctx.llm_override,
        llm_profile_info=ctx.llm_profile_info,
        state=state,
    ))
    poller = (loop.create_task(typeahead._poll_typed_input(reader, renderer, ctx, state))
              if reader.enabled else None)
    try:
        result = loop.run_until_complete(turn)
    except KeyboardInterrupt:
        result = _cancel_turn(loop, ctx, turn, state, renderer)
    except Exception as e:
        # One broken turn (LLM auth, network, agent bug) must not end the
        # whole chat: report it and hand the user the next prompt.
        logger.error("Chat turn failed: %s", e, exc_info=True)
        renderer.close()
        print(f"Turn failed: {e}", file=sys.stderr)
        result = {"summary": None, "cancelled": False, "errors": [str(e)],
                  "usage": state.get("usage") or {}}
    finally:
        # The reader owns terminal state on POSIX -- it has to be restored on
        # every exit, including Ctrl-C, or the shell stays in cbreak.
        typeahead._stop_typing(loop, reader, poller, renderer, state)
        release_run_attended(named)
        if claimed is not None:
            claimed.release(ctx.session_id, ctx.session_user)
        # What the turn registered under its request id (a tool call, a
        # preloaded tool, a sub-agent) goes with it, as the API lets go of its
        # request tree when the request ends.
        from ...core.request_context import release_request_user_tree
        for request_id in {named, state.get("request_id")} - {None, ""}:
            release_request_user_tree(request_id)
    for key in ("typed_queue", "typed_partial"):
        if state.get(key):
            result[key] = state[key]
    return result


def _cancel_turn(loop: asyncio.AbstractEventLoop, ctx: _ChatContext,
                 turn: "asyncio.Task", state: dict, renderer: ChatRenderer) -> dict:
    """Ctrl-C during a turn: graceful cancel first, hard cancel as fallback."""
    # The type-ahead still runs while the turn unwinds: a line typed now must not
    # answer a question of the run its person just stopped (an Allow beats the cancel).
    state.pop("questions", None)
    renderer.close()
    print("\nCancelling turn... (Ctrl-C again to force)", file=sys.stderr)
    request_id = state.get("request_id")
    if request_id:
        # Its user stopped it: noted, so the session is let go marked and nothing
        # starts it again by itself -- not the prompt's wake watcher, not leaving
        # the chat (core/session_presence/). Also when the Ctrl-C landed in the
        # run's own frames and it let go before this.
        note_stop(request_id)
        # Graceful: flips the cancellation token, the agent unwinds and
        # yields its cancelled/end events through the normal path.
        # KeyboardInterrupt is NOT an Exception -- a second Ctrl-C here has to
        # be caught explicitly or it escapes the REPL and kills the chat, the
        # opposite of the "again to force" we just promised.
        try:
            loop.run_until_complete(
                asyncio.wait_for(ctx.agent.cancel_request(request_id), timeout=5)
            )
        except KeyboardInterrupt:
            logger.debug("second Ctrl-C during graceful cancel")
        except Exception:
            logger.debug("cancel_request failed", exc_info=True)
    try:
        # wait_for cancels the task itself if the grace period runs out.
        result = loop.run_until_complete(asyncio.wait_for(turn, _CANCEL_GRACE_S))
        # Race: the turn may have COMPLETED between Ctrl-C and here -- the
        # agent was already finishing, its token gone, so no "cancelled"
        # came. A finished answer is shown, not discarded; the Ctrl-C still
        # counts for what was queued behind it.
        if result.get("summary") is None:
            result["cancelled"] = True
        result["interrupted"] = True
        return result
    except (KeyboardInterrupt, asyncio.TimeoutError, asyncio.CancelledError):
        # The biggest task in the file, and the one that must not be left
        # pending: its unwinding drains the status queue, unsubscribes and
        # closes the renderer -- inside whatever run_until_complete comes
        # next, drawing into the region the next prompt owns by then.
        interruptible._drain(loop, turn, "the turn")
    except Exception:
        logger.debug("Turn unwind failed", exc_info=True)
    renderer.close()
    # The turn's own result is gone with it; its usage lives on in the state, and a refusal before the run.
    return {"summary": None, "cancelled": True, "errors": [],
            "usage": state.get("usage") or {}, **({"refused": state["refused"]} if state.get("refused") else {})}


def _render_answer(renderer: ChatRenderer, summary: str) -> None:
    # rich prints straight to stdout, invisible to the region's offsets.
    renderer.commit()
    print()
    try:
        # As --color says (show_answer). A Ctrl-C here stops the drawing, not the save.
        show_answer(summary)
    except KeyboardInterrupt:
        print("\n(display interrupted)", file=sys.stderr)
    print()  # region is already committed; plain spacing line
