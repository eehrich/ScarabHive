"""A long-running API process keeps no background job's ending for good.

The ending of a job stays in memory until somebody takes it -- a poll, a wait, a
woken caller, a continue or delete of the instance. A caller that polls much
later, or a throwaway turn that never comes back, took nothing, and the entry
held its whole result for the life of the process. Now an ending nobody took
goes after FINISHED_JOB_RETENTION_SECONDS, once it is stored: a poll then
answers from the stored state, as after a restart.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from agent_system.config import AgentSystemConfig, ToolServerConfig
from plugins.sub_agent_manager import server as sam_server
from plugins.sub_agent_manager.server import SubAgentManagerServer

PARENT = "parent1"


@pytest.fixture
def server(monkeypatch):
    config = Mock(spec=AgentSystemConfig)
    config.session_file_path = "data/sessions"
    config.session_service = Mock()
    config.registry = Mock()
    server_config = Mock(spec=ToolServerConfig)
    server_config.max_sub_agents_per_session = 10
    server_config.max_nesting_depth = 5
    server_config.max_sub_agents_per_type = 3
    server_config.allowed_agents = ["*"]
    server_config.blocked_agents = []
    server = SubAgentManagerServer(name="sub_agent_manager", system_config=config,
                                   server_config=server_config)
    manager = Mock()
    manager._extract_user_id = Mock(return_value="u1")
    manager.update_sub_session_metadata = AsyncMock(return_value=None)  # stored
    manager.list_sub_sessions = AsyncMock(
        return_value=[{"instance_id": "sub_old", "agent_type": "worker", "status": "active"}])
    manager._session_service.session_manager.load_session = AsyncMock(
        return_value={"messages": [{"role": "assistant", "content": "the stored answer",
                                    "tool_calls": None}]})
    server._extract_registry = Mock(return_value=Mock())
    server._extract_session_service = Mock(return_value=Mock())
    server._get_manager = Mock(return_value=manager)
    monkeypatch.setattr(sam_server, "presence_for", lambda config: None)
    return server


def running(server, instance_id):
    server._async_jobs[instance_id] = {
        "instance_id": instance_id, "status": "running", "result": None,
        "parent_session_id": PARENT, "task_handle": None}


async def end(server, instance_id, **params):
    """A job's ending, the way every one of them is recorded; returns its bell."""
    return await server._finish_job(instance_id, {"_session_id": PARENT, **params}, "completed",
                                    stored={"status": "active"}, manager=server._get_manager(),
                                    result="x" * 1000)


def age(server, instance_id):
    """As if the retention had passed since the job ended."""
    server._async_jobs[instance_id]["_ended_at"] -= sam_server.FINISHED_JOB_RETENTION_SECONDS + 1


async def test_an_ending_nobody_takes_goes_after_the_retention(server):
    running(server, "sub_old")
    await end(server, "sub_old")
    age(server, "sub_old")

    running(server, "sub_next")
    await end(server, "sub_next")  # any job that ends looks

    assert "sub_old" not in server._async_jobs, "an ending nobody took stayed for good"
    assert "sub_next" in server._async_jobs, "an ending within the retention went"
    # and a caller that comes late still gets it -- from the stored state
    polled = await server._handle_poll({"instance_id": "sub_old", "_session_id": PARENT})
    assert polled["status"] == "completed"
    assert polled["result"] == "the stored answer"


async def test_an_ending_that_could_not_be_stored_stays_a_day(server):
    """Unstored, the entry is the only answer there is -- kept long, but not for good: a parent
    deleted while its job ran is why a write fails, and then nobody can read the entry at all."""
    server._get_manager().update_sub_session_metadata = AsyncMock(return_value=False)
    running(server, "sub_old")
    await end(server, "sub_old")
    age(server, "sub_old")

    running(server, "sub_next")
    await end(server, "sub_next")
    assert "sub_old" in server._async_jobs, "an unstored ending went with the stored ones"

    server._async_jobs["sub_old"]["_ended_at"] -= sam_server.UNSTORED_JOB_RETENTION_SECONDS
    running(server, "sub_last")
    await end(server, "sub_last")
    assert "sub_old" not in server._async_jobs, "an unstored ending stayed for good"


async def test_an_ending_recorded_elsewhere_ages_from_when_it_is_first_seen(server):
    """A cancel marks the entry itself; a task cancelled before it started never reaches
    `_finish_job`, so the entry has no time of its own and stayed."""
    server._async_jobs["sub_cancelled"] = {
        "instance_id": "sub_cancelled", "status": "cancelled", "parent_session_id": PARENT,
        "task_handle": None, "_ended_by_caller": True}

    running(server, "sub_next")
    await end(server, "sub_next")  # first seen: stamped, not dropped
    assert "sub_cancelled" in server._async_jobs
    server._async_jobs["sub_cancelled"]["_ended_at"] -= sam_server.UNSTORED_JOB_RETENTION_SECONDS + 1

    running(server, "sub_last")
    await end(server, "sub_last")
    assert "sub_cancelled" not in server._async_jobs


async def test_an_ending_whose_bell_still_rings_stays(server):
    """The bell stops once the entry is gone (`_ending_is_unread`): dropped while it rang, the
    caller would sleep over its job."""
    seen = []

    async def ringing(*args, **kwargs):
        running(server, "sub_other")
        await end(server, "sub_other")  # a job that ends while the bell rings
        seen.append("sub_rung" in server._async_jobs)

    server._wake_parent = ringing
    running(server, "sub_rung")
    bell = await end(server, "sub_rung", wake_when_done=True)
    assert bell is not None, "fixture: this ending rings nobody"
    age(server, "sub_rung")

    await bell()

    assert seen == [True], "the ending went while its bell rang"
    running(server, "sub_later")
    await end(server, "sub_later")
    assert "sub_rung" not in server._async_jobs, "once rung, it goes like any other"


async def test_poll_and_wait_hand_out_no_bookkeeping(server):
    """What the retention keeps on the entry (and every other mark of ours) is no answer."""
    running(server, "sub_polled")
    await end(server, "sub_polled")
    running(server, "sub_waited")
    await end(server, "sub_waited")

    polled = await server._handle_poll({"instance_id": "sub_polled", "_session_id": PARENT})
    waited = await server._handle_wait({"instance_id": "sub_waited", "_session_id": PARENT})

    for answer in (polled, waited):
        assert answer["status"] == "completed" and answer["result"] == "x" * 1000
        assert not [key for key in answer if key.startswith("_") or key == "task_handle"], answer
