"""A password change ends every login made before it.

Tokens live as long as auth.access_token_expire_minutes says (years, here): whoever signed in with the old
password -- the setup's admin123 among them -- stayed in after the change. A token now carries the account's
password generation (``gen``); the dependency, the security middleware and /auth/refresh turn away one from
before, and the browser that made the change gets a fresh cookie.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from unittest.mock import AsyncMock

from agent_system.api.auth_endpoints import router
from agent_system.auth import database
from agent_system.auth.middleware import EndpointSecurityMiddleware
from agent_system.auth.models import UserCreate, UserRole, UserUpdate
from agent_system.auth.security import create_access_token
from agent_system.config.models import AuthConfig

OLD, NEW = "password123", "newpassword456"


@pytest.fixture
def db(tmp_path):
    db = database.UserDatabase(tmp_path / "users.db")
    for name in ("alice", "bob"):
        db.create_user(UserCreate(username=name, email=f"{name}@example.com", password=OLD, role=UserRole.USER))
    return db


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[database.get_db] = lambda: db
    return TestClient(app)


def _login(client, name="alice", password=OLD):
    response = client.post("/auth/login", json={"username": name, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def _me(client, token):
    return client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code


def _change(client, token):
    response = client.patch("/auth/me", headers={"Authorization": f"Bearer {token}"},
                            json={"password": NEW, "current_password": OLD})
    assert response.status_code == 200, response.text
    return response


def test_the_logins_made_before_a_password_change_end(client):
    browser, other_device = _login(client)["access_token"], _login(client)["access_token"]
    assert _me(client, browser) == _me(client, other_device) == 200  # the fixture signs in at all

    _change(client, browser)

    assert _me(client, other_device) == 401
    assert _me(client, browser) == 401  # its token too: it goes on with the cookie it got
    assert _me(client, _login(client, password=NEW)["access_token"]) == 200


def test_the_browser_that_changed_the_password_stays_signed_in(client):
    response = _change(client, _login(client)["access_token"])

    fresh = response.cookies.get("access_token")
    assert fresh, "no new cookie: the browser that changed its password is signed out"
    assert _me(client, fresh) == 200


def test_a_refresh_token_from_before_the_change_mints_nothing(client):
    tokens = _login(client)
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 200

    _change(client, tokens["access_token"])

    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    fresh = _login(client, password=NEW)["refresh_token"]
    assert client.post("/auth/refresh", json={"refresh_token": fresh}).status_code == 200


def test_a_token_from_before_generations_were_counted_ends_with_the_next_change(client, db):
    """What a server issued before this change: no ``gen`` at all -- the logins with admin123 still around."""
    alice = db.get_user_by_username("alice")
    older = create_access_token({"sub": "alice", "user_id": alice.id, "role": "user"})
    assert _me(client, older) == 200

    _change(client, _login(client)["access_token"])

    assert _me(client, older) == 401


def test_a_change_of_name_or_email_keeps_the_logins(client, db):
    token = _login(client)["access_token"]
    key = db.generate_user_api_key(db.get_user_by_username("alice").id)

    response = client.patch("/auth/me", headers={"Authorization": f"Bearer {token}"},
                            json={"full_name": "Alice", "email": "alice2@example.com"})

    assert response.status_code == 200, response.text
    assert _me(client, token) == 200
    client.cookies.clear()  # the login's cookie would sign in by itself
    assert client.get("/auth/me", headers={"X-API-Key": key}).status_code == 200


def test_a_password_change_revokes_the_accounts_api_key(client, db):
    """An API key made with the old password -- by whoever knew it -- is a login like any other."""
    key = db.generate_user_api_key(db.get_user_by_username("alice").id)
    assert client.get("/auth/me", headers={"X-API-Key": key}).status_code == 200  # fixture: the key signs in

    _change(client, _login(client)["access_token"])
    client.cookies.clear()  # the fresh cookie of the change would sign in by itself

    assert client.get("/auth/me", headers={"X-API-Key": key}).status_code == 401
    assert db.get_user_by_username("alice").api_key is None


def test_another_accounts_logins_stay(client):
    bob = _login(client, "bob")["access_token"]

    _change(client, _login(client)["access_token"])

    assert _me(client, bob) == 200


def _changed_by_another_process(db, name="alice"):
    """What `agent-cli users update --password` does meanwhile: the same database, written by another process."""
    db.update_user(db.get_user_by_username(name).id, UserUpdate(password="set-by-the-admin-1"))


def test_a_login_the_password_changes_under_is_ended_by_that_change(client, db, monkeypatch):
    """The old password checked, then a change lands before the token is signed: the token must not outlive it."""
    from agent_system.api import auth_endpoints
    real = auth_endpoints.verify_password

    def slow_check(password, hashed):  # bcrypt takes long enough for another process to write
        ok = real(password, hashed)
        _changed_by_another_process(db)
        return ok

    monkeypatch.setattr(auth_endpoints, "verify_password", slow_check)
    tokens = _login(client)
    monkeypatch.setattr(auth_endpoints, "verify_password", real)

    assert _me(client, tokens["access_token"]) == 401
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_a_login_checks_the_password_the_generation_it_signs_belongs_to(client, db, monkeypatch):
    """A change landing after the account was looked up, before its generation is read: the old password, checked
    against the hash looked up first, would get a token of the new generation."""
    real = db.token_generation

    def after_a_change(user_id):
        _changed_by_another_process(db)
        return real(user_id)

    monkeypatch.setattr(db, "token_generation", after_a_change)
    answer = client.post("/auth/login", json={"username": "alice", "password": OLD})
    monkeypatch.setattr(db, "token_generation", real)

    assert answer.status_code == 401 or _me(client, answer.json()["access_token"]) == 401


def test_a_refresh_the_password_changes_under_mints_nothing_that_outlives_it(client, db, monkeypatch):
    refresh = _login(client)["refresh_token"]
    real = db.token_generation
    changed = []

    def check_then_change(user_id):  # the refresh token checked, then a change lands before the new ones are signed
        value = real(user_id)
        if not changed:
            _changed_by_another_process(db)
            changed.append(True)
        return value

    monkeypatch.setattr(db, "token_generation", check_then_change)
    answer = client.post("/auth/refresh", json={"refresh_token": refresh})
    monkeypatch.setattr(db, "token_generation", real)

    assert answer.status_code == 200 and changed, "fixture: the refresh was not answered after the change"
    assert _me(client, answer.json()["access_token"]) == 401
    assert client.post("/auth/refresh", json={"refresh_token": answer.json()["refresh_token"]}).status_code == 401


def _signed_in(client, db, credential):
    """The headers of one of alice's logins: a token, or an API key of hers."""
    if credential == "token":
        return {"Authorization": f"Bearer {_login(client)['access_token']}"}
    return {"X-API-Key": db.generate_user_api_key(db.get_user_by_username("alice").id)}


