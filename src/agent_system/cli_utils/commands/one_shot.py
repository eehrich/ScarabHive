"""The one-shot run of `agent-cli run`: how it is stopped, shown, and printed.

What happens between an open session and its save when the run is not the
chat: the task runs once, collected quietly with --raw or streamed --
status lines, tool calls, thinking, errors and the answer as they arrive --
and the result is printed after. ``RunControl`` carries what the stop needs
to reach (the request id, the presence hold) and what the printing after the
run must not repeat (the errors the stream showed). The chat has a renderer
of its own (cli_utils/chat.py); this is the terminal side of a single run,
kept apart from the steps in run.py that set it up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

import yaml

from ...core.cancellation import get_cancellation_manager
from ...llm.models import ChatMessage
from ...tools.status import status_bus
from ..agent_runner import say_cancelled
from ..common import colorize, get_phase_color_map, show_answer, supports_color
from ..event_loop import run_async

if TYPE_CHECKING:
    from ...servers.agent.server import Agent

logger = logging.getLogger(__name__)


class RunControl:
    """A one-shot run's request id, whether its user stopped it, and the
    errors the stream already showed.

    Ctrl-C is its user stopping the run. The hold says so as it lets go
    (core/session_presence.py): the session is marked, and nothing starts it
    again by itself -- whenever the run lets go, in its own frames before this
    hears of it or at exit after. The run's token stops its tool calls and
    sub-agents.
    """

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.stopped = False
        # The errors the stream showed as they came; the block after the run names the rest (an exception has no event).
        self.streamed_errors: list = []

    def stop(self) -> None:
        """Note the stop and cancel the run's request: its tool calls, its sub-agents."""
        self.stopped = True
        get_cancellation_manager().cancel_request(self.request_id)

    def run(self, coro: Any) -> dict:
        """run_async, with a Ctrl-C out of the loop taken as the user's stop."""
        try:
            return run_async(coro)
        except KeyboardInterrupt:
            self.stop()   # out of the loop, the run left where it was
            raise


def _print_status(event: Any) -> None:
    """One status line from the status bus."""
    # The enum's value: the raw StatusPhase printed as "[StatusPhase.END]"
    # and never matched the colours.
    phase = getattr(event, "phase", "progress")
    phase = phase.value if hasattr(phase, "value") else str(phase)
    colour = supports_color()
    shown = colorize(phase, get_phase_color_map().get(phase, "34")) if colour else phase
    line = f"[{shown}] {event.server}: {event.message}"
    if colour and (phase == "error" or event.level == "error"):
        line = colorize(line, "31")
    elif colour and event.level == "warning":
        line = colorize(line, "33")
    print(line)


async def stream_run(
    agent: Agent,
    task: Union[str, ChatMessage],
    control: RunControl,
    session_id: str,
    *,
    verbose: bool,
    show_tools: bool = False,
    show_status: bool = True,
    llm_override=None,
    llm_profile_info: Optional[str] = None
) -> dict:
    """Run *task* and show it as it goes: status lines, tool calls, thinking.

    The run is collected by collect_final_result, as --raw's is -- the one
    consumer of run_events. This mode kept its own copy of that loop, and
    the copy left the errors out of the result: a run that ended in one
    looked finished. The answer and the errors show as they arrive -- a
    Ctrl-C mostly lands out of the event loop, and after the run only the
    stop is printed then.
    """
    status_queue = await status_bus.subscribe() if show_status else None

    async def _status_subscriber() -> None:
        try:
            while True:
                _print_status(await status_queue.get())
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug(f"Status subscriber error: {e}")

    status_task = asyncio.create_task(_status_subscriber()) if status_queue else None

    # Whether thinking tokens were streamed this step: a non-streaming LLM
    # emits thinking_complete without any thinking_delta, and closing the
    # block then printed a stray blank line per call.
    thinking_streamed = False

    def _close_thinking_block() -> None:
        """Reset the colour and end the streamed line of a thinking block."""
        nonlocal thinking_streamed
        if thinking_streamed:
            if supports_color():
                print("\x1b[0m", end="")
            print()
        thinking_streamed = False

    async def _show(ev: Dict[str, Any]) -> None:
        nonlocal thinking_streamed
        t = ev.get("type")
        # "mcp_*" is the old name until every deployed side is new (rename 17.09.2026)
        if t in ("tool_call", "mcp_call") and show_tools:
            header = f"TOOL CALL -> server={ev.get('server')} action={ev.get('action')}"
            print(colorize(header, "36") if supports_color() else header)
            print(json.dumps(ev.get("params") or {}, indent=2, ensure_ascii=False, default=str))
        elif t in ("tool_result", "mcp_result") and show_tools:
            header = f"TOOL RESULT <- server={ev.get('server')} action={ev.get('action')}"
            print(colorize(header, "32") if supports_color() else header)
            print(json.dumps(ev.get("result"), indent=2, ensure_ascii=False, default=str))
        elif t == "thinking_delta":
            # Gated on show_status: with --no-status stdout carries the
            # result only (`>out.json` stays clean).
            delta = ev.get("delta", "")
            if delta and show_status:
                # Open the grey block once, not around every token: one
                # escape pair per delta buried the text in ESC[90m/ESC[0m.
                if not thinking_streamed:
                    thinking_streamed = True
                    if supports_color():
                        print("\x1b[90m", end="", flush=True)  # Dark gray
                print(delta, end="", flush=True)
        elif t == "thinking_complete":
            _close_thinking_block()
        elif t == "thinking":
            # A new step: close a block the last one left open, or the grey
            # leaks onward.
            _close_thinking_block()
            if verbose:
                print(f"[LLM] thinking (step {ev.get('step')})")
        elif t == "error":
            # As it comes: a Ctrl-C after it (the run still saves and runs
            # its hooks) would otherwise leave a failed run without a word.
            control.streamed_errors.append(ev.get("message"))
            err = f"ERROR: {ev.get('message')}"
            print(colorize(err, "31") if supports_color() else err)
        elif t == "final" and ev.get("summary"):
            print("", flush=True)
            show_answer(ev["summary"])

    from ...servers.agent.result_utils import collect_final_result
    try:
        result = await collect_final_result(
            agent, task, request_id=control.request_id, session_id=session_id,
            llm_override=llm_override, llm_profile_info_override=llm_profile_info,
            on_event=_show)
    except asyncio.CancelledError:
        result = {"task": task, "cancelled": True}
    finally:
        # A block left open would bleed grey into the shell prompt.
        _close_thinking_block()
        # Drain what is queued before the subscriber goes: the final
        # PHASE_END can be published and not yet printed.
        if status_queue:
            try:
                while not status_queue.empty():
                    _print_status(status_queue.get_nowait())
            except Exception as e:
                logger.debug(f"Exception while draining status queue: {e}")
        if status_task and not status_task.done():
            status_task.cancel()

    if result.get("cancelled"):
        control.stop()   # a Ctrl-C collect_final_result caught, or the run's own stop
        say_cancelled()
    return result


