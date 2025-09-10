import httpx, asyncio, json
import re
import pytest
from agent_system.agent.interface_api import build_app
from agent_system.mcp.status import publish_status, PHASE_START, PHASE_END

pytestmark = pytest.mark.anyio


async def test_status_page_template_served():
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/status')
        assert r.status_code == 200
        assert '<title>Status Stream</title>' in r.text
        # minimal script markers
        assert 'EventSource' in r.text


async def test_status_page_receives_events():
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Open raw stream first (bypass HTML) to simulate browser SSE consumption
        async def _pub():
            await asyncio.sleep(0.05)
            await publish_status('webui','Boot', phase=PHASE_START)
            await publish_status('webui','Done', phase=PHASE_END)
        pub_task = asyncio.create_task(_pub())
        resp = await client.get('/status/stream', params={'close_after':2})
        await pub_task
        lines = [l for l in resp.text.splitlines() if l.startswith('data: ')]
        assert len(lines) == 2
        payloads = [json.loads(l[6:]) for l in lines]
        phases = {p['phase'] for p in payloads}
        assert {'start','end'} <= phases
