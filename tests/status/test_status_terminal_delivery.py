import asyncio
import os
import pytest

from agent_system.tools import status


async def _collect_events(queue, timeout=2):
    events = []
    try:
        while True:
            ev = await asyncio.wait_for(queue.get(), timeout=timeout)
            events.append(ev)
            if ev.phase in (status.StatusPhase.END, status.StatusPhase.ERROR):
                break
    except asyncio.TimeoutError:
        pass
    return events



@pytest.mark.asyncio
async def test_terminal_events_bypass_suppression():
    # Strict rate limit
    os.environ["AGENT_STATUS_MAX_RPS"] = "1"

    q = await status.status_bus.subscribe(request_id="tid-test")

    # Rapid progress publishes (should be suppressed except some), then an END
    async def publisher():
        for i in range(10):
            await status.publish_status("testsrv", f"p{i}", request_id="tid-test", phase=status.StatusPhase.PROGRESS)
        await status.publish_status("testsrv", "terminal", request_id="tid-test", phase=status.StatusPhase.END)

    pub_task = asyncio.create_task(publisher())
    events = await _collect_events(q, timeout=5)
    await pub_task

    # There must be at least one END or ERROR in received events
    assert any(e.phase in (status.StatusPhase.END, status.StatusPhase.ERROR) for e in events), "Terminal event not delivered"
