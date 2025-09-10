"""Test status web UI filtering functionality."""
import httpx
import pytest
from agent_system.agent.interface_api import build_app

pytestmark = pytest.mark.anyio


async def test_status_page_contains_filtering_logic():
    """Test that the status page contains JavaScript for proper filtering."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/status')
        assert r.status_code == 200
        
        # Check that the page contains the filtering elements
        assert 'id="serverFilter"' in r.text
        assert 'id="requestFilter"' in r.text
        
        # Check that allEvents array is defined for storing events
        assert 'let allEvents = []' in r.text
        
        # Check that refreshTimeline function exists for re-filtering
        assert 'function refreshTimeline()' in r.text
        
        # Check that filter inputs call refreshTimeline instead of just clearing
        assert 'refreshTimeline' in r.text
        
        # Ensure the old broken behavior (just clearing without re-filtering) is not present
        assert "tl.innerHTML=''; total=0; counts.textContent='0 events';" not in r.text.replace('\n', '').replace(' ', '')


async def test_status_page_filtering_structure():
    """Test that status page has the correct structure for filtering functionality."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/status')
        assert r.status_code == 200
        
        # Verify the JavaScript contains key filtering functions
        content = r.text
        
        # Should have event storage
        assert 'allEvents.push(ev)' in content
        
        # Should have createEventElement function for reusable event creation
        assert 'function createEventElement(ev)' in content
        
        # Should filter existing events when filter changes
        assert 'allEvents.filter(applyFilters)' in content
