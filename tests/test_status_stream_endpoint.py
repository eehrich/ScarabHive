import json
import asyncio
import pytest
import httpx

from agent_system.app import build_app
from agent_system.mcp.status import StatusPhase, publish_status

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
            await publish_status(server="only_http", message="one", phase=StatusPhase.START)

        pub_task = asyncio.create_task(_delayed_publish())
        resp = await asyncio.wait_for(
            client.get("/status/stream", params={"close_after": 1}), timeout=5  # Expect 1 event
        )
        await pub_task
        assert resp.status_code == 200
        body = resp.text.splitlines()
        data_lines = [line for line in resp.text.splitlines() if line.startswith("data: ")]
        assert len(data_lines) == 1, body  # Should have 1 event
        # Check the event (our published one)
        payload = json.loads(data_lines[0][6:])  # [0] for first event
        assert payload["server"] == "only_http"
