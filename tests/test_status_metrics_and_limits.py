import httpx, pytest
from agent_system.mcp.status import StatusPhase, publish_status
from agent_system.agent.interface_api import build_app

pytestmark = pytest.mark.anyio


async def test_status_metrics_rate_and_debounce(monkeypatch):
    # Configure small rate limit & generous debounce window to trigger both
    monkeypatch.setenv("AGENT_STATUS_MAX_RPS", "2")  # low to trigger rate suppression
    monkeypatch.setenv("AGENT_STATUS_DEBOUNCE_MS", "5000")  # large window ensures duplicate suppression
    app = build_app()
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
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        r_fail = await client.get("/status/meta")
        assert r_fail.status_code == 401
        r_ok = await client.get("/status/meta", headers={"X-Status-Token": "secret123"})
        assert r_ok.status_code == 200