"""The hook, tool and agent-debug routes check for an administrator themselves (O9).

They relied on the route rules in config/security.yaml alone: a local layer that loosened or dropped
those rules opened system internals to every user. With auth off there is one user, the owner, and
everything stays open.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.api import agent_routes, hook_routes, tool_routes
from agent_system.auth import database, dependencies
from agent_system.auth.models import UserRole
from agent_system.config.models import AgentSystemConfig, AuthConfig

ROUTES = [
    ("GET", "/hooks"),
    ("GET", "/hooks/some_hook"),
    ("GET", "/hooks/stats/all"),
    ("GET", "/hooks/stats/some_hook"),
    ("GET", "/tools/status"),
    ("GET", "/tools/cache/statistics"),
    ("POST", "/tools/cache/invalidate"),
    ("GET", "/agents/debug/an_agent/allowed-tools"),
    ("GET", "/agents/debug/an_agent/system-prompt"),
]


def _client(monkeypatch, *, auth: bool, role: UserRole | None = None) -> TestClient:
    """The three routers alone, no middleware: what answers is the routes' own check."""
    app = FastAPI()
    for module in (agent_routes, tool_routes, hook_routes):
        app.include_router(module.router)
    app.state.config = AgentSystemConfig(auth=AuthConfig(enabled=auth, secret_key="k" * 32))
    user = None if role is None else SimpleNamespace(role=role, is_active=True, username="ada")

    async def signed_in(*_args, **_kwargs):
        return user

    monkeypatch.setattr(dependencies, "get_optional_user", signed_in)
    monkeypatch.setattr(database, "get_db", lambda: None)
    return TestClient(app)


@pytest.mark.parametrize("method,path", ROUTES)
def test_a_user_is_refused(monkeypatch, method, path):
    answer = _client(monkeypatch, auth=True, role=UserRole.USER).request(method, path)

    assert answer.status_code == 403, answer.text


@pytest.mark.parametrize("method,path", ROUTES)
def test_nobody_signed_in_is_asked_to(monkeypatch, method, path):
    assert _client(monkeypatch, auth=True).request(method, path).status_code == 401


@pytest.mark.parametrize("method,path", ROUTES)
def test_an_admin_is_answered(monkeypatch, method, path):
    answer = _client(monkeypatch, auth=True, role=UserRole.ADMIN).request(method, path)

    assert answer.status_code == 200, answer.text


@pytest.mark.parametrize("method,path", ROUTES)
def test_with_auth_off_everything_stays_open(monkeypatch, method, path):
    answer = _client(monkeypatch, auth=False).request(method, path)

    assert answer.status_code == 200, answer.text


def test_the_user_routes_next_to_the_debug_ones_stay_open_to_users():
    """/agents/{name}/allowed-tools is the debug route's twin for every user: the check is per route."""
    from agent_system.api.debug_endpoints import require_admin_viewer

    checked = {route.path for route in agent_routes.router.routes
               if any(d.call is require_admin_viewer for d in route.dependant.dependencies)}

    assert checked == {"/agents/debug/{agent_name}/allowed-tools", "/agents/debug/{agent_name}/system-prompt",
                       "/admin/config"}
