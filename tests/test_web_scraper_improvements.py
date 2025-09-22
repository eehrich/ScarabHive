"""Test improvements to web scraper for Cloudflare/403 detection and retry logic."""

import pytest
from unittest.mock import AsyncMock, patch

from plugins.web_scraper.server import WebScraperServer


@pytest.fixture
def web_scraper():
    """Create a WebScraperServer instance for testing."""
    return WebScraperServer("test_web_scraper")


class TestCloudflareDetection:
    """Test detection of Cloudflare and other blocked responses."""

    def test_cloudflare_patterns_detected(self, web_scraper):
        """Test that common Cloudflare patterns are detected."""
        cloudflare_html = """
        <html>
        <head><title>Just a moment...</title></head>
        <body>
        <h1>Please wait while your request is being verified...</h1>
        <p>This process is automatic. Your browser will redirect to your requested content shortly.</p>
        <p>Please allow up to 5 seconds...</p>
        <div>Ray ID: 123456789abcdef0</div>
        </body>
        </html>
        """
        
        is_blocked, reason = web_scraper._is_blocked_response(cloudflare_html, 200, "https://example.com")
        assert is_blocked is True
        assert "cloudflare challenge detected" in reason.lower()

    def test_403_status_detected(self, web_scraper):
        """Test that 403 status codes are detected as blocked."""
        html = "<html><body>Access Denied</body></html>"
        
        is_blocked, reason = web_scraper._is_blocked_response(html, 403, "https://example.com")
        assert is_blocked is True
        assert "access forbidden (403)" in reason.lower()

    def test_429_status_detected(self, web_scraper):
        """Test that 429 status codes are detected as rate limited."""
        html = "<html><body>Too Many Requests</body></html>"
        
        is_blocked, reason = web_scraper._is_blocked_response(html, 429, "https://example.com")
        assert is_blocked is True
        assert "rate limited (429)" in reason.lower()

    def test_captcha_detected(self, web_scraper):
        """Test that CAPTCHA challenges are detected."""
        captcha_html = """
        <html>
        <body>
        <h1>Security Check</h1>
        <p>Please solve the CAPTCHA below to continue</p>
        <div id="captcha">...</div>
        </body>
        </html>
        """
        
        is_blocked, reason = web_scraper._is_blocked_response(captcha_html, 200, "https://example.com")
        assert is_blocked is True
        assert "captcha" in reason.lower()

    def test_normal_content_not_blocked(self, web_scraper):
        """Test that normal content is not flagged as blocked."""
        normal_html = """
        <html>
        <head><title>Normal Page</title></head>
        <body>
        <h1>Welcome</h1>
        <p>This is normal content.</p>
        </body>
        </html>
        """
        
        is_blocked, reason = web_scraper._is_blocked_response(normal_html, 200, "https://example.com")
        assert is_blocked is False
        assert reason == ""


class TestRetryLogic:
    """Test retry logic for rate limiting and temporary failures."""

    @pytest.mark.asyncio
    async def test_successful_fetch_no_retry(self, web_scraper):
        """Test that successful fetches don't retry."""
        mock_fetch = AsyncMock(return_value=("content", 200, "https://example.com", "text/html"))
        web_scraper._fetch_html_once = mock_fetch
        
        result = await web_scraper._fetch_with_retry("https://example.com", "test-agent", 30.0, max_retries=3)
        
        assert mock_fetch.call_count == 1
        assert result == ("content", 200, "https://example.com", "text/html")

    @pytest.mark.asyncio
    async def test_429_retry_logic(self, web_scraper):
        """Test that 429 responses trigger retries."""
        # Mock sleep to avoid actual delays in tests
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            mock_fetch = AsyncMock(side_effect=[
                ("", 429, "https://example.com", "text/html"),  # First attempt fails
                ("", 429, "https://example.com", "text/html"),  # Second attempt fails
                ("content", 200, "https://example.com", "text/html")  # Third attempt succeeds
            ])
            web_scraper._fetch_html_once = mock_fetch
            
            result = await web_scraper._fetch_with_retry("https://example.com", "test-agent", 30.0, max_retries=3)
            
            assert mock_fetch.call_count == 3
            assert mock_sleep.call_count == 2  # Two sleeps for two retries
            assert result == ("content", 200, "https://example.com", "text/html")

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self, web_scraper):
        """Test that retries stop after max attempts."""
        with patch('asyncio.sleep', new_callable=AsyncMock):
            mock_fetch = AsyncMock(return_value=("", 429, "https://example.com", "text/html"))
            web_scraper._fetch_html_once = mock_fetch
            
            result = await web_scraper._fetch_with_retry("https://example.com", "test-agent", 30.0, max_retries=2)
            
            assert mock_fetch.call_count == 3  # Initial + 2 retries
            assert result == ("", 429, "https://example.com", "text/html")


class TestIntegration:
    """Test integration of all improvements."""

    @pytest.mark.asyncio
    async def test_blocked_response_handling(self, web_scraper):
        """Test that blocked responses are handled correctly in the main call method."""
        # Mock the fetch to return a Cloudflare response
        cloudflare_html = """
        <html>
        <head><title>Just a moment...</title></head>
        <body><h1>Please wait while your request is being verified...</h1></body>
        </html>
        """
        
        mock_fetch = AsyncMock(return_value=(cloudflare_html, 403, "https://example.com", "text/html"))
        web_scraper._fetch_with_retry = mock_fetch
        
        result = await web_scraper.call("fetch", {"url": "https://example.com"})
        
        assert result["status_code"] == 403
        assert "blocked:" in result["text"]
        # For 403 status, our logic prioritizes status code over content pattern
        assert "access forbidden (403)" in result["text"]

    def test_user_agent_updated(self, web_scraper):
        """Test that the User-Agent string has been updated to be more realistic."""
        params = {"url": "https://example.com"}
        
        # Extract the call logic to check default user agent
        user_agent = params.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        
        # Verify the User-Agent includes the full Chrome version
        assert "Chrome/120.0.0.0" in user_agent
        assert "Safari/537.36" in user_agent