"""Stopping and restarting the server on an admin's request (POST /admin/server/stop and /restart).

Both end the server the way Ctrl+C does: SIGINT to uvicorn, which stops taking requests, lets the running ones
finish and runs the app's shutdown. A request that does not end -- a stream a browser keeps open, a run that
ignores its cancellation -- must not keep a button from working: after GRACE_SECONDS a second SIGINT hurries
uvicorn, and after as long again the process ends outright. (Measured 11.10.2026, Python 3.14 and a stream held
open: the second signal did not end it -- uvicorn waits in Server.wait_closed() for the connection -- the
outright end did, so such a stop takes twice the grace.)

A restart is the same stop followed by a new process, so new plugins, agents, skills and changed code are
loaded as on any start. On Windows the launcher that keeps the server on a console of its own
(own_console.relaunch_restarting) starts it again when it exits with RESTART_EXIT_CODE; elsewhere the process
replaces itself (execv), keeping its pid -- a container's main process stays the same. Only a server started
through agent_system.app.run() has that: one started as "uvicorn agent_system.app:build_app" can be stopped
here, and is restarted by whoever started it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time

from agent_system import own_console
from agent_system.own_console import RESTART_EXIT_CODE

logger = logging.getLogger(__name__)

ACTIONS = ("stop", "restart")
#: Seconds the server gets to end by itself before it is hurried, and once more before it is ended outright.
GRACE_SECONDS = 10.0
#: Seconds between the answer to the request and the signal: the answer has to reach the browser first.
ANSWER_SECONDS = 0.5

_lock = threading.Lock()
_pending: str | None = None
_managed = False


class AlreadyPending(RuntimeError):
    """The server is on its way down already."""


class RestartUnavailable(RuntimeError):
    """Nothing would start this server again."""


def mark_managed() -> None:
    """app.run() says so before it serves: what it started ends in finish(), which can start it again."""
    global _managed
    _managed = True


def can_restart() -> bool:
    """Whether a restart would come back: under the Windows launcher, or where the process can replace itself."""
    return _managed and (os.name != "nt" or own_console.serving())


def pending() -> str | None:
    """"stop" or "restart" once one was asked for, else None."""
    return _pending


def request(action: str, loop: asyncio.AbstractEventLoop) -> None:
    """Have the server stop (and for "restart" start again) once the caller's answer is out."""
    global _pending
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}, got {action!r}")
    if action == "restart" and not can_restart():
        raise RestartUnavailable("This server was not started by agent-api, so nothing would start it again. "
                                 "Restart it the way it was started.")
    with _lock:
        if _pending is not None:
            raise AlreadyPending(f"The server is already about to {_pending}.")
        _pending = action
    loop.call_later(ANSWER_SECONDS, _begin)


def _begin() -> None:
    signal.raise_signal(signal.SIGINT)  # uvicorn's own handler: the graceful stop of a Ctrl+C
    threading.Thread(target=_insist, name="lifecycle-insist", daemon=True).start()


def _insist() -> None:
    """The server did not end within the grace: hurry it, then end it."""
    time.sleep(GRACE_SECONDS)
    logger.warning("The server has not stopped after %.0f s: hurrying it", GRACE_SECONDS)
    signal.raise_signal(signal.SIGINT)  # uvicorn's second: no more waiting for open connections
    time.sleep(GRACE_SECONDS)
    logger.error("The server has not stopped after %.0f s: ending the process", 2 * GRACE_SECONDS)
    _leave(outright=True)


def finish() -> None:
    """After the server has stopped (uvicorn.run returned): for a restart, start anew."""
    if _pending == "restart":
        logger.info("Restarting the server")
        _leave(outright=False)


def _leave(outright: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    if _pending == "restart" and os.name != "nt":
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
    code = RESTART_EXIT_CODE if _pending == "restart" else 0
    if outright:
        os._exit(code)  # from a thread, with the main one stuck: sys.exit would only end this thread
    raise SystemExit(code)
