import httpximport httpx

import pytestimport pytest

from agent_system.agent.interface_api import build_appfrom agent_system.agent.interface_api import build_app



pytestmark = pytest.mark.anyiopytestmark = pytest.mark.anyio





async def test_main_page_has_toolbar():async def test_main_page_has_toolbar():

    """Test that the main page contains the status toggle functionality in the toolbar."""    """Test that the main page contains the status toggle functionality in the toolbar."""

    app = build_app()    app = build_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        r = await client.get('/')        r = await client.get('/')

        assert r.status_code == 200        assert r.status_code == 200

        # Check for status toggle elements in the toolbar        # Check for status toggle elements in the toolbar

        assert 'statusToggleBtn' in r.text        assert 'statusToggleBtn' in r.text

        assert 'Status' in r.text        assert 'Status' in r.text

        assert 'Show status & metrics' in r.text        assert 'Show status & metrics' in r.text





async def test_status_meta_endpoint_provides_metrics():async def test_status_meta_endpoint_provides_metrics():

    """Test that /status/meta provides metrics for the toolbar status panel."""    """Test that /status/meta provides metrics for the toolbar status panel."""

    app = build_app()    app = build_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        r = await client.get('/status/meta')        r = await client.get('/status/meta')

        assert r.status_code == 200        assert r.status_code == 200

        data = r.json()        data = r.json()

                

        # Check structure - metrics are at root level, config is nested        # Check structure - metrics are at root level, config is nested

        assert 'config' in data        assert 'config' in data

        assert 'subscribers' in data        assert 'subscribers' in data

        assert 'publish_attempted' in data        assert 'publish_attempted' in data

        assert 'delivered' in data        assert 'delivered' in data

        assert 'suppressed_rate' in data        assert 'suppressed_rate' in data

        assert 'suppressed_debounce' in data        assert 'suppressed_debounce' in data

        assert 'redacted' in data        assert 'redacted' in data

                

        # Check config content          # Check config content  

        config = data['config']        config = data['config']

        assert 'AGENT_STATUS_MAX_RPS' in config        assert 'AGENT_STATUS_MAX_RPS' in config

        assert 'AGENT_STATUS_DEBOUNCE_MS' in config        assert 'AGENT_STATUS_DEBOUNCE_MS' in config





async def test_toolbar_responsive_layout():async def test_toolbar_responsive_layout():

    """Test that the toolbar layout is responsive."""    """Test that the toolbar layout is responsive."""

    app = build_app()    app = build_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        r = await client.get('/')        r = await client.get('/')

        assert r.status_code == 200        assert r.status_code == 200

        # Check for responsive viewport meta tag        # Check for responsive viewport meta tag

        assert 'viewport' in r.text        assert 'viewport' in r.text

        assert 'width=device-width' in r.text        assert 'width=device-width' in r.text

        # Check for main container structure        # Check for main container structure

        assert 'main-container' in r.text        assert 'main-container' in r.text

        assert 'main-content' in r.text        assert 'main-content' in r.text





async def test_toolbar_toggle_javascript():async def test_toolbar_toggle_javascript():

    """Test that the page includes JavaScript for toolbar toggle functionality."""    """Test that the page includes JavaScript for toolbar toggle functionality."""

    app = build_app()    app = build_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        r = await client.get('/')        r = await client.get('/')

        assert r.status_code == 200        assert r.status_code == 200

        # Check for status toggle button functionality        # Check for status toggle button functionality

        assert 'statusToggleBtn' in r.text        assert 'statusToggleBtn' in r.text

        assert 'aria-expanded="false"' in r.text        assert 'aria-expanded="false"' in r.text

        assert 'Show status & metrics' in r.text        assert 'Show status & metrics' in r.text

        # Check for JavaScript inclusion        # Check for JavaScript inclusion

        assert '/static/js/main.js' in r.text        assert '/static/js/main.js' in r.text





async def test_main_layout_structure():async def test_main_layout_structure():

    """Test that the main layout has proper structure with toolbar."""    """Test that the main layout has proper structure with toolbar."""

    app = build_app()    app = build_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        r = await client.get('/')        r = await client.get('/')

        assert r.status_code == 200        assert r.status_code == 200

        # Check for main container structure        # Check for main container structure

        assert 'main-container' in r.text        assert 'main-container' in r.text

        assert 'main-content' in r.text        assert 'main-content' in r.text

        assert 'chat' in r.text        assert 'chat' in r.text

        # Check for header/toolbar with status toggle        # Check for header/toolbar with status toggle

        assert 'statusToggleBtn' in r.text        assert 'statusToggleBtn' in r.text

        assert 'Status' in r.text        assert 'Status' in r.text

        # Check for input bar        # Check for input bar

        assert 'inputBar' in r.text        assert 'inputBar' in r.text

        assert 'task' in r.text        assert 'task' in r.text

        assert 'runBtn' in r.text        assert 'runBtn' in r.text
