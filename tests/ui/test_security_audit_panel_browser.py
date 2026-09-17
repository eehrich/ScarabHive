"""The Security Audit panel in a real browser, against the real routes, middleware and audit.

The app: the UI and admin routers behind configure_security_middleware with the route rules of config/config.yaml
(plus open rules for the test's own routes), a users database and logs/security.log under tmp_path, a test signing
secret. Accounts: ``root`` (admin) and ``bob`` (user). What the panel shows is what the page's own requests left in the
audit: ``GET|POST|DELETE /tools/probe/<anything>?status=<code>`` answers that status, open to anyone, so the page seeds
the log by asking it. GET /__stub/token?user= signs a token for that account, /__stub/forged one for root with another
secret; GET /__stub/asked counts the audit loads the panel asked for and names the query of the last. With the cookie
``sa=slow`` a load is answered after 1.5 s, ``slower`` after 3 s, ``fails`` fails it, ``slowfail`` fails it after 1.5 s.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.api.admin_endpoints import router as admin_router
from agent_system.auth import database, security
from agent_system.auth.middleware import AUDIT_LOGGER_NAME, SecurityAuditMiddleware, configure_security_middleware
from agent_system.auth.models import UserCreate, UserRole
from agent_system.config.models import AuthConfig, EndpointSecurityConfig, EndpointSecurityRule
from agent_system.ui.resources import STATIC_DIR
from agent_system.ui.routes import router as ui_router
from tests.ui.browser import REPO, find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 150
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

UI_TESTS = Path(__file__).resolve().parent
SECRET = "test-only-secret-not-the-config-one-0123456789"
DATA = "/admin/security/audit"
OPEN = ["* /tests/*", "* /__stub/*", "POST /__results", "* /tools/probe/*"]


def held(payload: bytes, status: int, seconds: float) -> StreamingResponse:
    """Headers at once, the body later: a held answer must not hold the browser's cache lock on the same address."""
    async def later():
        await asyncio.sleep(seconds)
        yield payload
    return StreamingResponse(later(), status_code=status, media_type="application/json", headers={"Cache-Control": "no-store"})


def panel_app(root: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.chdir(root)  # logs/security.log lands here
    db = database.UserDatabase(root / "users.db")
    monkeypatch.setattr(database, "_db", db)
    monkeypatch.setattr(security, "SECRET_KEY", SECRET)
    for name, role in [("root", UserRole.ADMIN), ("bob", UserRole.USER)]:
        db.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse", role=role))
    monkeypatch.setattr(logging.getLogger(AUDIT_LOGGER_NAME), "handlers", [])
    monkeypatch.setattr(SecurityAuditMiddleware, "_instance", None)

    shipped = yaml.safe_load((REPO / "config" / "config.yaml").read_text(encoding="utf-8"))["auth"]["endpoint_security"]["rules"]
    rules = [EndpointSecurityRule(pattern=pattern, policy="allow_anonymous") for pattern in OPEN]
    auth = AuthConfig(enabled=True, secret_key=SECRET, endpoint_security=EndpointSecurityConfig(
        rules=rules + [EndpointSecurityRule(**rule) for rule in shipped]))
    app = FastAPI()
    app.state.config = SimpleNamespace(auth=auth)
    asked = {"loads": 0, "last": ""}

    @app.api_route("/tools/probe/{rest:path}", methods=["GET", "POST", "DELETE"])
    async def probe(rest: str, status: int = 200):
        return Response(status_code=status, headers={"Cache-Control": "no-store"})

    @app.get("/__stub/token")
    async def token(user: str):
        return security.create_access_token({"sub": user, "user_id": db.get_user_by_username(user).id, "role": "admin"})

    @app.get("/__stub/forged")
    async def forged():
        claims = {"sub": "root", "user_id": db.get_user_by_username("root").id, "role": "admin"}
        return security.create_access_token(claims, secret_key="not-the-secret-" * 4)

    @app.get("/__stub/asked")
    async def loads_asked():
        return asked

    app.include_router(ui_router)
    app.include_router(admin_router)
    configure_security_middleware(app, auth_config=auth, rate_limit_enabled=False, audit_enabled=True)

    @app.middleware("http")  # outermost: counts and holds what the panel asks, refused or not
    async def modes(request: Request, call_next):
        if request.url.path != DATA:
            return await call_next(request)
        asked["loads"] += 1
        asked["last"] = request.url.query
        mode = request.cookies.get("sa")
        if mode == "fails":
            return JSONResponse({"detail": "The audit buffer is locked"}, status_code=500, headers={"Cache-Control": "no-store"})
        if mode == "slowfail":
            return held(b'{"detail": "The audit buffer is locked"}', 500, 1.5)
        response = await call_next(request)
        if mode not in ("slow", "slower"):
            return response
        payload = b"".join([chunk async for chunk in response.body_iterator])
        return held(payload, response.status_code, 3 if mode == "slower" else 1.5)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/ui", StaticFiles(directory=UI_TESTS), name="ui-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        app = panel_app(tmp_path_factory.mktemp("security_audit_panel"), monkeypatch)
        try:
            yield run_app_test_page(BROWSER, app, "tests/ui/security_audit_panel_tests.html", timeout=PAGE_TIMEOUT)
        finally:
            for handler in logging.getLogger(AUDIT_LOGGER_NAME).handlers:
                handler.close()


EXPECTED = [
    'opened by an admin, the panel shows the newest requests first with method, path, user and status, and counts them',
    'server strings are drawn as text, never as markup',
    'the filters ask the server for a category, the chosen status classes and a count',
    'no status chosen asks nothing and says so',
    'a double click toggles once, the buttons stay off until the answer is drawn, and the keyboard stays on the button',
    'an unchanged answer draws nothing, a new request is drawn',
    "the panel's own reads are not listed among the requests",
    'an answer overtaken by a later load is dropped',
    'ticks of the auto refresh ask nothing more while a load is on its way',
    'a failed load shows the error and nothing of before',
    'an account that is no admin is refused the page and the data, and an open panel says so',
    'the auto refresh is on from the start and brings new requests',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_security_audit_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