@pytest.mark.parametrize("credential", ["token", "api key"])
def test_a_login_gets_a_new_api_key(client, db, credential):
    answer = client.post("/auth/api-key", headers=_signed_in(client, db, credential))

    assert answer.status_code == 200, answer.text
    client.cookies.clear()  # the login's cookie would sign in by itself
    assert client.get("/auth/me", headers={"X-API-Key": answer.json()["api_key"]}).status_code == 200


@pytest.mark.parametrize("credential", ["token", "api key"])
def test_a_password_change_landing_before_the_new_api_key_is_written_refuses_it(client, db, monkeypatch,
                                                                                credential):
    headers = _signed_in(client, db, credential)
    real = db.generate_user_api_key

    def after_a_change(user_id, **kwargs):  # the login checked, then another process changes the password
        _changed_by_another_process(db)
        return real(user_id, **kwargs)

    monkeypatch.setattr(db, "generate_user_api_key", after_a_change)
    answer = client.post("/auth/api-key", headers=headers)

    assert answer.status_code == 401, answer.text
    assert db.get_user_by_username("alice").api_key is None


def test_a_password_change_landing_while_an_api_key_signs_in_refuses_the_new_one(client, db, monkeypatch):
    headers = _signed_in(client, db, "api key")
    real = db.get_user_by_api_key

    def then_a_change(api_key_hash):  # looked up, then a change lands: the last_login write waits in between
        found = real(api_key_hash)
        _changed_by_another_process(db)
        return found

    monkeypatch.setattr(db, "get_user_by_api_key", then_a_change)
    answer = client.post("/auth/api-key", headers=headers)

    assert answer.status_code == 401, answer.text
    assert db.get_user_by_username("alice").api_key is None


