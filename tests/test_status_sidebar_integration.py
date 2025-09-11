import httpx, asyncio, json
import re
import pytest
from agent_system.agent.interface_api import build_app
from agent_system.mcp.status import publish_status, PHASE_START, PHASE_END, get_status_metrics

pytestmark = pytest.mark.anyio


async def test_main_page_has_sidebar():
    """Test that the main page contains the status sidebar."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        # Check for sidebar elements
        assert 'sidebar' in r.text
        assert 'Status & Metrics' in r.text
        assert 'statusMetrics' in r.text
        assert 'updateStatusMetrics' in r.text


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
    """Test that the sidebar layout is responsive."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        # Check for mobile responsiveness
        assert '@media (max-width: 768px)' in r.text
        assert 'flex-direction: column' in r.text


async def test_sidebar_metrics_javascript():
    """Test that the sidebar includes JavaScript for metrics updates."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        # Check for metrics update functionality
        assert 'updateStatusMetrics' in r.text
        assert 'setInterval' in r.text
        assert '/status/meta' in r.text
        # Check for metric display elements
        assert 'metric-item' in r.text
        assert 'metric-label' in r.text
        assert 'metric-value' in r.text


async def test_main_layout_structure():
    """Test that the main layout has proper structure with sidebar."""
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get('/')
        assert r.status_code == 200
        # Check for main container structure
        assert 'main-container' in r.text
        assert 'main-content' in r.text
        assert 'sidebar' in r.text
        # Check for proper flexbox layout
        assert 'display: flex' in r.text
        assert 'flex: 1' in r.text  # main-content
        assert 'flex: 0 0 300px' in r.text  # sidebar
