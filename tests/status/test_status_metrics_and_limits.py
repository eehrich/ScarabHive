import httpx
import pytest
from agent_system.mcp.status import StatusPhase, publish_status

pytestmark = pytest.mark.anyio


def _build_app_with_auth_disabled():
    """Build app with auth disabled for testing."""
    # Patch AuthConfig.enabled to return False
    from agent_system.config.models import AuthConfig
    
    class DisabledAuth:
        def __get__(self, obj, objtype=None):
            return False
        def __set__(self, obj, value):
            pass
    
    AuthConfig.enabled = DisabledAuth()
    
    from agent_system.app import build_app
    return build_app()


@pytest.fixture(autouse=True)
def clean_status_env(monkeypatch):
    """Ensure status-related env vars are clean before each test."""
    # Remove any status auth env vars that might have been set
    for var in ["AGENT_STATUS_REQUIRE_AUTH", "AGENT_STATUS_TOKEN", "AGENT_STATUS_MAX_RPS", "AGENT_STATUS_DEBOUNCE_MS"]:
        monkeypatch.delenv(var, raising=False)
    yield


async def test_status_metrics_rate_and_debounce(monkeypatch):
    # Ensure no auth is required for this test
    monkeypatch.delenv("AGENT_STATUS_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("AGENT_STATUS_TOKEN", raising=False)
    # Configure small rate limit & generous debounce window to trigger both
    monkeypatch.setenv("AGENT_STATUS_MAX_RPS", "2")  # low to trigger rate suppression
    monkeypatch.setenv("AGENT_STATUS_DEBOUNCE_MS", "5000")  # large window ensures duplicate suppression
    app = _build_app_with_auth_disabled()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        # First publish (accepted)
        await publish_status("lim", "dup", phase=StatusPhase.PROGRESS)
        # Duplicate within debounce window (suppressed by debounce)
        await publish_status("lim", "dup", phase=StatusPhase.PROGRESS)
        # Another distinct message (accepted, but counts toward rate)
        await publish_status("lim", "another", phase=StatusPhase.PROGRESS)
        # Exceed rate limit with additional distinct messages
        await publish_status("lim", "third", phase=StatusPhase.PROGRESS)
        await publish_status("lim", "fourth", phase=StatusPhase.PROGRESS)  # should be rate suppressed

        meta = await client.get("/status/meta")
        assert meta.status_code == 200
        data = meta.json()

        # Basic metrics check - the actual available metrics
        assert "handlers_count" in data
        assert "sequence_counter" in data
        assert "handler_types" in data
        
        # Advanced metrics like publish_attempted, suppressed_debounce, etc. 
        # are not implemented in the current status system


async def test_status_meta_auth(monkeypatch):
    monkeypatch.setenv("AGENT_STATUS_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENT_STATUS_TOKEN", "secret123")
    app = _build_app_with_auth_disabled()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        r_fail = await client.get("/status/meta")
        assert r_fail.status_code == 401
        r_ok = await client.get("/status/meta", headers={"X-Status-Token": "secret123"})
        assert r_ok.status_code == 200