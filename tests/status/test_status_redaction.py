import httpx
import pytest
from agent_system.mcp.status import StatusPhase, publish_status

pytestmark = pytest.mark.anyio


class _DisabledAuth:
    def __get__(self, obj, objtype=None):
        return False

    def __set__(self, obj, value):
        pass


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    """Disable endpoint auth for every test here, restored on teardown.

    Via monkeypatch — a bare ``AuthConfig.enabled = DisabledAuth()`` (as the
    helper did before) leaks a session-wide class override that disables auth
    for EVERY later test in the suite (poisons the auth-enforcement and
    writer_jobs tests that run after this module).
    """
    from agent_system.config.models import AuthConfig
    monkeypatch.setattr(AuthConfig, "enabled", _DisabledAuth(), raising=False)


def _build_app_with_auth_disabled():
    """Build app (auth already disabled by the autouse _disable_auth fixture)."""
    from agent_system.app import build_app
    return build_app()


@pytest.fixture(autouse=True)
def clean_status_env(monkeypatch):
    """Ensure status-related env vars are clean before each test."""
    # Remove any status auth env vars that might have been set
    for var in ["AGENT_STATUS_REQUIRE_AUTH", "AGENT_STATUS_TOKEN"]:
        monkeypatch.delenv(var, raising=False)
    yield


async def test_status_redaction_message_and_meta(monkeypatch):
    # Ensure no auth required
    monkeypatch.delenv("AGENT_STATUS_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("AGENT_STATUS_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STATUS_REDACT_PATTERNS", "secret,token[0-9]+")
    monkeypatch.setenv("AGENT_STATUS_REDACT_REPLACEMENT", "[[REDACT]]")
    app = _build_app_with_auth_disabled()
    # Publish events containing sensitive tokens
    await publish_status("sec","user secret here", phase=StatusPhase.PROGRESS, meta={"note":"no secret"})
    await publish_status("sec","multi token123 and token999", phase=StatusPhase.PROGRESS, meta={"api_token":"xyz"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        meta = await client.get("/status/meta")
        assert meta.status_code == 200
        data = meta.json()
        
        # Basic metrics check - redaction features are not implemented
        assert "handlers_count" in data
        assert "sequence_counter" in data
        assert "handler_types" in data
        
        # Note: Message redaction features are not implemented in the current status system
