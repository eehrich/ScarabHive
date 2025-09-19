import httpximport httpx

import pytestimport pytest



from agent_system.agent.interface_api import build_appfrom agent_system.agent.interface_api import build_app



pytestmark = pytest.mark.anyiopytestmark = pytest.mark.anyio





async def test_main_page_has_toolbar():

    """Test that the main page contains the status toggle functionality in the toolbar."""

    app = build_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:async def test_main_page_has_toolbar():async def test_main_page_has_toolbar():

        r = await client.get('/')

        assert r.status_code == 200    """Test that the main page contains the status toggle functionality in the toolbar."""    """Test that the main page contains the status toggle functionality in the toolbar."""

        

        # Check for status toggle elements in the toolbar    app = build_app()    app = build_app()

        assert 'statusToggleBtn' in r.text

        assert 'Status' in r.text    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        assert 'Show status & metrics' in r.text

        r = await client.get('/')        r = await client.get('/')



async def test_status_meta_endpoint_provides_metrics():        assert r.status_code == 200        assert r.status_code == 200

    """Test that /status/meta provides metrics for the toolbar status panel."""

    app = build_app()        # Check for status toggle elements in the toolbar        # Check for status toggle elements in the toolbar

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        r = await client.get('/status/meta')        assert 'statusToggleBtn' in r.text        assert 'statusToggleBtn' in r.text

        assert r.status_code == 200

                assert 'Status' in r.text        assert 'Status' in r.text

        data = r.json()

                assert 'Show status & metrics' in r.text        assert 'Show status & metrics' in r.text

        # Essential status metrics that the toolbar expects

        assert 'config' in data

        assert 'subscribers' in data

        assert 'publish_attempted' in data

        assert 'delivered' in data

        assert 'suppressed_rate' in dataasync def test_status_meta_endpoint_provides_metrics():async def test_status_meta_endpoint_provides_metrics():

        assert 'suppressed_debounce' in data

    """Test that /status/meta provides metrics for the toolbar status panel."""    """Test that /status/meta provides metrics for the toolbar status panel."""



async def test_debug_toggle_functionality():    app = build_app()    app = build_app()

    """Test that the debug toggle endpoint works correctly for the toolbar."""

    app = build_app()    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        # Test POST to toggle debug mode        r = await client.get('/status/meta')        r = await client.get('/status/meta')

        r = await client.post('/debug/toggle')

        assert r.status_code == 200        assert r.status_code == 200        assert r.status_code == 200

        

        # Response should contain debug status        data = r.json()        data = r.json()

        data = r.json()

        assert 'debug' in data                

        assert isinstance(data['debug'], bool)

        # Check structure - metrics are at root level, config is nested        # Check structure - metrics are at root level, config is nested



async def test_toolbar_responsive_layout():        assert 'config' in data        assert 'config' in data

    """Test that the main page includes responsive layout styles for the toolbar."""

    app = build_app()        assert 'subscribers' in data        assert 'subscribers' in data

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        r = await client.get('/')        assert 'publish_attempted' in data        assert 'publish_attempted' in data

        assert r.status_code == 200

                assert 'delivered' in data        assert 'delivered' in data

        # Check for responsive toolbar styles

        assert 'toolbar' in r.text.lower()        assert 'suppressed_rate' in data        assert 'suppressed_rate' in data

        # The page should include viewport meta for mobile responsiveness

        assert 'viewport' in r.text        assert 'suppressed_debounce' in data        assert 'suppressed_debounce' in data



        assert 'redacted' in data        assert 'redacted' in data

async def test_toolbar_toggle_javascript():

    """Test that the main page includes JavaScript for toolbar toggle functionality."""                

    app = build_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:        # Check config content          # Check config content  

        r = await client.get('/')

        assert r.status_code == 200        config = data['config']        config = data['config']

        

        # Check for toggle functionality JavaScript        assert 'AGENT_STATUS_MAX_RPS' in config        assert 'AGENT_STATUS_MAX_RPS' in config

        assert 'toggleStatus' in r.text or 'toggle' in r.text.lower()

        assert 'AGENT_STATUS_DEBOUNCE_MS' in config        assert 'AGENT_STATUS_DEBOUNCE_MS' in config



async def test_mcp_servers_toggle_in_toolbar():

    """Test that the MCP-Servers toggle is present in the toolbar."""

    app = build_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        r = await client.get('/')async def test_toolbar_responsive_layout():async def test_toolbar_responsive_layout():

        assert r.status_code == 200

            """Test that the toolbar layout is responsive."""    """Test that the toolbar layout is responsive."""

        # Check for MCP servers toggle

        assert 'MCP-Servers' in r.text or 'mcp' in r.text.lower()    app = build_app()    app = build_app()



    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

async def test_context_toggle_in_toolbar():

    """Test that the Context toggle is present in the toolbar."""        r = await client.get('/')        r = await client.get('/')

    app = build_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:        assert r.status_code == 200        assert r.status_code == 200

        r = await client.get('/')

        assert r.status_code == 200        # Check for responsive viewport meta tag        # Check for responsive viewport meta tag

        

        # Check for context toggle        assert 'viewport' in r.text        assert 'viewport' in r.text

        assert 'Context' in r.text or 'context' in r.text.lower()

        assert 'width=device-width' in r.text        assert 'width=device-width' in r.text



async def test_debug_toggle_in_toolbar():        # Check for main container structure        # Check for main container structure

    """Test that the Debug toggle is present in the toolbar."""

    app = build_app()        assert 'main-container' in r.text        assert 'main-container' in r.text

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        r = await client.get('/')        assert 'main-content' in r.text        assert 'main-content' in r.text

        assert r.status_code == 200

        

        # Check for debug toggle

        assert 'Debug' in r.text or 'debug' in r.text.lower()



async def test_toolbar_toggle_javascript():async def test_toolbar_toggle_javascript():

async def test_main_layout_structure():

    """Test that the main page has proper layout structure with toolbar."""    """Test that the page includes JavaScript for toolbar toggle functionality."""    """Test that the page includes JavaScript for toolbar toggle functionality."""

    app = build_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:    app = build_app()    app = build_app()

        r = await client.get('/')

        assert r.status_code == 200    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        

        # Check for proper HTML structure        r = await client.get('/')        r = await client.get('/')

        assert '<html' in r.text

        assert '<head>' in r.text        assert r.status_code == 200        assert r.status_code == 200

        assert '<body>' in r.text

                # Check for status toggle button functionality        # Check for status toggle button functionality

        # The toolbar should be in the main content area

        content = r.text.lower()        assert 'statusToggleBtn' in r.text        assert 'statusToggleBtn' in r.text

        assert 'toolbar' in content or 'toggle' in content
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
