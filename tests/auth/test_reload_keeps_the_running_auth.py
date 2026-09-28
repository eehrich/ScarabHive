"""A config reload leaves authentication as the server started with it.

The middleware, the signing key and the plugin route rules are set up once, at start. A reloaded auth section changed
only what the app says about itself: `auth.enabled: false` on disk made viewer_role take every caller for the admin
(require_admin_viewer let them through) while the API went on asking for tokens.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from agent_system.api.admin_endpoints import router as admin_router
from agent_system.auth import database
from agent_system.auth.dependencies import require_admin
from agent_system.config.models import AgentSystemConfig, AuthConfig
from agent_system.services import config_reload
from agent_system.ui.routes import viewer_role


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "_db", database.UserDatabase(tmp_path / "users.db"))
    on_disk = AgentSystemConfig(auth=AuthConfig(enabled=False))
    app = FastAPI()
    app.include_router(admin_router)
    app.state.config = AgentSystemConfig(auth=AuthConfig(enabled=True, secret_key="k" * 32))
    app.state.config_service = SimpleNamespace(load_config=lambda config_path=None, force_reload=False: on_disk)
    app.state.config_path = None
    app.state.plugins_saw = []
    # the live plugin instances refresh from the reloaded config: what they are handed is recorded
    monkeypatch.setattr(config_reload, "reload_plugin_configs",
                        lambda fresh: app.state.plugins_saw.append(fresh.auth.enabled) or {"refreshed": []})
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(username="root")
    return app


async def test_auth_turned_off_on_disk_leaves_the_running_server_asking_for_a_login(app):
    answer = TestClient(app).post("/admin/reload-config")

    assert answer.status_code == 200, answer.text
    assert answer.json()["report"]["auth"] == "changed on disk: takes effect on a restart"
    assert app.state.config.auth.enabled is True
    assert app.state.plugins_saw == [True]
    request = Request({"type": "http", "app": app, "headers": [], "method": "GET", "path": "/ui/catalog",
                       "query_string": b""})
    with pytest.raises(HTTPException) as refused:
        await viewer_role(request)  # nobody signed in: before, "admin"
    assert refused.value.status_code == 401


async def test_a_reload_without_an_auth_change_says_nothing_about_it(app):
    app.state.config_service = SimpleNamespace(
        load_config=lambda config_path=None, force_reload=False: AgentSystemConfig(
            auth=AuthConfig(enabled=True, secret_key="k" * 32)))

    answer = TestClient(app).post("/admin/reload-config")

    assert answer.status_code == 200, answer.text
    assert "auth" not in answer.json()["report"]
