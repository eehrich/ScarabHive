"""The core auth and admin routers against a users database under tmp_path and a test secret: self-lockout via PATCH,
the login answer for over-long passwords, the byte limit on every password field, and tokens bound to their account.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.api.admin_endpoints import router as admin_router
from agent_system.api.auth_endpoints import router as auth_router
from agent_system.auth import database, security
from agent_system.auth.models import UserCreate, UserRole

PASSWORD = "correct-horse"
LONG = "€" * 25  # 75 bytes, 25 characters
EDGE = "€" * 24  # exactly 72 bytes


@pytest.fixture
def db(tmp_path, monkeypatch):
    users = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", users)
    monkeypatch.setattr(security, "SECRET_KEY", "test-only-secret-not-the-config-one")
    for name, role in [("root", UserRole.ADMIN), ("bob", UserRole.USER)]:
        users.create_user(UserCreate(username=name, email=f"{name}@example.com", password=PASSWORD, role=role))
    return users


@pytest.fixture
def web(db) -> TestClient:
    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(auth_router)
    return TestClient(app, raise_server_exceptions=False)


def login(web: TestClient, name: str, password: str = PASSWORD) -> dict:
    answer = web.post("/auth/login", json={"username": name, "password": password})
    assert answer.status_code == 200, answer.text
    return answer.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_an_admin_cannot_demote_or_deactivate_themselves_via_patch(web, db):
    me = bearer(login(web, "root")["access_token"])
    root = db.get_user_by_username("root").id
    for body in ({"role": "user", "is_active": False}, {"role": "user"}, {"role": "guest"}, {"is_active": False}):
        assert web.patch(f"/admin/users/{root}", headers=me, json=body).status_code == 400, body
    after = db.get_user_by_id(root)
    assert (after.role, after.is_active) == (UserRole.ADMIN, True)
    kept = web.patch(f"/admin/users/{root}", headers=me, json={"role": "admin", "is_active": True, "full_name": "Root"})
    assert kept.status_code == 200 and db.get_user_by_id(root).full_name == "Root"
    bob = db.get_user_by_username("bob").id
    assert web.patch(f"/admin/users/{bob}", headers=me, json={"is_active": False}).status_code == 200


def test_login_with_an_over_long_password_answers_like_a_wrong_one(web):
    assert web.post("/auth/login", json={"username": "bob", "password": LONG}).status_code == 401
    assert web.post("/auth/login", json={"username": "nosuch", "password": LONG}).status_code == 401


def test_login_for_an_unknown_account_does_the_same_password_check_as_for_a_known_one(web, monkeypatch):
    checks = []
    real = security.bcrypt.checkpw
    monkeypatch.setattr(security.bcrypt, "checkpw", lambda *args: checks.append(1) or real(*args))
    for name in ("bob", "nosuch"):
        checks.clear()
        assert web.post("/auth/login", json={"username": name, "password": "wrong-password"}).status_code == 401
        assert len(checks) == 1, name


def test_every_password_field_refuses_more_than_72_bytes(web, db):
    root_token = login(web, "root")["access_token"]
    me = bearer(root_token)
    bob = db.get_user_by_username("bob").id
    before = db.get_user_by_id(bob).hashed_password
    refused = {
        "register": web.post("/auth/register", json={"username": "dave", "email": "d@example.com", "password": LONG}),
        "admin create": web.post("/admin/users", headers=me,
                                 json={"username": "carol", "email": "c@example.com", "password": LONG}),
        "admin patch": web.patch(f"/admin/users/{bob}", headers=me, json={"password": LONG}),
        "patch me": web.patch("/auth/me", headers=me, json={"password": LONG, "current_password": PASSWORD}),
        "password reset": web.post("/auth/password-reset", json={"token": "x", "new_password": LONG}),
    }
    assert {name: answer.status_code for name, answer in refused.items()} == {name: 422 for name in refused}
    assert db.get_user_by_username("dave") is None and db.get_user_by_username("carol") is None
    assert db.get_user_by_id(bob).hashed_password == before
    assert web.post("/auth/register", json={"username": "erin", "email": "e@example.com", "password": EDGE}).status_code == 201
    assert web.post("/auth/login", json={"username": "erin", "password": EDGE}).status_code == 200


def test_tokens_of_a_deleted_account_do_not_work_for_a_new_account_of_the_same_name(web, db):
    me = bearer(login(web, "root")["access_token"])
    web.post("/admin/users", headers=me, json={"username": "eve", "email": "eve@example.com", "password": PASSWORD})
    old = login(web, "eve")
    assert web.get("/auth/me", headers=bearer(old["access_token"])).status_code == 200
    assert web.delete(f"/admin/users/{db.get_user_by_username('eve').id}", headers=me).status_code == 200
    created = web.post("/admin/users", headers=me,
                       json={"username": "eve", "email": "eve2@example.com", "password": "another-secret"})
    assert created.status_code == 201
    assert web.get("/auth/me", headers=bearer(old["access_token"])).status_code == 401
    assert web.post("/auth/refresh", json={"refresh_token": old["refresh_token"]}).status_code == 401
    new = login(web, "eve", "another-secret")
    assert web.get("/auth/me", headers=bearer(new["access_token"])).json()["email"] == "eve2@example.com"
    assert web.post("/auth/refresh", json={"refresh_token": new["refresh_token"]}).status_code == 200
