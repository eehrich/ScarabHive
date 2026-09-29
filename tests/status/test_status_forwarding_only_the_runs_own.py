"""A run's stream carries the status lines of that run and its sub-runs -- nobody else's.

A status event that names no request used to pass the filter and reach the stream of
every run in the process, whoever that run belonged to.
"""
from __future__ import annotations

import pytest

from agent_system.servers.agent.components.status_forwarding import DirectStatusHandler
from agent_system.tools.status import StatusEvent


@pytest.mark.asyncio
async def test_a_runs_stream_takes_its_own_lines_and_no_one_elses():
    forwarded: list = []
    handler = DirectStatusHandler("run1", forwarded)
    for request_id in ("run1", "run1_003", "run1_sub_001", None, "", "run2", "run10"):
        await handler.process(StatusEvent(server="s", request_id=request_id, message=str(request_id)))
    assert [event["request_id"] for event in forwarded] == ["run1", "run1_003", "run1_sub_001"]
