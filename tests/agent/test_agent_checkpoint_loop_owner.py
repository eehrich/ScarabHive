"""A run's checkpoint loop is the run's own: started with the session held,
stopped when the run ends -- and no other run's loop is stopped on its behalf.

The loop used to start before the session lock and be stopped by session id
from the run's outer cleanup:

* a request refused at the lock (the session busy with another run) stopped
  the loop of the run that held it;
* a run releases the session before its final save, and its cleanup comes
  later still -- the next run on the session had its loop stopped by the
  previous run's late cleanup;
* a request refused in the moment between two runs registered a loop of its
  own, and the next run went on without one once that request cleaned up;
* an agent called as a tool on its caller's session (another Agent, the same
  SessionService) stopped the caller's loop at its own end.

Each time a run went on without checkpoints, for the rest of a run that may
last hours.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService
from test_reasoning_loop_wiring import FINAL, _real_agent


class _Answers:
    model = "test/model"

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        yield FINAL


def _agent_with_service(tmp_path):
    (tmp_path / "sessions").mkdir(exist_ok=True)
    service = SessionService(session_manager=SessionManager(storage_path=str(tmp_path / "sessions")))
    service.checkpoint_interval_seconds = 60  # never fires during a test
    agent = _real_agent()
    agent.llm = _Answers()
    agent._session_service = service
    return agent, service


async def test_a_refused_request_neither_starts_nor_stops_a_loop():
    agent = _real_agent()
    agent.llm = _Answers()
    service = AsyncMock()
    service.start_checkpoint_loop = Mock()
    agent._session_service = service
    # Another run holds the session: this request is refused at the session lock.
    agent._session_tracker.register_request("holder", "busy", {"appended": []})
    assert await agent._session_tracker.acquire_session_lock("busy", "holder", timeout=1.0)

    events = [event async for event in agent.run_events("hello", session_id="busy")]

    assert any(event.get("type") == "error" for event in events), f"fixture: not refused: {events}"
    assert service.start_checkpoint_loop.call_args_list == [], "a refused request started a loop"
    assert service.stop_checkpoint_loop.await_args_list == [], "a refused request stopped the holder's loop"


async def test_a_run_has_its_loop_while_it_runs_and_none_after(tmp_path):
    agent, service = _agent_with_service(tmp_path)
    seen = []

    async for event in agent.run_events("hello", session_id="S"):
        if event.get("type") == "final":
            loop = service._checkpoint_tasks.get("S")
            seen.append(loop is not None and not loop.done())

    assert seen == [True], "the run had no checkpoint loop"
    assert service._checkpoint_tasks == {}, "the run's loop outlived it"


async def test_a_run_whose_cleanup_comes_late_leaves_the_next_run_s_loop_alone(tmp_path):
    """The run releases the session before its save; its generator is closed later
    still -- the app leaves that to the garbage collector. A next run on the session
    starts in between, with a loop of its own."""
    agent, service = _agent_with_service(tmp_path)

    first = agent.run_events("hello", session_id="S")
    async for event in first:
        if event.get("type") == "end":
            break  # read to its end, not closed
    second = agent.run_events("again", session_id="S")
    async for event in second:
        if event.get("type") == "start":
            break
    await second.__anext__()  # past the start: its loop is running
    loop = service._checkpoint_tasks.get("S")
    assert loop is not None and not loop.done(), "fixture: the second run started no loop"

    await first.aclose()  # the first run's cleanup, late
    for _ in range(5):  # the inner run is closed by asyncio's finalizer, a turn or two later
        await asyncio.sleep(0)

    assert not loop.done(), "the first run's cleanup stopped the second run's checkpoint loop"
    async for _ in second:
        pass


async def test_a_run_closed_at_its_start_event_lets_go_of_the_session(tmp_path):
    """Nothing after the start event runs when the consumer closes there (a client
    gone at once), the finally that ends a run included: the session stayed held by
    a run that never ran, and every later request on it was refused until restart."""
    agent, service = _agent_with_service(tmp_path)

    run = agent.run_events("hello", session_id="S")
    async for event in run:
        if event.get("type") == "start":
            break
    await run.aclose()
    # The inner run is closed by asyncio's finalizer, in a task of its own on the loop.
    for _ in range(50):
        if agent._session_tracker.check_session_locked("S") == (False, None):
            break
        await asyncio.sleep(0.01)

    assert agent._session_tracker.check_session_locked("S") == (False, None)
    assert agent._session_tracker._session_locks == {}
    events = [event async for event in agent.run_events("again", session_id="S")]
    assert any(event.get("type") == "final" for event in events), "the session was refused"


async def test_an_agent_called_on_its_caller_s_session_leaves_the_caller_s_loop_alone(tmp_path):
    """Another Agent (its own tracker, so not refused) on the same session and the same
    SessionService: its run found the caller's loop running, started none -- and stopped
    the caller's at its end, by the session id."""
    caller, service = _agent_with_service(tmp_path)
    called = _real_agent()
    called.llm = _Answers()
    called._session_service = service

    run = caller.run_events("hello", session_id="S")
    async for event in run:
        if event.get("type") == "start":
            break
    await run.__anext__()  # past the start: the caller's loop is running
    loop = service._checkpoint_tasks.get("S")
    assert loop is not None and not loop.done(), "fixture: the caller has no loop"

    [event async for event in called.run_events("a tool call", session_id="S")]

    assert not loop.done(), "the called agent stopped its caller's checkpoint loop"
    async for _ in run:
        pass
    assert loop.done() and service._checkpoint_tasks == {}, "the caller's loop outlived its run"


async def test_start_hands_back_the_loop_it_started(tmp_path):
    _, service = _agent_with_service(tmp_path)
    loop = service.start_checkpoint_loop(Mock(), "u1", "s1")
    try:
        assert isinstance(loop, asyncio.Task)
        assert service.start_checkpoint_loop(Mock(), "u1", "s1") is None, "a running loop was claimed again"
    finally:
        await service.stop_checkpoint_loop("s1", started=loop)
    assert loop.done() and "s1" not in service._checkpoint_tasks
    service.checkpoint_interval_seconds = 0
    assert service.start_checkpoint_loop(Mock(), "u1", "s2") is None


async def test_stopping_a_loop_that_is_gone_leaves_the_session_s_new_one(tmp_path):
    _, service = _agent_with_service(tmp_path)
    old = service.start_checkpoint_loop(Mock(), "u1", "s1")
    await service.stop_checkpoint_loop("s1")  # by key
    new = service.start_checkpoint_loop(Mock(), "u1", "s1")
    try:
        await service.stop_checkpoint_loop("s1", started=old)

        assert not new.done() and service._checkpoint_tasks.get("s1") is new
    finally:
        await service.stop_checkpoint_loop("s1", started=new)


async def test_a_cancel_landing_in_the_run_s_cleanup_leaves_no_loop_running(tmp_path):
    """Injected here: nothing cancels those awaits today. Stopped after them, the loop
    would have outlived its run for good -- the next run found it and started none, and
    no final save was protected from it any more."""
    agent, service = _agent_with_service(tmp_path)

    async def cancelled_here(*args, **kwargs):
        raise asyncio.CancelledError

    agent._take_in_late_messages = cancelled_here
    with pytest.raises(asyncio.CancelledError):
        [event async for event in agent.run_events("hello", session_id="S")]

    assert service._checkpoint_tasks == {}, "the run's checkpoint loop outlived it"
