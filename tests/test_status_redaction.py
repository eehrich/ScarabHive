import httpx, pytest, os
from agent_system.mcp.status import publish_status, PHASE_PROGRESS
from agent_system.agent.interface_api import build_app

pytestmark = pytest.mark.anyio

async def test_status_redaction_message_and_meta(monkeypatch):
    monkeypatch.setenv("AGENT_STATUS_REDACT_PATTERNS", "secret,token[0-9]+");
    monkeypatch.setenv("AGENT_STATUS_REDACT_REPLACEMENT", "[[REDACT]]")
    app = build_app()
    # Publish events containing sensitive tokens
    await publish_status("sec","user secret here", phase=PHASE_PROGRESS, meta={"note":"no secret"})
    await publish_status("sec","multi token123 and token999", phase=PHASE_PROGRESS, meta={"api_token":"xyz"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        meta = await client.get("/status/meta")
        assert meta.status_code == 200
        data = meta.json()
        # At least one redaction should have occurred
        assert data["redacted"] >= 2
        assert "AGENT_STATUS_REDACT_PATTERNS" in data["config"]
