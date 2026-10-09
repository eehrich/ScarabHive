"""The command line's event loop: one for the whole process.

``agent-cli`` and ``agent-run`` run a sequence of coroutines with synchronous
code between them; the chat REPL borrows the same loop for its turns. Its
teardown -- cancel what is left, close async generators, wait for the default
executor, close -- is ``shut_down_loop``, shared with the chat's own loop.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)


#: The CLI runs a SEQUENCE of coroutines with plain synchronous code between
#: them: bootstrap, batch system, session handling, the agent run, then the
#: shutdowns. ``asyncio.run`` gives each of those its own loop and CLOSES it on
#: return -- which silently kills whatever a previous step left running.
#:
#: That is not theoretical. An external MCP server connects during bootstrap
#: and keeps a task alive for the session; ``ServerConnection.connected`` is
#: ``self._task is not None and not self._task.done()``. With a loop per step
#: that task is already done when the agent asks for tools, so the pool reports
#: no connected server, the catalogue comes back empty, and the agent silently
#: gets zero external tools -- while ``agent-cli mcp test`` works, because it
#: opens and uses a single loop of its own. Measured 2026-09-01:
#: "External MCP servers: 2 connected, 0 failed" followed seconds later by
#: ``connected=[]``.
#:
#: One loop for the whole process fixes it without restructuring anything: the
#: call sites keep their order and the synchronous code between them stays put.
_cli_loop: Optional[asyncio.AbstractEventLoop] = None
#: The thread the shared loop belongs to. A loop may only be driven from the
#: thread that created it, and ``asyncio.run`` gave every thread its own by
#: construction -- a property this helper would otherwise silently drop.
_cli_loop_thread: Optional[int] = None


def get_cli_loop() -> asyncio.AbstractEventLoop:
    """The CLI's shared loop, created on first use.

    For the rare caller that needs the loop OBJECT rather than to run one
    coroutine -- the chat REPL drives it directly with ``run_until_complete``
    per turn. Handing chat its own loop instead would strand the MCP
    connections from bootstrap on a loop that never runs again: ``connected``
    stays True (the task is not done, its loop is merely parked), every
    call runs into the submit timeout, and the tools fail slowly instead of
    working. Borrowers must NOT close it; ``close_cli_loop`` owns teardown.
    """
    global _cli_loop, _cli_loop_thread
    if _cli_loop is None or _cli_loop.is_closed():
        _cli_loop_thread = threading.get_ident()
        _cli_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_cli_loop)
        # Every exit path -- early return of a subcommand, exception, sys.exit
        # -- must still tear the loop down, so register once, here, instead of
        # hoping a finally block covers them all.
        atexit.register(close_cli_loop)
    return _cli_loop


def run_async(coro: Any) -> Any:
    """Run one coroutine on the CLI's single, persistent event loop.

    Drop-in for ``asyncio.run`` at this layer, with the one difference that
    matters: the loop stays open afterwards, so anything the coroutine started
    is still alive for the next call.

    Off the owning thread it falls back to ``asyncio.run``. Sharing the loop
    there would be a cross-thread use of an event loop -- the sequencing this
    exists for is a property of the CLI's single main thread, not of the
    process.
    """
    if _cli_loop is not None and _cli_loop_thread != threading.get_ident():
        return asyncio.run(coro)
    return get_cli_loop().run_until_complete(coro)


def shut_down_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Mirror ``asyncio.run``'s teardown on *loop*, without closing it.

    Background tasks spawned while it ran (the cancellation manager's timeout
    monitor, a sub-agent's job) are cancelled and awaited -- otherwise
    ``close()`` logs "Task was destroyed but it is pending" through a
    half-torn-down logging stack -- then the async generators are closed.
    Subprocess transports (the stdio tool servers, the terminal plugin's
    shells) are torn down by the executor thread pool; without waiting for it
    the interpreter can outrun those threads and their ``__del__`` lands on a
    closed loop.
    """
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.run_until_complete(loop.shutdown_default_executor())


def close_cli_loop() -> None:
    """Tear down the CLI loop: cancel leftovers, close async generators, close.

    This is what ``asyncio.run`` did after every single step. Doing it ONCE at
    process exit is the whole point -- doing it in between was the bug.
    """
    global _cli_loop, _cli_loop_thread
    loop, _cli_loop = _cli_loop, None
    _cli_loop_thread = None
    if loop is None or loop.is_closed():
        return
    try:
        shut_down_loop(loop)
    except Exception as exc:  # pragma: no cover - best effort at exit
        logger.debug("CLI loop teardown: %s", exc)
    finally:
        loop.close()
        # asyncio.run leaves the thread's loop slot EMPTY afterwards (measured:
        # get_event_loop -> RuntimeError "no current event loop"). Leaving our
        # closed loop in the slot instead would hand later get_event_loop()
        # callers a dead loop and "Event loop is closed" errors.
        asyncio.set_event_loop(None)
