import httpx, asyncio, json
import re
import pytest
from agent_system.agent.interface_api import build_app
from agent_system.mcp.status import publish_status, PHASE_START, PHASE_END

pytestmark = pytest.mark.anyio


async def test_status_page_redirects_to_main():
    """Test that the status page redirects to the main page since status is integrated."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/status', follow_redirects=False)
        assert r.status_code == 302
        assert r.headers.get('location') == '/'


async def test_main_page_has_status_integration():
    """Test that the main page contains status toggle integration."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        assert '<title>Agent System (MCP)</title>' in r.text
        # Check for status toggle elements (replaced inline status events)
        assert 'statusToggleBtn' in r.text
        assert 'Show status & metrics' in r.text
        assert 'aria-expanded="false"' in r.text
        # Ensure MCP calls section is removed
        assert 'MCP Calls' not in r.text
        assert 'mcpBox' not in r.text
