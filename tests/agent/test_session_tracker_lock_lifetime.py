"""A session's lock lives while somebody holds or waits on it, and no longer.

SessionTracker kept one asyncio.Lock per session ever run -- a one-shot call's
ephemeral session included, hundreds per book -- for the life of the process.
The lock now goes with the last request that holds or waits on it. What must not
break: while anybody holds it, waits on it or owns the session, a new request
meets the SAME lock; two locks for one session would let two runs write it at
once.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_system.servers.agent.components.session_tracking import SessionTracker

SID = "s1"


def tracker_with(*request_ids: str) -> SessionTracker:
    tracker = SessionTracker({})
    for request_id in request_ids:
        tracker.register_request(request_id, SID, {"cancel": asyncio.Event(), "appended": []})
    return tracker


def keeps_nothing(tracker: SessionTracker) -> bool:
    return (SID not in tracker._session_locks and SID not in tracker._session_lock_users
            and SID not in tracker._session_lock_owners)


async def test_a_released_session_keeps_no_lock():
    tracker = tracker_with("r1")

    assert await tracker.acquire_session_lock(SID, "r1", timeout=1.0)
    await tracker.release_session_lock(SID, "r1")

    assert keeps_nothing(tracker)


async def test_a_run_that_ends_by_unregistering_keeps_no_lock():
    """The agent loop's cleanup releases through unregister_request."""
    tracker = tracker_with("r1")

    assert await tracker.acquire_session_lock(SID, "r1", timeout=1.0)
    tracker.unregister_request("r1")

    assert keeps_nothing(tracker)


async def queue_up(tracker: SessionTracker, first: str, second: str, timeout: float = 5.0):
    """Two requests for one session, the second WAITING on the lock the first holds.

    A request refuses at once while another OWNS the session; it waits only in the
    window between taking the lock and recording itself as owner. The tracker's own
    lock, held for a moment here, puts both in that window: `first` takes the session
    lock and queues for the tracker's lock behind `second`, which then waits on the
    session lock `first` holds.
    """
    await tracker._lock.acquire()
    taking = asyncio.ensure_future(tracker.acquire_session_lock(SID, first, timeout=5.0))
    waiting = asyncio.ensure_future(tracker.acquire_session_lock(SID, second, timeout=timeout))
    await asyncio.sleep(0.01)
    tracker._lock.release()
    return taking, waiting


async def test_a_lock_somebody_waits_on_stays_the_same_lock():
    tracker = tracker_with("r1", "r2", "r3")
    taking, waiting = await queue_up(tracker, "r1", "r2")
    assert await taking is True
    await asyncio.sleep(0.01)
    assert not waiting.done() and tracker._session_lock_users[SID] == 2, "fixture: r2 does not wait"
    lock = tracker._session_locks[SID]

    await tracker.release_session_lock(SID, "r1")
    assert tracker._session_locks.get(SID) is lock, "the lock went while r2 still waited on it"

    assert await waiting is True
    # r2 owns it now: a third request meets the same lock and is kept out
    assert await tracker.acquire_session_lock(SID, "r3", timeout=0.1) is False
    assert tracker._session_locks.get(SID) is lock

    await tracker.release_session_lock(SID, "r2")
    assert keeps_nothing(tracker)


@pytest.mark.parametrize("gives_up", ["timeout", "cancel"])
async def test_a_waiter_that_gives_up_leaves_nothing_behind(gives_up):
    tracker = tracker_with("r1", "r2")
    taking, waiting = await queue_up(tracker, "r1", "r2", timeout=0.5 if gives_up == "timeout" else 5.0)
    assert await taking is True
    await asyncio.sleep(0.01)
    assert not waiting.done(), "fixture: r2 does not wait"

    if gives_up == "timeout":
        assert await waiting is False
    else:
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    await tracker.release_session_lock(SID, "r1")

    assert keeps_nothing(tracker), "a waiter that gave up still counted as one"


async def test_a_request_cancelled_before_it_owns_the_lock_does_not_keep_it():
    """Between taking the lock and recording its owner the request can be cancelled
    (a window nothing opens today; held open here through the tracker's own lock).
    Taken and never owned, the lock would be released by nobody -- release goes by
    the owner -- and every later run of the session would wait out its timeout."""
    tracker = tracker_with("r1", "r2", "r3")
    await tracker._lock.acquire()
    taking = asyncio.ensure_future(tracker.acquire_session_lock(SID, "r1", timeout=5.0))
    queued = asyncio.ensure_future(tracker.acquire_session_lock(SID, "r2", timeout=5.0))
    await asyncio.sleep(0.01)
    tracker._lock.release()
    await asyncio.sleep(0)  # r1 takes the session lock and queues for the tracker's lock
    assert tracker._session_locks[SID].locked() and SID not in tracker._session_lock_owners, \
        "fixture: r1 is not between taking and owning"
    taking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await taking

    assert await queued is True, "the session stayed locked by a request that was gone"
    await tracker.release_session_lock(SID, "r2")
    assert keeps_nothing(tracker)


