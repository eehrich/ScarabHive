"""A plugin router that nests a router of its own is secured all the way down.

The registry puts its check on the include. Added route by route, it never reached a nested
router's routes on newer fastapi: there the nested router is ONE entry in router.routes,
with no dependencies of its own, and the check was dropped without a word.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from agent_system.config.models import AuthConfig
from agent_system.plugins.web_adapter import PluginWebRegistry


class _NestingPlugin:
    def get_web_router(self) -> APIRouter:
        outer, inner = APIRouter(prefix="/plugins/nesting"), APIRouter(prefix="/inner")

        @outer.get("/top")
        async def top():
            return {"ok": True}

        @inner.get("/deep")
        async def deep():
            return {"ok": True}

        outer.include_router(inner)
        return outer


def test_a_route_of_a_nested_plugin_router_asks_for_a_login():
    registry = PluginWebRegistry()
    registry.register_web_plugin("nesting", _NestingPlugin())
    app = FastAPI()
    auth = AuthConfig(enabled=True)
    auth.endpoint_security.audit_enabled = False
    registry.apply_to_app(app, auth)
    client = TestClient(app)
    assert client.get("/plugins/nesting/top").status_code == 401
    assert client.get("/plugins/nesting/inner/deep").status_code == 401
