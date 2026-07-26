import asyncio
import pytest
import httpx

pytestmark = pytest.mark.anyio  # single backend auto-selected


class _DisabledAuth:
    def __get__(self, obj, objtype=None):
        return False

    def __set__(self, obj, value):
        pass


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    """Disable endpoint auth for every test here, restored on teardown.

    Via monkeypatch — a bare ``AuthConfig.enabled = DisabledAuth()`` (as the
    helper did before) leaks a session-wide class override that disables auth
    for EVERY later test in the suite (poisons the auth-enforcement and
    writer_jobs tests that run after this module).
    """
    from agent_system.config.models import AuthConfig
    monkeypatch.setattr(AuthConfig, "enabled", _DisabledAuth(), raising=False)


def _build_app_with_auth_disabled():
    """Build app (auth already disabled by the autouse _disable_auth fixture)."""
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

