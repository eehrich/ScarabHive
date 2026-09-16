"""The Users panel in a real browser, against the real plugin: its router behind the app's route security, its static
files, a users database under tmp_path.

Accounts: ``root`` (admin, the viewer, with an API key), ``ada`` (admin, signed in before), ``bob`` (user) and
``mallory`` (inactive guest, a full name in markup), all with the password ``correct-horse``. A second instance,
``um_off``, runs with authentication off. The route rules are the defaults (any signed-in user), so a refusal of bob
is the plugin's own.

Behind the panel's back: GET /__stub/token?user= signs a token for that account (a test secret, not the config's);
GET /__stub/users lists username, email, full name, role and state; GET /__stub/verify?user=&password= checks a
password; GET /__stub/asked counts the account lists, creations and changes asked for; POST /__stub/email?user=&address=,
POST /__stub/add?user= and POST /__stub/remove?user= change the database. With the cookie ``um_users=slow`` the account list is answered after
1.5 s, ``fails`` fails it, ``slowfail`` fails it after 1.5 s; ``um_save=slow`` holds a change 1.5 s and a creation 3 s.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.auth import database
from agent_system.auth.models import UserCreate, UserRole
from agent_system.auth.security import create_access_token, verify_password
from agent_system.config.models import AgentSystemConfig, AuthConfig, MCPConfig
from agent_system.plugins.web_adapter import PluginWebRegistry
from agent_system.ui.resources import STATIC_DIR
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 150
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

TESTS = Path(__file__).resolve().parent
USERS = "/plugins/user_management/users"


def held(response, seconds: float, status: int | None = None, body: bytes | None = None):
    """Headers at once, the body later: a held answer must not hold the browser's cache lock on the same address."""
    async def later():
        await asyncio.sleep(seconds)
        if body is not None:
            yield body
        else:
            async for chunk in response.body_iterator:
                yield chunk
    headers = {} if response is None else {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return StreamingResponse(later(), status_code=status or response.status_code, media_type="application/json",
                             headers={**headers, "Cache-Control": "no-store"})


def panel_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from plugins.user_management.plugin import PLUGIN_FACTORY

    db = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", db)
    for name, role, active, full_name in [("root", UserRole.ADMIN, True, "Root Admin"), ("ada", UserRole.ADMIN, True, "Ada Lovelace"),
                                          ("bob", UserRole.USER, True, None),
                                          ("mallory", UserRole.GUEST, False, "<img src=x onerror=parent.__xss=1>")]:
        db.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse", role=role,
                                  is_active=active, full_name=full_name))
    db.generate_user_api_key(db.get_user_by_username("root").id)
    db.update_last_login(db.get_user_by_username("ada").id)

    auth = AuthConfig(enabled=True)
    auth.endpoint_security.audit_enabled = False
    app = FastAPI()
    asked = {"lists": 0, "created": 0, "changed": 0}

    @app.middleware("http")
    async def stand_ins(request: Request, call_next):
        path, cookies = request.url.path, request.cookies
        if path == USERS and request.method == "GET":
            asked["lists"] += 1
            mode = cookies.get("um_users")
            if mode == "fails":
                return JSONResponse({"detail": "The users table is locked"}, status_code=500, headers={"Cache-Control": "no-store"})
            if mode == "slowfail":
                return held(None, 1.5, 500, b'{"detail": "The users table is locked"}')
            response = await call_next(request)
            return held(response, 1.5) if mode == "slow" else response
        if path.startswith(USERS) and request.method in ("POST", "PUT"):
            asked["created" if request.method == "POST" else "changed"] += 1
            if cookies.get("um_save") == "slow":
                await asyncio.sleep(3 if request.method == "POST" else 1.5)
        return await call_next(request)

    @app.get("/__stub/token")
    async def token(user: str):
        return create_access_token({"sub": user, "user_id": db.get_user_by_username(user).id, "role": "admin"})

    @app.get("/__stub/users")
    async def users():
        return {u.username: [u.email, u.full_name, u.role.value, u.is_active] for u in db.list_users(limit=100)}

    @app.get("/__stub/verify")
    async def verify(user: str, password: str):
        return verify_password(password, db.get_user_by_username(user).hashed_password)

    @app.get("/__stub/asked")
    async def lists_asked():
        return asked

    @app.post("/__stub/email")
    async def email(user: str, address: str):
        from agent_system.auth.models import UserUpdate
        db.update_user(db.get_user_by_username(user).id, UserUpdate(email=address))
        return {}

    @app.post("/__stub/add")
    async def add(user: str):
        db.create_user(UserCreate(username=user, email=f"{user}@example.com", password="correct-horse"))
        return {}

    @app.post("/__stub/remove")
    async def remove(user: str):
        db.delete_user(db.get_user_by_username(user).id)
        return {}

    registry = PluginWebRegistry()  # the plugin's router and static files, mounted and secured as the app does it
    registry.register_web_plugin("user_management", PLUGIN_FACTORY("user_management", AgentSystemConfig(auth=auth), MCPConfig()))
    registry.register_web_plugin("um_off", PLUGIN_FACTORY("um_off", AgentSystemConfig(), MCPConfig()))
    registry.apply_to_app(app, auth)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/user_management", StaticFiles(directory=TESTS), name="panel-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        app = panel_app(tmp_path_factory.mktemp("users_panel"), monkeypatch)
        yield run_app_test_page(BROWSER, app, "tests/user_management/panel_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'opened by an admin, the panel lists every account with its role and state and counts them, without a credential',
    'names are drawn as text, never as markup',
    'the search filters by name and email and says when nothing matches',
    'a click on a column head sorts the accounts by value, and the order holds when they are drawn anew',
    'creating shows a refusal in the dialog, adds the account once on a double click and leaves no password in the page',
    'editing sends only what was changed and sets a password only when one is typed',
    'a late answer to a closed editor leaves a new one alone',
    'your own account cannot be deactivated, deleted or demoted from the panel',
    'deactivating and deleting ask first, once on a double click, and the keyboard stays on the row',
    'a failed load shows the error and nothing of the accounts shown before',
    'an answer overtaken by a later load is dropped, and a tick leaves a load on its way alone',
    'a user who is no admin is shown the refusal, and the server refuses every change they send',
    'with authentication off the panel says so and the API refuses',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_user_management_panel(results, name):
    assert results.get(name) == "ok", f"{name}: {results.get(name)!r} (all: {results})"


def test_the_page_runs_exactly_the_expected_checks(results):
    assert sorted(results) == sorted(EXPECTED)
