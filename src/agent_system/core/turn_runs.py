"""The turns agents run, told as runs: who works now, on which step, below whom.

A panel's AI task and work that outlives its turn already say so in the run events (``agent_system.run_events``:
``plugins/agent_tasks.py``, ``core/background_runs.py``). A turn in a chat and a sub-agent's run did not: that a
session was at work showed as a dot beside it, what it was doing only inside its chat, at the place of the call --
and a sub-agent three levels down nowhere else at all.

Here every turn is a run, under the id of the session it works in (as a panel's run is), with the session whose
turn started it as ``parent_session``: a chat's turn (``source`` ``chat``) and, below it, the sub-agents it called
(``sub_agent``), theirs below them. Its steps are the model thinking and the tools it calls, in a line each; its
end is how the turn ended. One session, one run: its next turn begins the same run again.

Told where a person is at it. The process says so at its start (``report_turns``: the server, agent-cli), and of
its turns those a person started are told -- a turn somebody attends (a chat in the browser or at the terminal, a
session woken for its person) -- with whatever runs below a run that is told. What a program sends the server (a
worker's job, a call through the OpenAI API) and what runs below it tells nothing: jobs by the thousand have their
own view, and the run events are one file every process writes. ``AGENT_TURN_RUNS=0`` silences a process that
would tell, ``=1`` makes any process tell every turn.

A view, never the turn's master: whatever fails here is logged, and the turn goes on.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Optional

from agent_system.core.background_runs import run_who
from agent_system.run_events import TURN_SOURCES, RunReporter, reporter_for

logger = logging.getLogger(__name__)

#: The environment variable that overrides what the process said at its start: 0 tells nothing, 1 tells.
ENV = "AGENT_TURN_RUNS"
#: The ``source`` of a turn nobody's turn started, and of one a turn started (a sub-agent, an agent called as a tool).
CHAT, SUB_AGENT = "chat", "sub_agent"
#: Their ``task``: a short key every event carries (the title says what the turn is about).
TASK = "turn"
#: The step a turn is on while its model works on the next one.
THINKING = "Thinking"

_on = False
#: The turns of this process at work, by session id.
_TURNS: dict[str, "Turn"] = {}
#: The end being written for a session's turn (RunReporter.end_detached): the session's next turn waits for it --
#: begun before that end was written, the end would end the new turn.
_ENDING: dict[str, threading.Thread] = {}


def report_turns(on: bool = True) -> None:
    """Said once by a process a person may be at (the server, agent-cli): its turns are told as runs."""
    global _on
    _on = on


def telling() -> str:
    """Which turns this process tells: ``""`` none, ``attended`` those a person started and what runs below a run
    that is told, ``all`` every one."""
    forced = os.environ.get(ENV, "").strip().lower()
    if forced in ("0", "off", "false", "no"):
        return ""
    if forced in ("1", "on", "true", "yes"):
        return "all"
    return "attended" if _on else ""


def title_of(task: str) -> str:
    """What a turn is about, at a glance: the first line of what it was asked."""
    return next((line.strip() for line in str(task or "").splitlines() if line.strip()), "")


class Turn:
    """One turn at work: its reporter, and the steps still being written."""

    def __init__(self, reporter: RunReporter, attended: bool, request_id: str = "") -> None:
        self.reporter = reporter
        self.attended = attended
        self.request_id = request_id
        self._steps: set[asyncio.Task] = set()

    def step(self, label: str, labels: Optional[dict[str, str]] = None) -> None:
        """The turn goes on to a step. Not waited for: a tool does not start later because the run events are
        busy -- the reporter writes its events one after the other all the same."""
        if self.reporter.ended:
            return
        try:
            writing = asyncio.ensure_future(self.reporter.step(label, labels=labels))
        except RuntimeError:      # no loop running: nothing to say it with
            return
        self._steps.add(writing)
        writing.add_done_callback(self._steps.discard)

    def end(self, state: str, text: str = "", seen: bool = False) -> None:
        for writing in list(self._steps):
            writing.cancel()      # a step after the end is refused anyway; pending, it would outlive its loop
        thread = self.reporter.end_detached(*self.reporter.ended_payload(state, text), seen=seen)
        if thread is not None:
            for session_id in [key for key, ending in _ENDING.items() if not ending.is_alive()]:
                del _ENDING[session_id]
            _ENDING[self.reporter.run_id] = thread

    def watched(self) -> bool:
        """Whether its person has the turn in front of them now: started by them, and still read -- at the
        terminal, or by a stream of its job (a tab closed since, a session woken while nobody looked: not)."""
        if not self.attended:
            return False
        try:
            from agent_system.services.background_job_manager import get_background_job_manager
            unread = get_background_job_manager().unread_for(self.request_id)
        except Exception:  # noqa: BLE001 - no job manager here (a CLI): read at its terminal
            return True
        return not unread      # None: no job, read inline for as long as it runs; 0.0: a stream reads it


async def started(*, agent: str, system_config: Any, session_id: str, user_id: str, task: str, parent: str,
                  attended: bool, request_id: str = "") -> Optional[Turn]:
    """A turn begins in ``session_id``; ``parent``: the session whose turn started it, "" for a turn of its own.
    None where it is not told (``telling``), and for a session whose run is somebody else's to tell: a panel's
    task at work there, or one that waits for its person -- a result to check, a failure not seen yet. A chat in
    that session is not what they wait for."""
    which = telling()
    if not which or not session_id or session_id in _TURNS or reporter_for(session_id) is not None:
        return None
    if parent == session_id:
        parent = ""
    if which != "all" and not ((parent in _TURNS or reporter_for(parent) is not None) if parent else attended):
        return None
    reporter: Optional[RunReporter] = None
    try:
        before = _ENDING.get(session_id)
        if before is not None and before.is_alive():
            await asyncio.to_thread(before.join, 30.0)
        reporter = RunReporter(session_id, agent, who=run_who(system_config, user_id), title=title_of(task),
                               source=SUB_AGENT if parent else CHAT, task=TASK, parent=parent, listed=False)
        row = await asyncio.to_thread(reporter.store.run, session_id)
        if row is not None and row["needs_you"] and row["source"] not in TURN_SOURCES:
            return None
        # Not cut off half way: cancelled while the start is being written, the run would stand at work with
        # nobody to end it. Written to its end, then ended.
        starting = asyncio.ensure_future(reporter.started())
        try:
            begun = await asyncio.shield(starting)
        except asyncio.CancelledError:
            def ended_at_once(_done: Any) -> None:
                thread = reporter.end_detached("run.cancelled")
                if thread is not None:
                    _ENDING[session_id] = thread      # the session's next turn waits for it, as for any end

            starting.add_done_callback(ended_at_once)
            raise
        if begun is None:
            return None
        reporter.keep_alive()
        turn = _TURNS[session_id] = Turn(reporter, attended, request_id)
        return turn
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.exception("turn in %s: not told as a run", session_id)
        return None


def ending(session_id: str) -> bool:
    """Whether the turn that worked in ``session_id`` is writing its end: over, and not yet said so."""
    thread = _ENDING.get(session_id or "")
    return thread is not None and thread.is_alive()


def step(session_id: str, label: str, labels: Optional[dict[str, str]] = None) -> None:
    """The turn working in ``session_id`` goes on to a step; nothing where none is told."""
    turn = _TURNS.get(session_id or "")
    if turn is not None:
        turn.step(label, labels)


def tool_step(session_id: str, calls: list[tuple[str, str, Any]]) -> None:
    """The turn calls tools: ``calls`` as (server, tool, arguments). One step for all the calls of one answer of
    the model -- they run side by side -- named after the first."""
    if not calls or (session_id or "") not in _TURNS:
        return
    server, tool, arguments = calls[0]
    label = tool_label(server, tool, arguments)
    step(session_id, f"{label} (+{len(calls) - 1})" if len(calls) > 1 else label)


def tool_label(server: str, tool: str, arguments: Any) -> str:
    """A tool call in a line: its server, the tool without the server's prefix, and what it is called on -- the
    first short argument that says so (a path, a query, a command)."""
    name = str(tool or "")
    if server and name.startswith(f"{server}_"):
        name = name[len(server) + 1:]
    what = ""
    if isinstance(arguments, dict):
        for key, value in arguments.items():
            if isinstance(value, str) and value.strip() and not key.startswith("_"):
                what = " ".join(value.split())
                break
    line = f"{server}: {name}" if server and name and name != server else (name or str(server or "tool"))
    return f"{line} · {what}" if what else line


def ended(turn: Optional[Turn], session_id: str, results: dict[str, Any], cancelled: bool) -> None:
    """The turn ended -- as its results say: cancelled, done with its answer, or failed with its last error. A
    turn that ended without any of them was left by whoever read it (a client gone): cancelled.

    A failure needs its person (run_events.needs_you) only where nobody saw it: not one in a chat somebody
    watches as it ends, and not a sub-agent's -- the turn that called it is told, and says what comes of it."""
    if turn is None:
        return
    if _TURNS.get(session_id) is turn:
        del _TURNS[session_id]
    try:
        errors = list(results.get("errors") or [])
        if cancelled:
            turn.end("cancelled")
        elif "summary" in results:
            turn.end("done", str(results.get("summary") or ""))
        elif errors:
            turn.end("failed", str(errors[-1]), seen=bool(turn.reporter.parent) or turn.watched())
        else:
            turn.end("cancelled")
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.exception("turn in %s: its end was not told", session_id)


async def cancel(session_id: str) -> bool:
    """Stop the turn of this process working in ``session_id``, as the app stops any request (its sub-requests
    with it): a chat's turn, or one sub-agent below it -- the turn that called it goes on, and is told. False
    when no turn of this process works there."""
    turn = _TURNS.get(session_id or "")
    if turn is None or not turn.request_id:
        return False
    from agent_system.services.background_job_manager import get_background_job_manager

    # forced after a while, as a panel's task is: a turn stuck in blocking work would never see the token
    return bool(await get_background_job_manager().cancel_job(turn.request_id, force_timeout=5.0))


def settle(timeout: float = 5.0) -> None:
    """Wait for the ends being written (tests)."""
    for thread in list(_ENDING.values()):
        thread.join(timeout)
