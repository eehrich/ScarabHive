"""Work on the REPL's loop that a Ctrl-C stops -- the work, not the chat.

Between turns everything the chat does runs on its one event loop: a
listing, a resume, a save, a rewind. ``run_until_complete`` leaves a task
PENDING when a KeyboardInterrupt lands in it, and a task left pending there
runs on inside the next ``run_until_complete`` -- after the person was told
it had stopped. So such work is run as a task and, on Ctrl-C, cancelled and
waited out (_run_interruptible, _drain), or, where stopping halfway would be
a lie about the disk, waited for (_run_to_the_end).

Its own module because the commands, the save and the turn's cancel path all
lean on it, and none of them is where it belongs more than the others.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


def _through_before_the_interrupt(task: "asyncio.Task") -> bool:
    """Whether *task* finished with a result before the Ctrl-C landed.

    asyncio does not stop the loop for a task that finished with a
    KeyboardInterrupt, so waiting again HANGS -- the caller has to read the
    task instead. And the work is done: calling it cancelled throws away a
    result that already changed the world.
    """
    return task.done() and not task.cancelled() and task.exception() is None


def _drain(loop: asyncio.AbstractEventLoop, task: "asyncio.Task", what: str) -> None:
    """Cancel *task* and wait it out -- it must not finish LATER.

    Three attempts, because every further Ctrl-C interrupts the wait: a task
    left pending on the shared loop runs on inside the next
    ``run_until_complete``. That is the bug this whole path exists for -- a
    compaction rewriting the message list of the turn after it, a save
    writing while a woken process has taken the session up.
    """
    task.cancel()
    for _ in range(3):
        if task.done():
            break
        try:
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        except KeyboardInterrupt:
            logger.debug("another Ctrl-C while %s unwound", what)
    else:
        logger.warning("%s did not unwind and is still pending", what)
        return
    if not task.cancelled() and task.exception() is not None:
        # Read once: an exception nobody retrieves is reported by asyncio at
        # garbage collection, long after the command it belonged to.
        logger.debug("%s ended with %r", what, task.exception())


def _run_interruptible(loop: asyncio.AbstractEventLoop, coro: Any,
                       what: str) -> tuple[bool, Any]:
    """Run *coro* on the REPL's loop; Ctrl-C cancels it, not the chat.

    (finished, result). Driven as a TASK, not as a bare coroutine:
    ``run_until_complete`` leaves the future PENDING on KeyboardInterrupt, so
    the work would quietly finish inside the NEXT turn -- a compaction
    resuming there rewrites the very message list that turn is reading, after
    the person was told it had been interrupted. And the interrupt itself
    used to end the whole chat with a traceback.
    """
    task = loop.create_task(coro)
    try:
        return True, loop.run_until_complete(task)
    except KeyboardInterrupt:
        if _through_before_the_interrupt(task):
            # A /resume that got through has already switched the session.
            # Reporting it as cancelled made the caller let go of the hold on
            # the session the chat was now writing to -- and keep the one on
            # the session it had left.
            return True, task.result()
        _drain(loop, task, what)
        print(f"\n({what} cancelled)", file=sys.stderr)
        return False, None


def _run_to_the_end(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    """Run *coro*; a Ctrl-C waits for it instead of leaving it half done.

    For a rewind: it puts files back in a worker thread that a cancel does not
    stop, so reporting it "cancelled" would be a lie about the disk. A second
    Ctrl-C leaves it to finish unseen.
    """
    task = loop.create_task(coro)
    try:
        return loop.run_until_complete(task)
    except KeyboardInterrupt:
        print("\n(finishing -- files are being put back; Ctrl-C again to stop waiting)", file=sys.stderr)
        return loop.run_until_complete(task)
