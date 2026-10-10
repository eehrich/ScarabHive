"""A session lock held by a writer is waited for; one held by a run is not.

An append or /undo's cut holds the agent's session lock from before it reads the session until its save is done
(api/session_writes.beside_the_runs, acquire_session_lock(writer=True)). Refused at once like a run, it turned away what asked
for the lock meanwhile: a run starting, an API turn opening -- and the put back of a failed turn, which then left
the turn in the conversation. A writer that meets a run is still refused: a run is not over in a moment.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from agent_system.servers.agent.components.session_tracking import SessionTracker

SID = "s1"


def keeps_nothing(tracker: SessionTracker) -> bool:
    return (SID not in tracker._session_locks and SID not in tracker._session_lock_users
            and SID not in tracker._session_lock_owners and not tracker._session_lock_writers)


@pytest.mark.parametrize("asker", ["run", "writer"])
async def test_what_meets_a_writer_waits_for_it(asker):
    tracker = SessionTracker({})
    assert await tracker.acquire_session_lock(SID, "write_1", writer=True)
    assert tracker.held_by_a_writer(SID)

    asking = asyncio.ensure_future(tracker.acquire_session_lock(SID, "other", timeout=2.0, writer=asker == "writer"))
    await asyncio.sleep(0.05)
    assert not asking.done(), "refused instead of waiting for the writer"
    await tracker.release_session_lock(SID, "write_1")

    assert await asking is True
    assert tracker.check_session_locked(SID) == (True, "other")
    assert tracker.held_by_a_writer(SID) is (asker == "writer")
    await tracker.release_session_lock(SID, "other")
    assert keeps_nothing(tracker)


async def test_a_writer_that_meets_a_run_is_refused_at_once():
    tracker = SessionTracker({})
    assert await tracker.acquire_session_lock(SID, "run_1")

    started = time.monotonic()
    assert await tracker.acquire_session_lock(SID, "write_1", timeout=5.0, writer=True) is False

    assert time.monotonic() - started < 1.0, "the writer waited for a run"
    assert not tracker.held_by_a_writer(SID)
    await tracker.release_session_lock(SID, "run_1")
    assert keeps_nothing(tracker)


async def test_one_that_gives_up_on_a_writer_leaves_nothing_behind():
    tracker = SessionTracker({})
    assert await tracker.acquire_session_lock(SID, "write_1", writer=True)

    assert await tracker.acquire_session_lock(SID, "run_1", timeout=0.05) is False
    await tracker.release_session_lock(SID, "write_1")

    assert keeps_nothing(tracker)


@pytest.mark.parametrize("ends", ["delete", "clear", "unregister"])
async def test_the_writer_mark_goes_with_the_lock(ends):
    tracker = SessionTracker({})
    tracker.register_request("write_1", SID, {"cancel": asyncio.Event(), "appended": []})
    assert await tracker.acquire_session_lock(SID, "write_1", writer=True)

    if ends == "delete":
        tracker.delete_session(SID)
    elif ends == "clear":
        tracker.clear()
    else:
        tracker.unregister_request("write_1")

    assert not tracker.held_by_a_writer(SID)
    assert await tracker.acquire_session_lock(SID, "run_1", timeout=0.1)
    assert await tracker.acquire_session_lock(SID, "run_2", timeout=0.1) is False, "a run's lock read as a writer's"


async def test_what_waited_for_a_writer_is_refused_at_once_by_a_run_that_took_over():
    """Queued in the lock behind the writer, the second asker waited behind the run that came first too -- its
    whole timeout, where meeting a run means an answer at once."""
    tracker = SessionTracker({})
    assert await tracker.acquire_session_lock(SID, "write_1", writer=True)
    first = asyncio.ensure_future(tracker.acquire_session_lock(SID, "run_1", timeout=5.0))
    await asyncio.sleep(0.01)
    second = asyncio.ensure_future(tracker.acquire_session_lock(SID, "run_2", timeout=5.0))
    await asyncio.sleep(0.05)

    await tracker.release_session_lock(SID, "write_1")
    started = time.monotonic()
    assert await first is True
    assert await second is False
    assert time.monotonic() - started < 1.0, "waited behind the run instead of being refused"

    await tracker.release_session_lock(SID, "run_1")
    assert keeps_nothing(tracker) and tracker._session_lock_written == {}


async def test_a_free_lock_is_taken_whatever_time_is_left():
    """No time left -- a timeout of 0, or the last of it spent waiting for a writer -- still takes a lock nobody
    has: through wait_for, a free lock was refused."""
    tracker = SessionTracker({})

    assert await tracker.acquire_session_lock(SID, "run_1", timeout=0) is True

    await tracker.release_session_lock(SID, "run_1")
    assert keeps_nothing(tracker)
