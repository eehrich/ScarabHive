import httpx, pytest
from agent_system.mcp.status import publish_status, PHASE_PROGRESS
from agent_system.agent.interface_api import build_app

pytestmark = pytest.mark.anyio


async def test_status_metrics_rate_and_debounce(monkeypatch):
    # Configure small rate limit & generous debounce window to trigger both
    monkeypatch.setenv("AGENT_STATUS_MAX_RPS", "2")  # low to trigger rate suppression
    monkeypatch.setenv("AGENT_STATUS_DEBOUNCE_MS", "5000")  # large window ensures duplicate suppression
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        # First publish (accepted)
        await publish_status("lim", "dup", phase=PHASE_PROGRESS)
        # Duplicate within debounce window (suppressed by debounce)
        await publish_status("lim", "dup", phase=PHASE_PROGRESS)
        # Another distinct message (accepted, but counts toward rate)
        await publish_status("lim", "another", phase=PHASE_PROGRESS)
        # Exceed rate limit with additional distinct messages
        await publish_status("lim", "third", phase=PHASE_PROGRESS)
        await publish_status("lim", "fourth", phase=PHASE_PROGRESS)  # should be rate suppressed

        meta = await client.get("/status/meta")
        assert meta.status_code == 200
        data = meta.json()

        # publish_attempted increments for each call, even suppressed
        assert data["publish_attempted"] >= 5
        # We expect at least one debounce and one rate suppression
        assert data["suppressed_debounce"] >= 1
        assert data["suppressed_rate"] >= 1
        # Delivered can be 0 if no active subscribers during test; ensure it's not greater than attempts
        assert data["delivered"] <= data["publish_attempted"]


async def test_status_meta_auth(monkeypatch):
    monkeypatch.setenv("AGENT_STATUS_REQUIRE_AUTH", "1")
    monkeypatch.setenv("AGENT_STATUS_TOKEN", "secret123")
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        r_fail = await client.get("/status/meta")
        assert r_fail.status_code == 401
        r_ok = await client.get("/status/meta", headers={"X-Status-Token": "secret123"})
        assert r_ok.status_code == 200