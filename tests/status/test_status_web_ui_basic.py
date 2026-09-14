import httpx
import pytest
from agent_system.app import build_app

pytestmark = pytest.mark.anyio


async def test_status_page_redirects_to_main():
    """Test that the status page redirects to the main page since status is integrated."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/status', follow_redirects=False)
        assert r.status_code == 302
        assert r.headers.get('location') == '/'