def test_an_account_deleted_before_its_new_api_key_is_written_gets_none(client, db, monkeypatch):
    headers = _signed_in(client, db, "token")
    real = db.generate_user_api_key

    def after_a_deletion(user_id, **kwargs):  # the login checked, then an admin deletes the account
        db.delete_user(user_id)
        return real(user_id, **kwargs)

    monkeypatch.setattr(db, "generate_user_api_key", after_a_deletion)
    answer = client.post("/auth/api-key", headers=headers)

    assert answer.status_code == 401, answer.text  # a login that ended, not a server error


def _reset_wins(client, db, answer, token):
    """An admin's reset landed during the account's own change: the reset stands, the change and its login do not."""
    assert answer.status_code == 409, answer.text
    assert db.token_generation(db.get_user_by_username("alice").id) == 1  # the reset's count; the refusal added none
    assert "access_token" not in answer.cookies
    assert _me(client, token) == 401
    assert client.post("/auth/login", json={"username": "alice", "password": NEW}).status_code == 401
    assert client.post("/auth/login", json={"username": "alice", "password": "set-by-the-admin-1"}).status_code == 200


def test_a_reset_landing_while_the_current_password_is_checked_wins(client, db, monkeypatch):
    from agent_system.api import auth_endpoints
    token = _login(client)["access_token"]
    real = auth_endpoints.verify_password

    def slow_check(password, hashed):  # bcrypt takes long enough for another process to write
        ok = real(password, hashed)
        _changed_by_another_process(db)
        return ok

    monkeypatch.setattr(auth_endpoints, "verify_password", slow_check)
    answer = client.patch("/auth/me", headers={"Authorization": f"Bearer {token}"},
                          json={"password": NEW, "current_password": OLD})
    monkeypatch.setattr(auth_endpoints, "verify_password", real)

    _reset_wins(client, db, answer, token)


def test_a_reset_landing_right_before_ones_own_change_is_written_wins(client, db, monkeypatch):
    token = _login(client)["access_token"]
    real = db.update_user

    def overtaken(user_id, update, **kwargs):
        real(user_id, UserUpdate(password="set-by-the-admin-1"))  # another process's reset, just before this change
        return real(user_id, update, **kwargs)

    monkeypatch.setattr(db, "update_user", overtaken)
    answer = client.patch("/auth/me", headers={"Authorization": f"Bearer {token}"},
                          json={"password": NEW, "current_password": OLD})

    _reset_wins(client, db, answer, token)


def test_an_account_deleted_while_its_new_password_is_hashed_is_gone_not_a_conflict(client, db, monkeypatch):
    token = _login(client)["access_token"]
    real = database.get_password_hash

    def hash_then_delete(password):  # bcrypt takes long enough for an admin to delete the account
        hashed = real(password)
        db.delete_user(db.get_user_by_username("alice").id)
        return hashed

    monkeypatch.setattr(database, "get_password_hash", hash_then_delete)
    answer = client.patch("/auth/me", headers={"Authorization": f"Bearer {token}"},
                          json={"password": NEW, "current_password": OLD})

    assert answer.status_code == 400, answer.text  # what a vanished account answers, not "changed meanwhile"


