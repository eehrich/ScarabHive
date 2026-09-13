"""POST /auth/register is reachable without login, so the caller must not be
able to choose its own privileges. It took the admin creation schema and
wrote the requested role straight to the database."""

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.api.auth_endpoints import router
from agent_system.auth.database import get_db, setup_database
from agent_system.auth.models import UserRole

NEW_USER = {"username": "newbie", "email": "newbie@example.com", "password": "password123"}


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield setup_database(Path(tmpdir) / "users.db")


@pytest.fixture
def client(temp_db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: temp_db
    return TestClient(app)


def test_a_registered_user_is_a_plain_user(client, temp_db):
    response = client.post("/auth/register", json=NEW_USER)

    assert response.status_code == 201, response.text
    stored = temp_db.get_user_by_username("newbie")
    assert stored.role == UserRole.USER
    assert stored.is_active is True


@pytest.mark.parametrize("field, value", [("role", "admin"), ("is_active", False)])
def test_registration_cannot_choose_role_or_active_state(client, temp_db, field, value):
    response = client.post("/auth/register", json={**NEW_USER, field: value})

    assert response.status_code == 422
    assert temp_db.get_user_by_username("newbie") is None


def test_changing_to_an_email_someone_else_has_is_a_400(client, temp_db):
    client.post("/auth/register", json=NEW_USER)
    client.post("/auth/register", json={**NEW_USER, "username": "other",
                                        "email": "other@example.com"})
    token = client.post("/auth/login", json={
        "username": "other", "password": "password123"}).json()["access_token"]

    response = client.patch("/auth/me", headers={"Authorization": f"Bearer {token}"},
                            json={"email": "newbie@example.com"})

    assert response.status_code == 400
    assert temp_db.get_user_by_username("other").email == "other@example.com"
