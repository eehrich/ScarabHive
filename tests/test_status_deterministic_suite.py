import json
import asyncio
import httpx
import pytest
from datetime import datetime
from agent_system.mcp.status import StatusBus, StatusEvent, publish_status, StatusPhase
from agent_system.app import build_app

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

async def test_api_stream_filters_and_close_after():
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as client:
        async def _pub():
            await asyncio.sleep(0.05)
            await publish_status('fs','one', request_id='abc', phase=StatusPhase.PROGRESS)
            await publish_status('fs','two', request_id='abc', phase=StatusPhase.END)
        t = asyncio.create_task(_pub())
        resp = await client.get('/status/stream', params={'server':'fs','request_id':'abc','close_after':2})
        await t
        lines = [l for l in resp.text.splitlines() if l.startswith('data: ')]
        assert len(lines) == 2
        phases = [json.loads(l[6:])['phase'] for l in lines]
        assert phases == ['progress','end']

async def test_api_stream_heartbeat_only_then_event():
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as client:
        async def _pub():
            await asyncio.sleep(0.2)
            await publish_status('hb','late', phase=StatusPhase.PROGRESS)
        t = asyncio.create_task(_pub())
        resp = await client.get('/status/stream', params={'close_after':1,'heartbeat':5})
        await t
        body = resp.text.splitlines()
        assert any(l.startswith(':hb') or l==':ok' for l in body)

# The old CLI status subcommand has been removed and replaced with status events
# shown during normal 'run' operations. The previous test verifying a standalone
# status subcommand is obsolete and intentionally removed.

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