def test_a_reset_landing_right_after_ones_own_change_ends_that_login_too(client, db, monkeypatch):
    """The cookie is valid for the count this change made, not for one a later reset made."""
    real = db.update_user

    def then_reset(user_id, update, **kwargs):
        updated = real(user_id, update, **kwargs)
        real(user_id, UserUpdate(password="set-by-the-admin-1"))  # another process's reset, right after
        return updated

    monkeypatch.setattr(db, "update_user", then_reset)
    fresh = _change(client, _login(client)["access_token"]).cookies.get("access_token")

    assert fresh, "fixture: no cookie"
    assert _me(client, fresh) == 401


@pytest.fixture
def admin_client(db):
    from agent_system.api.admin_endpoints import router as admin_router
    db.create_user(UserCreate(username="root", email="root@example.com", password=OLD, role=UserRole.ADMIN))
    app = FastAPI()
    app.include_router(router)
    app.include_router(admin_router)
    app.dependency_overrides[database.get_db] = lambda: db
    return TestClient(app)


def test_an_admin_who_sets_their_own_password_stays_signed_in_and_gets_no_one_elses_login(admin_client, db):
    root = _login(admin_client, "root")["access_token"]
    headers = {"Authorization": f"Bearer {root}"}

    others = admin_client.patch(f"/admin/users/{db.get_user_by_username('bob').id}", headers=headers,
                                json={"password": NEW})
    own = admin_client.patch(f"/admin/users/{db.get_user_by_username('root').id}", headers=headers,
                             json={"password": NEW})

    assert others.status_code == own.status_code == 200
    assert "access_token" not in others.cookies  # bob's password set: bob's logins end, the admin gets none of his
    assert _me(admin_client, own.cookies.get("access_token")) == 200
    assert _me(admin_client, root) == 401


def test_an_admin_whose_own_password_is_reset_meanwhile_does_not_overwrite_it(admin_client, db, monkeypatch):
    """As PATCH /auth/me: a reset from another process landing after the admin's login was checked stands, and the
    login it ended gets no fresh cookie."""
    root = _login(admin_client, "root")["access_token"]
    real = db.update_user

    def overtaken(user_id, update, **kwargs):
        real(user_id, UserUpdate(password="set-by-the-admin-1"))  # another process's reset, just before this change
        return real(user_id, update, **kwargs)

    monkeypatch.setattr(db, "update_user", overtaken)
    answer = admin_client.patch(f"/admin/users/{db.get_user_by_username('root').id}",
                                headers={"Authorization": f"Bearer {root}"}, json={"password": NEW})

    assert answer.status_code == 409, answer.text
    assert "access_token" not in answer.cookies
    assert admin_client.post("/auth/login", json={"username": "root", "password": NEW}).status_code == 401
    assert admin_client.post("/auth/login", json={"username": "root", "password": "set-by-the-admin-1"}).status_code == 200


def test_the_security_middleware_turns_away_a_token_from_before_the_change(db, monkeypatch):
    """The middleware decides by itself which routes a request reaches; it reads the token on its own."""
    monkeypatch.setattr(database, "_db", db)
    middleware = EndpointSecurityMiddleware(AsyncMock(), AuthConfig(enabled=True, secret_key="k" * 32, algorithm="HS256"))
    alice = db.get_user_by_username("alice")

    def identity(generation):
        token = jwt.encode({"sub": "alice", "user_id": alice.id, "role": "user", "gen": generation}, "k" * 32,
                           algorithm="HS256")
        return middleware._extract_user_info({"headers": [(b"authorization", f"Bearer {token}".encode())],
                                              "query_string": b""})

    assert identity(0) == ("alice", "user")
    db.update_user(alice.id, UserUpdate(password=NEW))  # the admin's way, the CLI's and the user panel's too

    assert identity(0) == (None, None)
    assert identity(1) == ("alice", "user")