async def test_a_request_cancelled_after_its_lock_was_deleted_releases_nothing():
    """delete_session releases the lock a request took but did not own yet. That
    request's cancel must neither release it again (a RuntimeError in place of the
    cancel, or another request's hold) nor let go of a count that is no longer its."""
    tracker = tracker_with("r1", "r2", "r3")
    await tracker._lock.acquire()
    taking = asyncio.ensure_future(tracker.acquire_session_lock(SID, "r1", timeout=5.0))
    queued = asyncio.ensure_future(tracker.acquire_session_lock(SID, "r2", timeout=5.0))
    await asyncio.sleep(0.01)
    tracker._lock.release()
    await asyncio.sleep(0)
    assert tracker._session_locks[SID].locked() and SID not in tracker._session_lock_owners, \
        "fixture: r1 is not between taking and owning"

    tracker.delete_session(SID)
    taking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await taking

    assert await queued is True
    assert tracker._session_lock_users.get(SID) == 1, \
        "r1 let go of a count that belongs to the session's next lock"
    assert await tracker.acquire_session_lock(SID, "r3", timeout=0.1) is False, "r2's hold was released"
    await tracker.release_session_lock(SID, "r2")
    assert keeps_nothing(tracker)


async def test_a_session_deleted_while_it_runs_keeps_no_lock_afterwards():
    """delete_session takes the lock away from a running request; that request's
    release then finds no owner and forgets nothing."""
    tracker = tracker_with("r1", "r2")
    assert await tracker.acquire_session_lock(SID, "r1", timeout=1.0)

    tracker.delete_session(SID)
    await tracker.release_session_lock(SID, "r1")
    assert await tracker.acquire_session_lock(SID, "r2", timeout=1.0)
    await tracker.release_session_lock(SID, "r2")

    assert keeps_nothing(tracker)


async def test_many_one_shot_sessions_leave_no_locks():
    """What the leak looked like: one lock per session ever run."""
    tracker = SessionTracker({})
    for n in range(50):
        session_id, request_id = f"one-shot-{n}", f"req-{n}"
        tracker.register_request(request_id, session_id, {"cancel": asyncio.Event(), "appended": []})
        assert await tracker.acquire_session_lock(session_id, request_id, timeout=1.0)
        tracker.unregister_request(request_id)
        tracker.discard_session(session_id)

    assert tracker._session_locks == {} and tracker._session_lock_users == {}


class _Answers:
    """A model that answers at once."""

    model = "test/model"

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        from test_reasoning_loop_wiring import FINAL
        yield FINAL


async def test_an_agent_run_leaves_no_lock_behind():
    """Through the real loop: acquire at the start of a run, release at its end."""
    from test_reasoning_loop_wiring import _real_agent

    agent = _real_agent()
    agent.llm = _Answers()
    for session_id in ("first", "second"):
        [event async for event in agent.run_events("hello", session_id=session_id)]

    assert agent._session_tracker._session_locks == {}
    assert agent._session_tracker._session_lock_users == {}


async def test_a_cancel_landing_in_the_run_s_cleanup_still_lets_go_of_the_session():
    """_finalize_request awaits two steps before it releases the session. A cancel
    landing there (injected here: nothing produces one today) would skip the release,
    leave the session owned by a run that is gone, and refuse every later request on
    it until the process restarts."""
    from test_reasoning_loop_wiring import _real_agent

    agent = _real_agent()
    agent.llm = _Answers()
    take_in_late_messages = agent._take_in_late_messages

    async def cancelled_here(*args, **kwargs):
        raise asyncio.CancelledError

    agent._take_in_late_messages = cancelled_here
    with pytest.raises(asyncio.CancelledError):
        [event async for event in agent.run_events("hello", session_id="stuck")]
    agent._take_in_late_messages = take_in_late_messages

    assert agent._session_tracker.check_session_locked("stuck") == (False, None)
    events = [event async for event in agent.run_events("again", session_id="stuck")]
    assert any(event.get("type") == "final" for event in events), \
        "the session was refused: still owned by the run that was gone"


async def test_a_run_whose_status_scopes_do_not_open_lets_go_of_the_session(monkeypatch):
    """Entering the run's status scopes publishes, after its start event and before the try whose finally ends
    it. A cancel or an error there left the request registered and its session locked for good: every later
    request on it was refused until the process restarted."""
    from test_reasoning_loop_wiring import _real_agent

    from agent_system.servers.agent.mixins import run as run_mod

    real_scope = run_mod.status_scope
    endings = []

    class _Told:
        """The coordinator's scope, noting how it is told to end."""

        def __init__(self, scope):
            self.scope = scope

        async def __aenter__(self):
            return await self.scope.__aenter__()

        async def __aexit__(self, *exc):
            endings.append(exc[0])
            return await self.scope.__aexit__(*exc)

    def failing_worker_scope(bus, name, request_id):
        if name.endswith("_worker"):
            raise RuntimeError("the status bus is gone")
        return _Told(real_scope(bus, name, request_id))

    agent = _real_agent()
    agent.llm = _Answers()
    monkeypatch.setattr(run_mod, "status_scope", failing_worker_scope)
    with pytest.raises(RuntimeError):
        [event async for event in agent.run_events("hello", session_id="stuck")]
    monkeypatch.setattr(run_mod, "status_scope", real_scope)

    assert agent._session_tracker.check_session_locked("stuck") == (False, None)
    assert agent._session_tracker._active_requests == {}
    assert endings == [RuntimeError], "the coordinator's scope ended as completed"
    events = [event async for event in agent.run_events("again", session_id="stuck")]
    assert any(event.get("type") == "final" for event in events), "the session was refused"
