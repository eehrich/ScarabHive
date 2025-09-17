import json
import asyncio
import pytest
import httpx

from agent_system.agent.interface_api import build_app
from agent_system.mcp.status import publish_status, PHASE_START

pytestmark = pytest.mark.anyio  # single backend auto-selected


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_status_stream_immediate_close():
    app = build_app()
    async with _client(app) as client:
        resp = await client.get("/status/stream", params={"close_after": 0})
        assert resp.status_code == 200
        assert ":ok" in resp.text


async def test_status_stream_single_event():
    app = build_app()
    async with _client(app) as client:
        async def _delayed_publish():
            await asyncio.sleep(0.05)
            await publish_status(server="only_http", message="one", phase=PHASE_START)

        pub_task = asyncio.create_task(_delayed_publish())
        resp = await asyncio.wait_for(
            client.get("/status/stream", params={"close_after": 2}), timeout=5  # Expect 2 events now
        )
        await pub_task
        assert resp.status_code == 200
        body = resp.text.splitlines()
        data_lines = [line for line in resp.text.splitlines() if line.startswith("data: ")]
        assert len(data_lines) == 2, body  # Should have 2 events now
        # Check the second event (our published one)
        payload = json.loads(data_lines[1][6:])  # [1] for second event
        assert payload["server"] == "only_http"
