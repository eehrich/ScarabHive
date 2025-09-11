import json, asyncio, httpx, pytest
from agent_system.agent.interface_api import build_app
from agent_system.mcp.status import publish_status, PHASE_START

pytestmark = pytest.mark.anyio


async def test_traceparent_meta_extraction():
    app = build_app()
    tp = '00-0123456789abcdef0123456789abcdef-0123456789abcdef-01'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        # Start a stream filtered to server
        async def _pub():
            await asyncio.sleep(0.05)
            await publish_status('trace_srv','boot', phase=PHASE_START, traceparent=tp)
        task = asyncio.create_task(_pub())
        resp = await client.get('/status/stream', params={'close_after':1, 'server':'trace_srv'})
        await task
        lines = [l for l in resp.text.splitlines() if l.startswith('data: ')]
        assert len(lines) == 1
        payload = json.loads(lines[0][6:])
        assert payload['meta']['trace_id'] == '0123456789abcdef0123456789abcdef'
        assert payload['meta']['span_id'] == '0123456789abcdef'
