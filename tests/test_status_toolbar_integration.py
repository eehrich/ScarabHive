import httpx
import pytest

from agent_system.app import build_app

pytestmark = pytest.mark.anyio


async def test_main_page_has_toolbar():
    """Main page contains toolbar and status toggle."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        assert 'statusToggleBtn' in r.text
        assert 'Status' in r.text
        assert 'Show status & metrics' in r.text


async def test_status_meta_endpoint_provides_metrics():
    """/status/meta returns expected metric keys for the toolbar."""
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

