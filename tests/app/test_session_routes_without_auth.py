"""The session routes are mounted with authentication off too.

The web shell reads its session list from ``/api/sessions/hierarchy`` and a
session from ``/api/sessions/{id}``. Those routes were mounted only inside the
``auth.enabled`` branch, so with auth off -- the one-person setup -- every one
of them answered 404 and the sidebar stayed empty. Their user is optional
(``get_optional_user``); without auth a request acts as "anonymous".
"""
from __future__ import annotations

from http_routes import http_routes


class _AuthOff:
    """AuthConfig.enabled, pinned to False while the app is built."""

    def __get__(self, obj, objtype=None):
        return False

    def __set__(self, obj, value):
        pass


def test_session_routes_exist_with_auth_off(monkeypatch):
    from agent_system.config.models import AuthConfig

    monkeypatch.setattr(AuthConfig, "enabled", _AuthOff(), raising=False)
    from agent_system.app import build_app

    app = build_app()
    assert app.state.config.auth.enabled is False
    routes = {(method, path) for method, path in http_routes(app)}
    for path in ("/api/sessions/hierarchy", "/api/sessions/listing", "/api/sessions/active",
                 "/api/sessions/{session_id}"):
        assert ("GET", path) in routes, f"GET {path} is not mounted with auth off"
