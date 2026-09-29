"""Shared fixtures for the status endpoint tests.

Every test under ``tests/status/`` builds the app via ``build_app()`` and hits
the status endpoints assuming **global endpoint auth is OFF**. (The status-token
mechanism, ``AGENT_STATUS_REQUIRE_AUTH`` / ``AGENT_STATUS_TOKEN``, is a separate
layer and is still enforced where a test opts into it.)

Historically each status file poked ``AuthConfig.enabled`` directly, without
restoring it — a session-wide class-level leak that disabled auth for the
*entire* test suite. Fixing that leak (per-file monkeypatch) exposed that some
status files never disabled auth themselves and silently relied on the leak
(they started returning 401). This package-scoped autouse fixture makes the
intent explicit and airtight: auth is disabled for every status test and
restored on teardown via monkeypatch.
"""
import pytest


class _DisabledAuth:
    """Data descriptor forcing ``AuthConfig(...).enabled`` to False.

    A data descriptor (``__get__`` + ``__set__``) on the class takes precedence
    over the pydantic instance field, so every AuthConfig instance reads False —
    a plain ``AuthConfig.enabled = False`` would not override instance access.
    """

    def __get__(self, obj, objtype=None):
        return False

    def __set__(self, obj, value):
        pass


@pytest.fixture(autouse=True)
def _disable_endpoint_auth(monkeypatch):
    from agent_system.config.models import AuthConfig
    monkeypatch.setattr(AuthConfig, "enabled", _DisabledAuth(), raising=False)
