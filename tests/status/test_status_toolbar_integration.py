import httpx
import pytest

from agent_system.app import build_app

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def clean_status_env(monkeypatch):
    """Ensure status-related env vars are clean before each test."""
    # Remove any status auth env vars that might have been set
    for var in ["AGENT_STATUS_REQUIRE_AUTH", "AGENT_STATUS_TOKEN"]:
        monkeypatch.delenv(var, raising=False)
    yield


async def test_status_meta_endpoint_provides_metrics():
    """/status/meta returns the metric keys the System panel shows."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/status/meta')
        assert r.status_code == 200
        data = r.json()
        # Core metrics that the new status system provides
        assert 'handlers_count' in data
        assert 'sequence_counter' in data
        assert 'handler_types' in data
        assert 'subscribers' in data
        assert 'publish_attempted' in data
        assert 'delivered' in data

