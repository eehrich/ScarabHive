"""The Users API against a real users database under tmp_path: who may call it, what it answers, what it refuses.

The plugin's router is mounted bare (no route security from the config), so every refusal here is the plugin's own.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.auth import database
from agent_system.auth.models import UserCreate, UserRole
from agent_system.auth.security import create_access_token, verify_password
from agent_system.config.models import AgentSystemConfig, AuthConfig, MCPConfig
from plugins.user_management.plugin import PLUGIN_FACTORY

PUBLIC = {"id", "username", "email", "full_name", "role", "is_active", "created_at", "last_login", "has_api_key"}
PASSWORD = "correct-horse"


@pytest.fixture
def db(tmp_path, monkeypatch):
    users = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", users)
    for name, role, active in [("root", UserRole.ADMIN, True), ("ada", UserRole.ADMIN, True), ("bob", UserRole.USER, True),
                               ("idle", UserRole.ADMIN, False)]:
        users.create_user(UserCreate(username=name, email=f"{name}@example.com", password=PASSWORD, role=role, is_active=active))
    users.generate_user_api_key(users.get_user_by_username("ada").id)
    return users


def client(auth_enabled: bool = True) -> TestClient:
    plugin = PLUGIN_FACTORY("user_management", AgentSystemConfig(auth=AuthConfig(enabled=auth_enabled)), MCPConfig())
    app = FastAPI()
    app.include_router(plugin.get_web_router())
    return TestClient(app)


def as_user(name: str, **claims) -> dict:
    token = create_access_token({"sub": name, "role": "admin", **claims}, expires_delta=timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


def ids(db) -> SimpleNamespace:
    return SimpleNamespace(**{name: db.get_user_by_username(name).id for name in ("root", "ada", "bob", "idle")})


def snapshot(db) -> list:
    return [(u.username, u.email, u.full_name, u.role, u.is_active, u.hashed_password) for u in db.list_users(limit=100)]


def test_an_admin_lists_every_account_without_credentials(db):
    answer = client().get("/plugins/user_management/users", headers=as_user("root"))
    assert answer.status_code == 200
    body = answer.json()
    assert body["me"] == ids(db).root
    assert sorted(user["username"] for user in body["users"]) == ["ada", "bob", "idle", "root"]
    assert all(set(user) == PUBLIC for user in body["users"])
    assert {user["username"]: user["has_api_key"] for user in body["users"]} == {"root": False, "ada": True, "bob": False, "idle": False}
    assert "$2b$" not in answer.text


@pytest.mark.parametrize("who, status", [
    ("nobody", 401),
    ("a user", 403),
    ("a user whose token claims admin", 403),
    ("an inactive admin", 403),
    ("a refresh token", 401),
    ("a deleted admin", 401),
    ("an admin's API key", 401),
])
def test_the_plugin_refuses_everyone_but_an_active_admin(db, who, status):
    headers = {
        "nobody": {},
        "a user": as_user("bob", role="user"),
        "a user whose token claims admin": as_user("bob"),
        "an inactive admin": as_user("idle"),
        "a refresh token": {"Authorization": "Bearer " + create_access_token({"sub": "root", "role": "admin"}, token_type="refresh")},
        "a deleted admin": as_user("gone"),
        "an admin's API key": {"X-API-Key": db.generate_user_api_key(ids(db).root)} if who == "an admin's API key" else {},
    }[who]
    web = client()
    target = ids(db).ada
    before = snapshot(db)
    answers = [
        web.get("/plugins/user_management/users", headers=headers),
        web.post("/plugins/user_management/users", headers=headers,
                 json={"username": "mallory", "email": "m@example.com", "password": PASSWORD, "role": "admin"}),
        web.put(f"/plugins/user_management/users/{target}", headers=headers, json={"role": "guest", "password": "taken-over"}),
        web.delete(f"/plugins/user_management/users/{target}", headers=headers),
    ]
    assert [answer.status_code for answer in answers] == [status] * 4
    assert snapshot(db) == before


def test_with_authentication_off_the_api_refuses_and_the_page_says_so(db):
    web = client(auth_enabled=False)
    before = snapshot(db)
    assert web.get("/plugins/user_management/users", headers=as_user("root")).status_code == 403
    assert web.delete(f"/plugins/user_management/users/{ids(db).bob}", headers=as_user("root")).status_code == 403
    assert snapshot(db) == before
    page = web.get("/plugins/user_management/")
    assert page.status_code == 200 and "Authentication is off" in page.text
    assert "panel.js" not in page.text and 'id="users"' not in page.text
    enabled = client().get("/plugins/user_management/")
    assert "Authentication is off" not in enabled.text and "/plugins/user_management/static/panel.js" in enabled.text


def test_an_admin_cannot_lock_themselves_out(db):
    web = client()
    root = ids(db).root
    refusals = [
        web.put(f"/plugins/user_management/users/{root}", headers=as_user("root"), json={"role": "user"}),
        web.put(f"/plugins/user_management/users/{root}", headers=as_user("root"), json={"role": "guest", "email": "r@example.com"}),
        web.put(f"/plugins/user_management/users/{root}", headers=as_user("root"), json={"is_active": False}),
        web.delete(f"/plugins/user_management/users/{root}", headers=as_user("root")),
    ]
    assert [answer.status_code for answer in refusals] == [409] * 4
    me = db.get_user_by_id(root)
    assert (me.role, me.is_active, me.email) == (UserRole.ADMIN, True, "root@example.com")
    kept = web.put(f"/plugins/user_management/users/{root}", headers=as_user("root"),
                   json={"role": "admin", "is_active": True, "full_name": "Root"})
    assert kept.status_code == 200 and db.get_user_by_id(root).full_name == "Root"


def test_another_admin_can_be_demoted_deactivated_and_deleted(db):
    web = client()
    ada = ids(db).ada
    assert web.put(f"/plugins/user_management/users/{ada}", headers=as_user("root"), json={"role": "user"}).status_code == 200
    assert web.put(f"/plugins/user_management/users/{ada}", headers=as_user("root"), json={"is_active": False}).status_code == 200
    assert (db.get_user_by_id(ada).role, db.get_user_by_id(ada).is_active) == (UserRole.USER, False)
    assert web.delete(f"/plugins/user_management/users/{ada}", headers=as_user("root")).json() == {"deleted": ada}
    assert db.get_user_by_id(ada) is None
    assert web.delete(f"/plugins/user_management/users/{ada}", headers=as_user("root")).status_code == 404
    assert web.put(f"/plugins/user_management/users/{ada}", headers=as_user("root"), json={"full_name": "x"}).status_code == 404


def test_creating_validates_and_answers_without_credentials(db):
    web = client()
    new = {"username": "carol", "email": "carol@example.com", "password": PASSWORD, "full_name": "Carol", "role": "guest",
           "is_active": False, "hashed_password": "$2b$12$forged", "api_key": "forged"}
    created = web.post("/plugins/user_management/users", headers=as_user("root"), json=new)
    assert created.status_code == 200 and set(created.json()) == PUBLIC and created.json()["has_api_key"] is False
    carol = db.get_user_by_username("carol")
    assert (carol.role, carol.is_active, carol.api_key) == (UserRole.GUEST, False, None)
    assert verify_password(PASSWORD, carol.hashed_password)
    refused = {
        "duplicate username": ({**new, "email": "other@example.com"}, 409),
        "duplicate email": ({**new, "username": "carol2"}, 409),
        "short password": ({**new, "username": "dave", "email": "d@example.com", "password": "seven77"}, 422),
        "password over 72 bytes": ({**new, "username": "dave", "email": "d@example.com", "password": "€" * 25}, 422),
        "unknown role": ({**new, "username": "dave", "email": "d@example.com", "role": "superuser"}, 422),
        "role in capitals": ({**new, "username": "dave", "email": "d@example.com", "role": "ADMIN"}, 422),
        "bad email": ({**new, "username": "dave", "email": "not-an-email"}, 422),
        "bad username": ({**new, "username": "da ve", "email": "d@example.com"}, 422),
    }
    for name, (body, status) in refused.items():
        assert web.post("/plugins/user_management/users", headers=as_user("root"), json=body).status_code == status, name
    assert db.get_user_by_username("dave") is None and db.count_users() == 5


def test_updating_changes_only_what_is_sent_and_validates_it(db):
    web = client()
    bob = ids(db).bob
    url = f"/plugins/user_management/users/{bob}"
    changed = web.put(url, headers=as_user("root"), json={"full_name": "Bob B"})
    assert changed.status_code == 200 and set(changed.json()) == PUBLIC and "$2b$" not in changed.text
    after = db.get_user_by_id(bob)
    assert (after.full_name, after.email, after.role, after.is_active) == ("Bob B", "bob@example.com", UserRole.USER, True)
    assert verify_password(PASSWORD, after.hashed_password)
    for body, status in [({"password": "seven77"}, 422), ({"password": "€" * 25}, 422), ({"role": "owner"}, 422),
                         ({"email": "ada@example.com"}, 409), ({"email": "nope"}, 422)]:
        assert web.put(url, headers=as_user("root"), json=body).status_code == status, body
    assert verify_password(PASSWORD, db.get_user_by_id(bob).hashed_password)
    assert db.get_user_by_id(bob).email == "bob@example.com"
    assert web.put(url, headers=as_user("root"), json={"password": "a-new-secret"}).status_code == 200
    assert verify_password("a-new-secret", db.get_user_by_id(bob).hashed_password)


def test_state_changes_take_json_only(db):
    web = client()
    before = snapshot(db)
    body = '{"username": "mallory", "email": "m@example.com", "password": "correct-horse", "role": "admin"}'
    plain = web.post("/plugins/user_management/users", headers={**as_user("root"), "Content-Type": "text/plain"}, content=body)
    untyped = web.post("/plugins/user_management/users", headers=as_user("root"), content=body)
    form = web.post("/plugins/user_management/users", headers=as_user("root"),
                    data={"username": "mallory", "email": "m@example.com", "password": PASSWORD, "role": "admin"})
    put = web.put(f"/plugins/user_management/users/{ids(db).bob}", headers={**as_user("root"), "Content-Type": "text/plain"},
                  content='{"role": "admin"}')
    assert [plain.status_code, untyped.status_code, form.status_code, put.status_code] == [422, 415, 422, 422]
    assert snapshot(db) == before
