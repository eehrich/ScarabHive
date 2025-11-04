import pytest
from datetime import datetime
from agent_system.mcp.status import StatusBus, StatusEvent, publish_status, StatusPhase

pytestmark = pytest.mark.anyio


async def test_bus_filters_and_phases():
    bus = StatusBus()
    q_all = await bus.subscribe()
    q_srv = await bus.subscribe(server='alpha')
    q_req = await bus.subscribe(request_id='r1')
    q_both = await bus.subscribe(server='alpha', request_id='r1')

    ev1 = StatusEvent('alpha','r1','boot', datetime.now(), phase=StatusPhase.START)
    ev2 = StatusEvent('beta','r2','other', datetime.now(), phase=StatusPhase.PROGRESS)
    await bus.publish(ev1)
    await bus.publish(ev2)

    assert (await q_all.get()) == ev1
    assert (await q_all.get()) == ev2
    assert (await q_srv.get()) == ev1
    assert (await q_req.get()) == ev1
    assert (await q_both.get()) == ev1


async def test_error_phase_level_escalation(monkeypatch):
    captured = {}
    async def fake_publish(ev):
        captured['level'] = ev.level
        captured['phase'] = ev.phase
    from agent_system.mcp import status as status_mod
    orig = status_mod.status_bus.publish
    status_mod.status_bus.publish = fake_publish  # type: ignore
    try:
        await publish_status('esc','oops', phase=StatusPhase.ERROR, level='info')
    finally:
        status_mod.status_bus.publish = orig
    assert captured.get('phase') == StatusPhase.ERROR
    assert captured.get('level') == 'error'
