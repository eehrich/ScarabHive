"""A continue takes its sub-agent's session lock before it touches the session; a save after a run leaves a
session somebody holds.

A run of this process could have the sub-agent's session -- a chat on it in the web UI, a run still finishing.
The continue replaced its metadata and template vars (_prepare_agent) and rewrote its record (refresh, reopen),
then its own run was refused at the lock -- and the save after that run wrote the other run's live state to
disk. Now the continue takes the lock under the request id its run takes it by, first; and every save after a
sub-agent's run passes after_run (SessionService.save_session).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

from test_plugin_sub_agent_manager_status import _create, _wire, server, system_config  # noqa: F401

INSTANCE = "sub_session_1"
CONTINUE = {"operation": "continue", "instance_id": INSTANCE, "message": "go on", "_session_id": "parent_session"}


def _continuable(server):
    """The instance exists and is the caller's; its refresh records whether the session's lock was taken."""
    manager = server._get_manager()
    agent = server._extract_registry().get("basic_agent")
    held_at_refresh = []

    async def refresh(**kwargs):
        held_at_refresh.append(agent._session_tracker.check_session_locked(INSTANCE))
        return {"aufgabe": "World"}

    manager.refresh_sub_context_vars = refresh
    manager._session_service.session_manager.load_session = AsyncMock(return_value={
        "agent_name": "basic_agent", "parent_session": {"session_id": "parent_session"}})
    return manager, agent, held_at_refresh


async def test_a_continue_leaves_a_session_another_run_of_this_process_has(server):
    _wire(server, [{"type": "start"}, {"type": "final", "summary": "done"}, {"type": "end"}])
    manager, agent, held_at_refresh = _continuable(server)
    manager.reopen_sub_session = AsyncMock()
    tracker = agent._session_tracker
    assert await tracker.acquire_session_lock(INSTANCE, "web_run")  # a chat on the sub-agent's session
    try:
        result = await server.call_with_status("test_manager_manage_sub_agent", dict(CONTINUE))
    finally:
        await tracker.release_session_lock(INSTANCE, "web_run")

    assert result["status"] == "error" and "another request" in result["error"], result
    assert held_at_refresh == [], "its vars were refreshed under the run that has it"
    manager.reopen_sub_session.assert_not_awaited()
    tracker.set_session_metadata.assert_not_called()
    manager._session_service.save_session.assert_not_awaited()


async def test_the_continue_holds_the_session_before_it_touches_it_and_its_run_takes_it_again(server):
    _wire(server, [])
    _manager, agent, held_at_refresh = _continuable(server)
    tracker = agent._session_tracker
    held_by_the_run = []

    async def run_events(*args, request_id=None, session_id=None, **kwargs):
        # as Agent.run_events: the session's lock under the run's request id -- a re-entry here
        assert await tracker.acquire_session_lock(session_id, request_id, timeout=0.1), "the run was refused"
        held_by_the_run.append(request_id)
        try:
            yield {"type": "final", "summary": "done"}
            yield {"type": "end"}
        finally:
            await tracker.release_session_lock(session_id, request_id)

    agent.run_events = run_events
    result = await server.call_with_status("test_manager_manage_sub_agent", dict(CONTINUE))

    assert result["status"] == "completed", result
    assert held_at_refresh == [(True, held_by_the_run[0])], "the session was touched before its lock was taken"
    assert tracker.check_session_locked(INSTANCE) == (False, None), "the lock stayed behind"


async def test_a_continue_whose_run_never_starts_lets_go_of_the_session(server):
    _wire(server, [{"type": "start"}, {"type": "final", "summary": "done"}, {"type": "end"}])
    _manager, agent, _held = _continuable(server)
    agent._session_tracker.set_session_metadata = Mock(side_effect=RuntimeError("prepare failed"))

    result = await server.call_with_status("test_manager_manage_sub_agent", dict(CONTINUE))

    assert result["status"] == "error", result
    assert agent._session_tracker.check_session_locked(INSTANCE) == (False, None), "the lock stayed behind"


async def _async_create(server):
    result = await server.call_with_status("test_manager_manage_sub_agent", {
        "operation": "create", "agent_type": "basic_agent", "task": "do the thing", "blocking": False,
        "_session_id": "parent_session"})
    assert result["status"] == "running", result
    for _ in range(300):
        async with server._async_jobs_lock:
            if (server._async_jobs.get(INSTANCE) or {}).get("status") in ("completed", "failed", "cancelled"):
                return
        await asyncio.sleep(0.01)
    raise AssertionError("the background run did not end")


async def test_every_save_after_a_sub_agents_run_leaves_a_session_somebody_holds(server):
    _wire(server, [{"type": "start"}, {"type": "final", "summary": "done"}, {"type": "end"}])
    _manager, _agent, _held = _continuable(server)
    save = server._extract_session_service().save_session

    await _create(server)
    await server.call_with_status("test_manager_manage_sub_agent", dict(CONTINUE))
    await _async_create(server)

    assert [call.kwargs.get("after_run") for call in save.await_args_list] == [True, True, True]


async def test_a_session_taken_right_after_the_run_is_left_to_its_taker_without_a_word(server, caplog):
    """A chat on the sub-agent's session may take it the moment the run lets go. The continue lets go of its own
    hold only: releasing into the other request's lock logged a warning about a lock that was never its."""
    import logging

    _wire(server, [])
    _manager, agent, _held = _continuable(server)
    tracker = agent._session_tracker

    async def run_events(*args, request_id=None, session_id=None, **kwargs):
        assert await tracker.acquire_session_lock(session_id, request_id, timeout=0.1)
        try:
            yield {"type": "final", "summary": "done"}
            yield {"type": "end"}
        finally:
            await tracker.release_session_lock(session_id, request_id)
            assert await tracker.acquire_session_lock(session_id, "web_run")  # taken the moment it is free

    agent.run_events = run_events
    try:
        with caplog.at_level(logging.WARNING):
            result = await server.call_with_status("test_manager_manage_sub_agent", dict(CONTINUE))
        assert result["status"] == "completed", result
        assert tracker.check_session_locked(INSTANCE) == (True, "web_run")
    finally:
        await tracker.release_session_lock(INSTANCE, "web_run")
    assert "tried to release lock" not in caplog.text, caplog.text


