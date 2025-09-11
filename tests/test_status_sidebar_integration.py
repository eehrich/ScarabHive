import httpx, asyncio, json
import re
import pytest
from agent_system.agent.interface_api import build_app
from agent_system.mcp.status import publish_status, PHASE_START, PHASE_END, get_status_metrics

pytestmark = pytest.mark.anyio


async def test_main_page_has_sidebar():
    """Test that the main page contains the status toggle functionality."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        # Check for status toggle elements (sidebar replaced with toggle button)
        assert 'statusToggleBtn' in r.text
        assert 'Status' in r.text
        assert 'Show status & metrics' in r.text


async def test_status_meta_endpoint_provides_metrics():
    """Test that /status/meta provides metrics for the sidebar."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/status/meta')
        assert r.status_code == 200
        data = r.json()
        
        # Check structure - metrics are at root level, config is nested
        assert 'config' in data
        assert 'subscribers' in data
        assert 'publish_attempted' in data
        assert 'delivered' in data
        assert 'suppressed_rate' in data
        assert 'suppressed_debounce' in data
        assert 'redacted' in data
        
        # Check config content  
        config = data['config']
        assert 'AGENT_STATUS_MAX_RPS' in config
        assert 'AGENT_STATUS_DEBOUNCE_MS' in config


async def test_sidebar_responsive_layout():
    """Test that the layout is responsive."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        # Check for responsive viewport meta tag
        assert 'viewport' in r.text
        assert 'width=device-width' in r.text
        # Check for main container structure (sidebar removed)
        assert 'main-container' in r.text
        assert 'main-content' in r.text


async def test_sidebar_metrics_javascript():
    """Test that the page includes JavaScript for status toggle functionality."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        # Check for status toggle button functionality
        assert 'statusToggleBtn' in r.text
        assert 'aria-expanded="false"' in r.text
        assert 'Show status & metrics' in r.text
        # Check for JavaScript inclusion
        assert '/static/js/index.js' in r.text


async def test_main_layout_structure():
    """Test that the main layout has proper structure without sidebar."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        # Check for main container structure (sidebar removed)
        assert 'main-container' in r.text
        assert 'main-content' in r.text
        assert 'chat' in r.text
        # Check for header with status toggle
        assert 'statusToggleBtn' in r.text
        assert 'Status' in r.text
        # Check for input bar
        assert 'inputBar' in r.text
        assert 'task' in r.text
        assert 'runBtn' in r.text
