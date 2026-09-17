"""The security audit behind the Security Audit panel: what it keeps, who it names, who may read it.

The app here is the real one in miniature: the UI and admin routers behind configure_security_middleware,
a users database and logs/security.log under tmp_path, a test signing secret. Accounts: ``root`` (admin),
``bob`` (user), ``ada`` (deactivated admin). ``GET /tools/probe/<anything>?status=<code>`` answers that status,
``GET /run/boom`` crashes.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from agent_system.api.admin_endpoints import router as admin_router
from agent_system.auth import database, security
from agent_system.auth.middleware import (
    AUDIT_LOGGER_NAME,
    SecurityAuditMiddleware,
    configure_security_middleware,
)
from agent_system.auth.models import UserCreate, UserRole
from agent_system.config.models import AuthConfig, EndpointSecurityConfig, EndpointSecurityRule
from agent_system.plugins.web_adapter import PluginEndpointSecurityEnforcer
from agent_system.ui.routes import router as ui_router

REPO = Path(__file__).resolve().parents[2]
SECRET = "test-only-secret-not-the-config-one-0123456789"
DATA = "/admin/security/audit"
PAGE = "/ui/panels/security_audit"


@pytest.fixture
def users(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # logs/security.log lands here
    db = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", db)
    monkeypatch.setattr(security, "SECRET_KEY", SECRET)
    for name, role, active in [("root", UserRole.ADMIN, True), ("bob", UserRole.USER, True), ("ada", UserRole.ADMIN, False)]:
        db.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse", role=role,
                                  is_active=active))
    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    monkeypatch.setattr(audit_logger, "handlers", [])  # set up anew, under tmp_path
    monkeypatch.setattr(SecurityAuditMiddleware, "_instance", None)
    yield db
    for handler in audit_logger.handlers:
        handler.close()


def auth_config(audit=True, rules=(), default_policy="allow_anonymous") -> AuthConfig:
    """Without rules every route is open to the middleware: a refusal then is the route's own."""
    return AuthConfig(enabled=True, secret_key=SECRET, endpoint_security=EndpointSecurityConfig(
        audit_enabled=audit, default_policy=default_policy, rules=list(rules)))


def make_app(auth: AuthConfig, **middleware) -> FastAPI:
    app = FastAPI()
    app.state.config = SimpleNamespace(auth=auth)

    @app.get("/tools/probe/{rest:path}")
    async def probe(rest: str, status: int = 200):
        return Response(status_code=status)

    @app.get("/run/boom")
    async def boom():
        raise RuntimeError("boom")

    app.include_router(ui_router)
    app.include_router(admin_router)
    options = {"rate_limit_enabled": False, "security_headers_enabled": False, **middleware}
    configure_security_middleware(app, auth_config=auth, audit_enabled=auth.endpoint_security.audit_enabled, **options)
    return app


def token(db, name, secret=SECRET):
    claims = {"sub": name, "user_id": db.get_user_by_username(name).id, "role": "admin"}
    return {"Authorization": "Bearer " + security.create_access_token(claims, secret_key=secret)}


def kept(client, db, **params):
    response = client.get(DATA, headers=token(db, "root"), params=params)
    assert response.status_code == 200, response.text
    return response.json()["entries"]


def security_log(tmp_path) -> str:
    for handler in logging.getLogger(AUDIT_LOGGER_NAME).handlers:
        handler.flush()
    return (tmp_path / "logs" / "security.log").read_text(encoding="utf-8")


# ------------------------------------------------------------------ reading


def test_an_admin_reads_the_newest_requests_first_filtered_by_category_status_and_limit(users):
    client = TestClient(make_app(auth_config()))
    for path, status in [("/tools/probe/a", 200), ("/tools/probe/b", 302), ("/tools/probe/c", 404), ("/tools/probe/d", 503)]:
        client.get(path, params={"status": status})
    client.get("/health")

    newest = kept(client, users, category="tools")
    errors = kept(client, users, category="tools", status=["4xx", "5xx"])
    two = kept(client, users, category="tools", limit=2)
    everything = kept(client, users)

    assert [(e["path"], e["status_code"], e["category"]) for e in newest] == [
        ("/tools/probe/d", 503, "tools"), ("/tools/probe/c", 404, "tools"), ("/tools/probe/b", 302, "tools"), ("/tools/probe/a", 200, "tools")]
    assert [e["path"] for e in errors] == ["/tools/probe/d", "/tools/probe/c"]
    assert [e["path"] for e in two] == ["/tools/probe/d", "/tools/probe/c"]
    assert everything[0]["path"] == "/health" and everything[0]["category"] == "health"


@pytest.mark.parametrize("url", [DATA, PAGE])
def test_page_and_data_refuse_anyone_but_an_active_admin(users, url):
    client = TestClient(make_app(auth_config()))

    answers = {
        "root": client.get(url, headers=token(users, "root")).status_code,
        "bob": client.get(url, headers=token(users, "bob")).status_code,
        "deactivated admin": client.get(url, headers=token(users, "ada")).status_code,
        "nobody": client.get(url).status_code,
        "forged": client.get(url, headers=token(users, "root", secret="not-the-secret-" * 4)).status_code,
    }

    assert answers == {"root": 200, "bob": 403, "deactivated admin": 403, "nobody": 401, "forged": 401}


