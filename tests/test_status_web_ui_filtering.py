"""Test status web UI filtering functionality."""
import httpx
import pytest
from agent_system.agent.interface_api import build_app

pytestmark = pytest.mark.anyio


async def test_main_page_contains_filtering_logic():
    """Test that the main page contains JavaScript for status toggle functionality."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        
        # Check that the page contains the status toggle elements
        assert 'statusToggleBtn' in r.text
        assert 'Show status & metrics' in r.text
        assert 'aria-expanded="false"' in r.text
        
        # Check that JavaScript is included
        assert '/static/js/index.js' in r.text
        
        # Ensure old MCP calls functionality is removed
        assert 'MCP Calls' not in r.text
        assert 'mcpBox' not in r.text


async def test_main_page_status_structure():
    """Test that main page has the correct structure for status toggle functionality."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        
        # Verify the page contains status toggle elements
        content = r.text
        
        # Should have status toggle button
        assert 'statusToggleBtn' in content
        
        # Should have proper ARIA attributes
        assert 'aria-expanded="false"' in content
        
        # Should have status button text
        assert 'Show status & metrics' in content
        
        # Should have JavaScript inclusion
        assert '/static/js/index.js' in content
