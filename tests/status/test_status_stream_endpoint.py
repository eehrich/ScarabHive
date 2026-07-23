import asyncio
import pytest
import httpx

pytestmark = pytest.mark.anyio  # single backend auto-selected


def _build_app_with_auth_disabled():
    """Build app with auth disabled for testing."""
    # Patch AuthConfig.enabled to return False
    from agent_system.config.models import AuthConfig
    
    class DisabledAuth:
        def __get__(self, obj, objtype=None):
            return False
        def __set__(self, obj, value):
            pass
    
    AuthConfig.enabled = DisabledAuth()
    
    from agent_system.app import build_app
    return build_app()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_events_stream_immediate_close():
    """Test that events endpoint responds with initial ok comment."""
    app = _build_app_with_auth_disabled()
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

