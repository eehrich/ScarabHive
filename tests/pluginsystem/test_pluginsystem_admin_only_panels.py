"""The log viewer and the SSH panel answer admins only, by the rules the shipped config carries.

log_viewer serves the logs, and they carry every user's prompts, names, IPs
and session ids; ssh_control runs commands on the configured hosts with the
server's keys and adds hosts. Any `user` token reached both. The rules are in
config/security.yaml (auth.plugin_security.endpoint_rules), which config.yaml
includes: this loads the shipped config, mounts the real plugins through the
registry with exactly those rules and asks every route they mount.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.config.models import AgentConfig, AgentSystemConfig, AuthConfig, ToolServerConfig
from agent_system.config.settings import load_settings
from agent_system.plugins.web_adapter import PluginWebRegistry
from http_routes import http_routes

SHIPPED_CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
PANELS = ("log_viewer", "ssh_control")


def _shipped_auth() -> AuthConfig:
    # as the API gets them: the include read, not one file's text
    auth = AuthConfig(enabled=True, plugin_security=load_settings(str(SHIPPED_CONFIG)).auth.plugin_security)
    auth.endpoint_security.audit_enabled = False
    return auth


@pytest.fixture
def panels(tmp_path, monkeypatch):
    return _mounted(tmp_path, monkeypatch, {"log_viewer": "log_viewer", "ssh_control": "ssh_control"})


@pytest.fixture
def renamed_panels(tmp_path, monkeypatch):
    # A second or renamed instance of either type: names no rule in the shipped config knows.
    return _mounted(tmp_path, monkeypatch, {"log_viewer": "ops_logs", "ssh_control": "ssh_lab"})


def _mounted(tmp_path, monkeypatch, names):
    from agent_system.auth import database, security
    from agent_system.auth.models import UserCreate, UserRole
    from plugins.log_viewer.plugin import LogViewerHybridPlugin
    from plugins.ssh_control.auth import SSHAuthenticator
    from plugins.ssh_control.plugin import PLUGIN_FACTORY as SSH_FACTORY

    async def no_connection(*args, **kwargs):
        raise AssertionError("the test must not open an SSH connection")

    monkeypatch.setattr(SSHAuthenticator, "create_connection", staticmethod(no_connection))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "api.log").write_text("2026-01-01 10:00:00,000 INFO x someone's prompt\n", encoding="utf-8")
    users = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", users)
    monkeypatch.setattr(security, "SECRET_KEY", "test-only-secret-not-the-config-one")
    for name, role in (("root", UserRole.ADMIN), ("bob", UserRole.USER)):
        users.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse", role=role))

    log_config = ToolServerConfig(type="log_viewer", enabled=True, agent_config=AgentConfig())
    log_config.log_files = ["logs/api.log"]
    ssh_config = ToolServerConfig()
    ssh_config.machines = [{"name": "alpha", "host": "alpha.test", "username": "deploy"}]
    ssh_config.security = {"audit_log": False}
    registry = PluginWebRegistry()
    log_name, ssh_name = names["log_viewer"], names["ssh_control"]
    registry.register_web_plugin(log_name, LogViewerHybridPlugin(log_name, AgentSystemConfig(), log_config))
    registry.register_web_plugin(ssh_name, SSH_FACTORY(ssh_name, AgentSystemConfig(), ssh_config))
    app = FastAPI()
    registry.apply_to_app(app, _shipped_auth())

    def headers(name):
        claims = {"sub": name, "user_id": users.get_user_by_username(name).id, "role": "user"}
        return {"Authorization": "Bearer " + security.create_access_token(claims)}

    return app, TestClient(app), headers


def _routes(app, names=PANELS):
    """(method, path) of every API route the two panels mount, path parameters filled in."""
    return sorted((method, re.sub(r"\{[^}]+\}", "x", path)) for method, path in http_routes(app)
                  if any(path.startswith(f"/plugins/{name}/") for name in names))


def test_a_user_reaches_no_route_of_either_panel(panels):
    app, client, headers = panels
    routes = _routes(app)
    assert {path.split("/")[2] for _, path in routes} == set(PANELS), routes
    answers = {(method, path): client.request(method, path, headers=headers("bob"), json={}).status_code
               for method, path in routes}
    assert answers and set(answers.values()) == {403}, answers


def test_a_renamed_instance_answers_admins_only_too(renamed_panels):
    # The rules match instance names; the plugin types declare admin themselves.
    app, client, headers = renamed_panels
    routes = _routes(app, ("ops_logs", "ssh_lab"))
    assert {path.split("/")[2] for _, path in routes} == {"ops_logs", "ssh_lab"}, routes
    answers = {(method, path): client.request(method, path, headers=headers("bob"), json={}).status_code
               for method, path in routes}
    assert answers and set(answers.values()) == {403}, answers
    for path in ("/plugins/ops_logs/", "/plugins/ops_logs/logs/list", "/plugins/ssh_lab/api/machines"):
        assert client.get(path, headers=headers("root")).status_code == 200, path


def test_an_admin_opens_both_panels(panels):
    _, client, headers = panels
    for path in ("/plugins/log_viewer/", "/plugins/log_viewer/logs/list", "/plugins/log_viewer/logs/content/logs/api.log",
                 "/plugins/ssh_control/", "/plugins/ssh_control/api/machines"):
        assert client.get(path, headers=headers("root")).status_code == 200, path


def test_every_instance_of_either_type_in_the_shipped_config_is_admin_only(monkeypatch):
    # The rules match request paths, that is instance names: a renamed or a second instance
    # of either plugin type would be open to every user while the tests above stay green.
    from agent_system.config import settings
    from agent_system.plugins.web_adapter import PluginEndpointSecurityEnforcer

    monkeypatch.setattr(settings, "_load_secrets_file", lambda path: None)  # keys stay out of this process
    config = settings.load_settings(str(SHIPPED_CONFIG))
    instances = [name for name, raw in (config.plugins.servers or {}).items()
                 if raw.enabled and settings.get_tool_server_config(name, config).type in PANELS]
    assert instances, "no log_viewer or ssh_control instance in the shipped config"
    enforcer = PluginEndpointSecurityEnforcer(config.auth)
    roles = {(name, method, path): enforcer.get_plugin_policy(name, path, method)["min_role"]
             for name in instances
             for method, path in (("GET", f"/plugins/{name}/"), ("POST", f"/plugins/{name}/api/execute"))}
    assert set(roles.values()) == {"admin"}, roles
