"""Test status web UI filtering functionality."""
import httpx
import pytest
from agent_system.agent.interface_api import build_app

pytestmark = pytest.mark.anyio


async def test_main_page_contains_filtering_logic():
    """Test that the main page contains JavaScript for proper status filtering."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        
        # Check that the page contains the status integration elements
        assert 'addStatusEvent' in r.text
        assert 'status-event' in r.text
        assert '/status/stream' in r.text
        
        # Check that status event styling is present
        assert 'status-phase' in r.text
        assert 'status-content' in r.text
        
        # Ensure old MCP calls functionality is removed
        assert 'MCP Calls' not in r.text
        assert 'mcpBox' not in r.text


async def test_main_page_status_structure():
    """Test that main page has the correct structure for status functionality."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        
        # Verify the JavaScript contains key status functions
        content = r.text
        
        # Should have status event creation function
        assert 'addStatusEvent' in content
        
        # Should connect to status stream
        assert 'EventSource(\'/status/stream\')' in content
        
        # Should have status styling
        assert '.status-event' in content
        
        # Should have event display elements
        assert 'statusBox' in content
        assert 'statusContainer' in content
