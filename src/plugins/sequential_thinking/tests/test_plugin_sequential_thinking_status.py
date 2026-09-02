"""The memory warning above 80 % history usage must not fail the call.

``safe_status_call`` forwards ``**kwargs`` to the StatusScope method, and the
warning branch passed ``level="warning"`` -- a keyword ``StatusScope.progress``
does not take. From thought 81 of a session on, every call to the main tool
therefore raised TypeError and was reported as a failure, although the thought
had already been persisted: the caller saw an error for work that was done.

The test drives the real dispatch (``call_with_status`` -> StatusScope), which
is what supplies the ``_status`` object the warning branch uses.
"""
from __future__ import annotations

import pytest

from agent_system.config.models import AgentSystemConfig, MCPConfig
from agent_system.mcp.status import StatusPhase, get_status_bus
from plugins.sequential_thinking.server import SequentialThinkingServer

# Small enough that the 80 % mark is reached in a handful of calls, and the
# BINDING limit for this test -- with the shipped 100 it would take 81 calls.
HISTORY_SIZE = 5


@pytest.fixture
def server():
    config = MCPConfig(type="sequential_thinking", enabled=True)
    config.max_history_size = HISTORY_SIZE
    config.session_ttl_seconds = 3600
    config.enable_branching = True
    config.enable_revisions = True
    config.max_summary_thoughts = 10
    return SequentialThinkingServer("sequential_thinking", AgentSystemConfig(), config)


async def _think(server, session_id, number, total):
    """One real tool call; returns (result, published events)."""
    bus = get_status_bus()
    queue = await bus.subscribe(server="sequential_thinking.execute()")
    try:
        result = await server.call_with_status("sequential_thinking_execute", {
            "thought": f"step {number}",
            "thought_number": number,
            "total_thoughts": total,
            "next_thought_needed": number < total,
            "session_id": session_id,
        })
    finally:
        bus.unsubscribe(queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return result, events


async def test_thoughts_past_the_memory_warning_still_succeed(server):
    session_id = "s-memory"
    results = []
    for number in range(1, HISTORY_SIZE + 1):
        result, events = await _think(server, session_id, number, HISTORY_SIZE)
        results.append((result, events))

    session = server._sessions[session_id]
    # Fixture assertion: without crossing 80 % the warning branch never runs
    # and this test would pass on any code at all.
    assert len(session.thoughts) / server.max_history_size > 0.8, \
        f"only {len(session.thoughts)} thoughts -- the warning branch never ran"

    for number, (result, events) in enumerate(results, start=1):
        assert result.get("status") == "success", (number, result)
        errors = [e for e in events if e.phase is StatusPhase.ERROR]
        assert not errors, (number, [e.message for e in errors])
