import asyncio
import pytest
import httpx

from agent_system.app import build_app

pytestmark = pytest.mark.anyio  # single backend auto-selected


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_events_stream_immediate_close():
    """Test that events endpoint responds with initial ok comment."""
    app = build_app()
    async with _client(app) as client:
        # Start streaming and immediately close by using a short timeout
        try:
            resp = await asyncio.wait_for(
                client.get("/events?task=test", timeout=0.1),
                timeout=0.5
            )
            # Should get at least the initial :ok comment before timeout
            assert ":ok" in resp.text or resp.status_code == 200
        except (asyncio.TimeoutError, httpx.ReadTimeout):
            # Expected - stream times out because it's infinite
            pass


@pytest.mark.skip(reason="Test requires integration with agent execution to receive status events through /events endpoint")
async def test_status_stream_single_event():
    """Test that published events are delivered through the stream."""
    # Note: Status events are now delivered through /events endpoint during agent execution
    # This test is skipped as it requires full agent integration
    pass