async def test_a_continue_cancelled_by_its_scope_closes_its_run_before_it_is_done(server):
    """A cancel scope (a stream whose client left, above the parent's run) hands its cancel out again at every
    await: _consume_run's close of the run was skipped with the activity write before it, and the run lay at a
    yield -- its save not made, its session's lock held -- until the garbage collector came."""
    import anyio

    _wire(server, [])
    manager, agent, _held = _continuable(server)
    tracker = agent._session_tracker
    finalized = []

    async def slow_activity(*args, **kwargs):
        await asyncio.sleep(0.05)  # the activity is written to the record

    manager.update_sub_agent_activity = slow_activity

    async def run_events(*args, request_id=None, session_id=None, **kwargs):
        # as Agent.run_events: its request registered, then the session's lock under its id
        tracker.register_request(request_id, session_id, {"cancel": asyncio.Event(), "appended": []})
        assert await tracker.acquire_session_lock(session_id, request_id, timeout=0.1)
        try:
            for _ in range(100):
                yield {"type": "thinking_delta", "delta": "x"}
                await asyncio.sleep(0.01)
            yield {"type": "final", "summary": "done"}
            yield {"type": "end"}
        finally:  # its _finalize_request: the save, then the lock let go
            await asyncio.sleep(0.01)
            finalized.append(request_id)
            await tracker.release_session_lock(session_id, request_id)

    agent.run_events = run_events
    async with anyio.create_task_group() as group:
        async def continue_it():
            await server.call_with_status("test_manager_manage_sub_agent", dict(CONTINUE))

        group.start_soon(continue_it)
        await asyncio.sleep(0.03)  # inside the activity write for the run's first event
        group.cancel_scope.cancel()

    assert finalized, "the run was left at a yield, not closed"
    assert tracker.check_session_locked(INSTANCE) == (False, None)


async def test_the_lock_of_a_run_that_started_is_left_to_that_run():
    """_take_the_session holds the lock under the id the run takes it by; after the run started, the lock is the
    run's, and let go of by its end -- not by the continue, while the run may still be closing."""
    from agent_system.servers.agent.components.session_tracking import SessionTracker
    from plugins.sub_agent_manager.server import SubAgentManagerServer

    agent = Mock()
    agent._session_tracker = tracker = SessionTracker()
    assert await tracker.acquire_session_lock(INSTANCE, "sub_cont_1")

    tracker.register_request("sub_cont_1", INSTANCE, {"cancel": asyncio.Event(), "appended": []})  # it started
    await SubAgentManagerServer._let_go_of_the_session(agent, INSTANCE, "sub_cont_1")
    assert tracker.check_session_locked(INSTANCE) == (True, "sub_cont_1"), "let go under a run that started"

    tracker.unregister_request("sub_cont_1")  # the run's end
    assert tracker.check_session_locked(INSTANCE) == (False, None)
    assert await tracker.acquire_session_lock(INSTANCE, "sub_cont_2")  # one that never started
    await SubAgentManagerServer._let_go_of_the_session(agent, INSTANCE, "sub_cont_2")
    assert tracker.check_session_locked(INSTANCE) == (False, None), "a run that never started kept it"
