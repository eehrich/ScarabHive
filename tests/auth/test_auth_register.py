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


@pytest.mark.parametrize("username", ["cli_user", "anonymous", "CLI_User", "Anonymous"])
def test_nobody_registers_as_an_identity_that_runs_without_an_account(client, temp_db, username):
    # agent-cli runs as cli_user, unauthenticated web access as anonymous: an account
    # under either name would share their sessions and pass every check by name.
    response = client.post("/auth/register", json={**NEW_USER, "username": username})

    assert response.status_code == 400, response.text
    assert temp_db.get_user_by_username(username) is None


def test_a_name_that_differs_only_in_case_is_taken(client, temp_db):
    # A user's sessions live in a directory named after it; on Windows these are one.
    assert client.post("/auth/register", json=NEW_USER).status_code == 201

    response = client.post("/auth/register", json={**NEW_USER, "username": "Newbie",
                                                   "email": "other@example.com"})

    assert response.status_code == 400, response.text
    assert temp_db.get_user_by_username("Newbie") is None


def test_an_admin_cannot_create_one_either(temp_db):
    from agent_system.auth.models import UserCreate

    with pytest.raises(ValueError, match="reserved"):
        temp_db.create_user(UserCreate(username="cli_user", email="c@example.com",
                                       password="password123", role=UserRole.ADMIN))


# --- auth.registration: off, approval, default role ----------------------------------------------

def _registration(client, **settings):
    from agent_system.api.auth_endpoints import registration_settings
    from agent_system.config.models import RegistrationConfig

    client.app.dependency_overrides[registration_settings] = lambda: RegistrationConfig(**settings)
    return client


def test_registration_switched_off_creates_nobody(client, temp_db):
    response = _registration(client, enabled=False).post("/auth/register", json=NEW_USER)

    assert response.status_code == 403, response.text
    assert temp_db.get_user_by_username("newbie") is None


def test_a_registration_awaiting_approval_is_inactive_and_cannot_log_in(client, temp_db):
    response = _registration(client, require_approval=True).post("/auth/register", json=NEW_USER)

    assert response.status_code == 201, response.text
    assert response.json()["is_active"] is False
    assert temp_db.get_user_by_username("newbie").is_active is False
    login = client.post("/auth/login", json={"username": NEW_USER["username"], "password": NEW_USER["password"]})
    assert login.status_code == 403, login.text


def test_the_default_role_of_a_registration_can_be_guest(client, temp_db):
    _registration(client, default_role="guest").post("/auth/register", json=NEW_USER)

    assert temp_db.get_user_by_username("newbie").role == UserRole.GUEST


def test_no_configuration_makes_a_registration_an_admin():
    from pydantic import ValidationError

    from agent_system.config.models import RegistrationConfig

    with pytest.raises(ValidationError):
        RegistrationConfig(default_role="admin")


@pytest.mark.parametrize("typo", [{"enable": False}, {"require_aproval": True}])
def test_a_misspelled_registration_switch_is_refused_not_ignored(typo):
    # Dropped without a word, `enable: false` would leave registration open.
    from pydantic import ValidationError

    from agent_system.config.models import AuthConfig

    with pytest.raises(ValidationError):
        AuthConfig(registration=typo)


def test_the_running_app_s_registration_setting_is_the_one_applied(temp_db):
    from agent_system.config.models import AgentSystemConfig

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: temp_db
    app.state.config = AgentSystemConfig(auth={"registration": {"enabled": False}})

    assert TestClient(app).post("/auth/register", json=NEW_USER).status_code == 403
    assert temp_db.get_user_by_username("newbie") is None