def run_one_shot(agent: Agent, task: Union[str, ChatMessage], control: RunControl,
                 session_id: str, *, raw: bool, verbose: bool, show_tools: bool,
                 show_status: bool, llm_override: Any, llm_profile_info: Optional[str]) -> dict:
    """Run *task* once: collected with --raw, else streamed with status."""
    if raw:
        # Raw mode: use run_events with result collection
        from ...servers.agent.result_utils import collect_final_result

        result = control.run(collect_final_result(agent, task, request_id=control.request_id, session_id=session_id, llm_override=llm_override, llm_profile_info_override=llm_profile_info))
        if result.get("cancelled"):
            control.stopped = True   # a Ctrl-C collect_final_result caught
        return result
    return control.run(stream_run(agent, task, control, session_id, verbose=verbose, show_tools=show_tools, show_status=show_status, llm_override=llm_override, llm_profile_info=llm_profile_info))


def _pretty_print_result(res: dict, *, show_tools: bool, verbose: bool, streamed_errors: list) -> None:
    """Tool calls and errors after the run. The summary is not repeated:
    the stream printed it as it arrived."""
    # Calls (print first so summary appears at the end, only when show_tools is True)
    calls = res.get("calls", []) or []
    if calls and show_tools:
        print("")
        print("Tool calls:")
        for c in calls:
            srv = c.get("server")
            action = c.get("action")
            header = f"- {srv} :: {action}"
            if supports_color():
                header = colorize(header, "36")
            print(header)
            result_obj = c.get("result")
            # Render result as YAML for human readability when possible
            try:
                yaml_text = yaml.safe_dump(result_obj, allow_unicode=True, sort_keys=False)
                for line in yaml_text.rstrip().splitlines():
                    print(f"    {line}")
            except Exception as e:
                # Fallback to JSON-ish string
                logger.debug(f"Failed to YAML dump result: {e}")
                try:
                    j = json.dumps(result_obj, ensure_ascii=False)
                    print(f"    {j}")
                except Exception as e2:
                    logger.debug(f"Failed to JSON dump result: {e2}")
                    print(f"    {str(result_obj)}")

    # Errors the stream did not show already
    errors = [e for e in res.get("errors") or [] if e not in streamed_errors]
    if errors:
        print("")
        print(colorize("Errors:", "31") if supports_color() else "Errors:")
        for e in errors:
            print(f"  - {e}")

    # If verbose, print raw JSON for debugging
    if verbose:
        print("")
        print(colorize("Raw result JSON:", "35") if supports_color() else "Raw result JSON:")
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))


def print_result(result: dict, *, raw: bool, show_tools: bool, verbose: bool,
                 streamed_errors: list) -> None:
    """Human-readable final output, or the result as JSON with --raw."""
    # If raw requested, print JSON and exit. Ensure output is flushed so
    # test harnesses and non-interactive environments capture it.
    if raw:
        # default=str: a tool value that is not plain JSON (a set, a date) must
        # not turn a finished run into exit 1 at the very last print.
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str), flush=True)
    else:
        _pretty_print_result(result, show_tools=show_tools, verbose=verbose,
                             streamed_errors=streamed_errors)
        try:
            sys.stdout.flush()
        except Exception:
            pass
