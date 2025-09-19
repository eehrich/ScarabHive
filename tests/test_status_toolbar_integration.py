import httpx
import pytest

from agent_system.agent.interface_api import build_app

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
        assert 'config' in data
        assert 'subscribers' in data
        assert 'publish_attempted' in data
        assert 'delivered' in data
        assert 'suppressed_rate' in data
        assert 'suppressed_debounce' in data
        assert 'redacted' in data


async def test_debug_toggle_functionality():
    """POST /debug/toggle toggles debug flag and returns JSON with debug boolean."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post('/debug/toggle')
        assert r.status_code == 200
        data = r.json()
        assert 'debug' in data
        assert isinstance(data['debug'], bool)