def test_a_refusal_comes_before_parameter_validation(users):
    client = TestClient(make_app(auth_config()))

    assert client.get(DATA, headers=token(users, "bob"), params={"limit": 0}).status_code == 403
    assert client.get(DATA, params={"category": "nonsense"}).status_code == 401


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 1001}, {"limit": "many"}, {"category": "static"}, {"category": "<b>"},
    {"status": "6xx"}, {"status": ["2xx", "404"]},
])
def test_parameters_outside_what_the_log_knows_are_refused(users, params):
    client = TestClient(make_app(auth_config()))

    assert client.get(DATA, headers=token(users, "root"), params=params).status_code == 422
    assert client.get(DATA, headers=token(users, "root"), params={"limit": 1000, "status": ["2xx", "5xx"]}).status_code == 200


@pytest.mark.parametrize("url", [DATA, PAGE])
def test_while_the_audit_is_off_page_and_data_are_not_found(users, url):
    off = TestClient(make_app(auth_config(audit=False)))
    assert off.get(url, headers=token(users, "root")).status_code == 404

    # also while an audit is kept in this process: the one of another app, or of this one before a config reload
    TestClient(make_app(auth_config())).get("/health")
    assert SecurityAuditMiddleware.get_instance() is not None
    assert off.get(url, headers=token(users, "root")).status_code == 404


def test_the_page_is_the_kit_panel_with_the_categories_the_log_knows(users):
    page = TestClient(make_app(auth_config())).get(PAGE, headers=token(users, "root"))

    assert "/static/js/panels/security_audit.js" in page.text and "/static/kit/kit.css" in page.text
    for category in ("plugin", "api", "agent", "auth", "tools", "debug", "health", "other"):
        assert f'<option value="{category}">' in page.text
    assert page.text.count('data-status="') == 4 and 'value="static"' not in page.text


def test_the_shipped_route_rules_refuse_the_page_to_non_admins_before_the_route(users):
    shipped = yaml.safe_load((REPO / "config" / "config.yaml").read_text(encoding="utf-8"))
    rules = [EndpointSecurityRule(**rule) for rule in shipped["auth"]["endpoint_security"]["rules"]]
    client = TestClient(make_app(auth_config(rules=rules, default_policy="require_auth")))

    for url in (PAGE, DATA):
        refused = client.get(url, headers=token(users, "bob"))
        # the middleware's wording, not require_admin's: the rule in config.yaml refused it
        assert (refused.status_code, refused.json()["detail"]) == (403, "Insufficient permissions. Required role: admin"), url
        assert client.get(url, headers=token(users, "root")).status_code == 200
    assert client.get(PAGE).status_code == 401

    # the audit wraps the endpoint security: its refusals are in the log, with who was refused
    refusals = [(e["path"], e["status_code"], e["user_id"]) for e in kept(client, users) if not e["allowed"]]
    assert refusals == [(PAGE, 401, "anonymous"), (DATA, 403, "bob"), (PAGE, 403, "bob")]


# ------------------------------------------------------------------ keeping


def test_the_log_names_the_verified_account_never_a_claim_anyone_can_write(users):
    client = TestClient(make_app(auth_config()))
    client.get("/tools/probe/real", headers=token(users, "bob"))
    client.get("/tools/probe/forged", headers=token(users, "root", secret="not-the-secret-" * 4))
    client.get("/tools/probe/deactivated", headers=token(users, "ada"))
    client.cookies.set("access_token", token(users, "root")["Authorization"][7:])
    client.get("/tools/probe/cookie")

    names = {e["path"].rsplit("/", 1)[1]: e["user_id"] for e in kept(client, users, category="tools")}

    assert names == {"real": "bob", "forged": "anonymous", "cookie": "root", "deactivated": "anonymous"}


def test_a_request_that_revokes_its_own_api_key_is_named_by_that_key(users):
    """The account is resolved before the route runs: afterwards the key no longer names anyone."""
    from agent_system.api.auth_endpoints import router as auth_router

    app = make_app(auth_config())
    app.include_router(auth_router)
    client = TestClient(app)
    key = users.generate_user_api_key(users.get_user_by_username("bob").id)

    revoked = client.delete("/auth/api-key", headers={"X-API-Key": key})

    assert revoked.status_code == 200, revoked.text
    assert users.get_user_by_username("bob").api_key is None, "fixture: the key was not revoked"
    assert [(e["path"], e["method"], e["user_id"]) for e in kept(client, users, category="auth")] == [
        ("/auth/api-key", "DELETE", "bob")]


