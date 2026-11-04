"""GET /api/agent-runs/stream subscribes to the bus when its body is read, not when the handler returns.

A response whose body is never started -- the client left first -- never runs
the generator's finally, so a queue subscribed in the handler stayed on the bus,
filling, for the life of the process.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from agent_system.api import run_event_routes
from agent_system.run_events import RunEventBus, RunEventStore, use_run_events

pytestmark = pytest.mark.anyio


@pytest.fixture
def bus(tmp_path):
    bus = RunEventBus()
    use_run_events(RunEventStore(tmp_path / "run_events.db"), bus)
    yield bus
    use_run_events(None, RunEventBus())


def _request():
    return Request({"type": "http", "method": "GET", "path": "/api/agent-runs/stream", "query_string": b"",
                    "headers": [], "app": FastAPI()})


async def test_a_stream_nobody_reads_holds_no_subscription(bus):
    response = await run_event_routes.agent_runs_stream(_request(), after="")

    assert bus.subscribers == 0, "subscribed before the body was read"
    await response.body_iterator.aclose()
    assert bus.subscribers == 0


async def test_a_stream_being_read_is_subscribed_until_it_is_closed(bus):
    response = await run_event_routes.agent_runs_stream(_request(), after="")
    lines = response.body_iterator

    first = await asyncio.wait_for(lines.__anext__(), timeout=2)

    assert first.startswith("retry:")
    assert bus.subscribers == 1
    await lines.aclose()
    assert bus.subscribers == 0