def test_the_plugin_security_status_is_for_administrators_only(users):
    from agent_system.plugins.web_adapter import PluginWebRegistry

    app = make_app(auth_config())  # no route rules: the refusal is the route's own
    PluginWebRegistry().apply_to_app(app, app.state.config.auth)
    client = TestClient(app)
    url = "/api/plugins/security/status"

    answers = {name: client.get(url, headers=token(users, name)).status_code for name in ("root", "bob", "ada")}

    assert answers == {"root": 200, "bob": 403, "ada": 403}
    assert client.get(url).status_code == 401
    assert client.get(url, headers=token(users, "root")).json()["auth_enabled"] is True


def test_a_flood_the_rate_limiter_refuses_does_not_reach_the_audit(users):
    """The limiter shields the audit: a flood would push the real entries out of memory and the file."""
    client = TestClient(make_app(auth_config(), rate_limit_enabled=True, requests_per_minute=2))
    answers = [client.get("/tools/probe/flood").status_code for _ in range(4)]

    # read past the limiter: a read through it would be refused as well
    logged = SecurityAuditMiddleware.get_instance().get_audit_log(category="tools")
    assert answers == [200, 200, 429, 429]
    assert [e["status_code"] for e in logged] == [200, 200]


def test_the_account_is_looked_up_once_per_request_and_never_for_static_files(users, monkeypatch):
    from agent_system.auth.middleware import EndpointSecurityMiddleware

    real = EndpointSecurityMiddleware._extract_user_info
    lookups = []
    monkeypatch.setattr(EndpointSecurityMiddleware, "_extract_user_info",
                        lambda self, scope: lookups.append(scope["path"]) or real(self, scope))
    rules = [EndpointSecurityRule(pattern="/tools/probe/*", policy="require_auth", min_role="user")]
    client = TestClient(make_app(auth_config(rules=rules)))

    client.get("/tools/probe/protected", headers=token(users, "bob"))
    client.get("/health", headers=token(users, "bob"))
    client.get("/static/kit/kit.css", headers=token(users, "bob"))

    assert lookups == ["/tools/probe/protected", "/health"]
    assert {e["path"]: e["user_id"] for e in SecurityAuditMiddleware.get_instance().get_audit_log()} == {
        "/tools/probe/protected": "bob", "/health": "bob"}


def test_a_request_left_without_an_answer_is_filtered_with_the_server_errors(users):
    TestClient(make_app(auth_config())).get("/health")
    audit = SecurityAuditMiddleware.get_instance()
    audit._audit_access("/run/gone", "POST", "bob", "10.0.0.1", 0, 5.0, "agent", False)

    assert [e["path"] for e in audit.get_audit_log(category="agent", status_classes=("5xx",))] == ["/run/gone"]
    assert audit.get_audit_log(category="agent", status_classes=("2xx", "4xx")) == []


def test_a_request_that_crashes_is_audited_as_the_500_its_client_gets(users):
    client = TestClient(make_app(auth_config()), raise_server_exceptions=False)

    assert client.get("/run/boom").status_code == 500
    assert [(e["path"], e["status_code"]) for e in kept(client, users, category="agent")] == [("/run/boom", 500)]


def test_the_panels_own_answered_reads_stay_out_of_memory_but_not_out_of_the_file(users, tmp_path):
    client = TestClient(make_app(auth_config()))
    for _ in range(3):
        kept(client, users)
    client.get(DATA, headers=token(users, "bob"))

    reads = [e for e in kept(client, users, category="other") if e["path"] == DATA]

    assert [(e["status_code"], e["user_id"]) for e in reads] == [(403, "bob")]
    assert security_log(tmp_path).count(f"GET {DATA} | user=root") == 4


def test_every_audited_request_is_one_line_in_the_security_log(users, tmp_path):
    client = TestClient(make_app(auth_config()))
    PluginEndpointSecurityEnforcer(auth_config())  # the plugin route audit writes to the same file
    PluginEndpointSecurityEnforcer(auth_config()).audit_denied("probe", "/plugins/probe/x\nFORGED | y", "GET", None, "role")
    PluginEndpointSecurityEnforcer(auth_config()).audit_denied("pro | be", "/plugins/x", "GET", "bob | user=root", "role")
    client.get("/tools/probe/line%0AFORGED | GET /admin")
    client.get("/tools/probe/x%20|%20user=root%20|%20ip=1.2.3.4%20|%20status=200%20")

    log = security_log(tmp_path)
    lines = log.splitlines()

    assert len(logging.getLogger(AUDIT_LOGGER_NAME).handlers) == 1
    assert len(lines) == 4, log
    # asctime | level | verdict | request | user | ip | status | duration | category -- the plugin line: ... | plugin | user | reason
    assert [len(line.split(" | ")) for line in lines] == [7, 7, 9, 9], log
    assert [line.count(" | user=") for line in lines] == [1, 1, 1, 1], log
    assert log.count("GET /tools/probe/line%0AFORGED%20%7C%20GET%20/admin | user=anonymous") == 1
    assert "plugin=pro%20%7C%20be | user=bob%20%7C%20user=root" in log
