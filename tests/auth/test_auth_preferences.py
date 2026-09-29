"""GET/PUT /auth/me/preferences: how the web chat shows a run, kept with the account.

Driven through the real router and a real SQLite database, as the other /auth/me
tests are. Values are compared with the model's own defaults, not with literals:
what the defaults ARE is a design choice that may move; that every key comes back
filled in, and that a choice is kept per account, is the contract.
"""
import json
import sqlite3
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.api.auth_endpoints import router
from agent_system.auth.database import get_db, setup_database
from agent_system.auth.models import UserCreate, UserPreferences, UserRole


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "users.db"


@pytest.fixture
def db(db_path):
    return setup_database(db_path)


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _user(db, name):
    return db.create_user(UserCreate(username=name, email=f"{name}@example.com", password="password123",
                                     role=UserRole.USER, is_active=True))


def _auth(client, name):
    token = client.post("/auth/login", json={"username": name, "password": "password123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


DEFAULTS = UserPreferences().model_dump()
# A value other than the default for every chat key, taken from the model itself.
OTHER = {name: next(v for v in field.annotation.__args__ if v != field.default)
         for name, field in UserPreferences.model_fields["chat"].annotation.model_fields.items()}


def test_nothing_chosen_answers_every_key_with_its_default(client, db):
    _user(db, "alice")
    response = client.get("/auth/me/preferences", headers=_auth(client, "alice"))
    assert response.status_code == 200
    assert response.json() == DEFAULTS
    assert set(response.json()["chat"]) == set(OTHER), "a key the chat reads is missing from the answer"


def test_a_choice_is_kept_and_comes_back_whole(client, db, db_path):
    _user(db, "alice")
    headers = _auth(client, "alice")
    [(key, value)] = list(OTHER.items())[:1]

    saved = client.put("/auth/me/preferences", headers=headers, json={"chat": {key: value}})
    assert saved.status_code == 200
    expected = {"chat": {**DEFAULTS["chat"], key: value}}
    assert saved.json() == expected, "what is left out is the default"
    assert client.get("/auth/me/preferences", headers=headers).json() == expected
    # Kept on disk, not in the process: a restart reads it back.
    assert setup_database(db_path).get_preferences(db.get_user_by_username("alice").id).model_dump() == expected


def test_a_later_choice_replaces_the_earlier(client, db):
    _user(db, "alice")
    headers = _auth(client, "alice")
    [(key, value)] = list(OTHER.items())[:1]
    assert client.put("/auth/me/preferences", headers=headers, json={"chat": {key: value}}).status_code == 200
    again = client.put("/auth/me/preferences", headers=headers, json={"chat": {}})
    assert again.status_code == 200, again.text
    assert client.get("/auth/me/preferences", headers=headers).json() == DEFAULTS


def test_one_accounts_choice_is_not_anothers(client, db):
    _user(db, "alice")
    _user(db, "bob")
    client.put("/auth/me/preferences", headers=_auth(client, "alice"), json={"chat": OTHER})
    assert client.get("/auth/me/preferences", headers=_auth(client, "alice")).json() == {"chat": OTHER}
    assert client.get("/auth/me/preferences", headers=_auth(client, "bob")).json() == DEFAULTS


@pytest.mark.parametrize("body", [
    {"chat": {"fold_step": "never"}},          # a misspelt key
    {"chat": {"thinking": "sideways"}},        # a value nobody reads
    {"theme": "dark"},                         # a section that does not exist
])
def test_what_nobody_reads_is_refused_and_nothing_is_stored(client, db, body):
    _user(db, "alice")
    headers = _auth(client, "alice")
    client.put("/auth/me/preferences", headers=headers, json={"chat": OTHER})

    assert client.put("/auth/me/preferences", headers=headers, json=body).status_code == 422
    assert client.get("/auth/me/preferences", headers=headers).json() == {"chat": OTHER}


def test_without_signing_in_there_are_none(client, db):
    assert client.get("/auth/me/preferences").status_code == 401
    assert client.put("/auth/me/preferences", json={"chat": OTHER}).status_code == 401


def test_a_stored_choice_this_code_no_longer_knows_shows_the_defaults(client, db, db_path, caplog):
    user = _user(db, "alice")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("INSERT INTO user_preferences (user_id, data, updated_at) VALUES (?, ?, ?)",
                     (user.id, '{"chat": {"thinking": "renamed-since"}}', "2026-09-21T00:00:00+00:00"))
    response = client.get("/auth/me/preferences", headers=_auth(client, "alice"))
    assert response.status_code == 200 and response.json() == DEFAULTS
    assert "no longer fit" in caplog.text, "the fallback happened without a word"


def test_a_row_stored_before_a_key_existed_keeps_its_choices_and_gets_the_new_keys_default(
        client, db, db_path, caplog):
    """A key added later reaches accounts that saved before it: the row lacks it, nothing else.

    The row has the keys users.db held before `sub_agent_output`, each with a value other
    than its default: read as "no longer fits", the account would lose every one of them.
    """
    user = _user(db, "alice")
    chosen = {key: OTHER[key] for key in ("fold_steps", "thinking", "sub_agents")}
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("INSERT INTO user_preferences (user_id, data, updated_at) VALUES (?, ?, ?)",
                     (user.id, json.dumps({"chat": chosen}), "2026-09-21T22:19:01+00:00"))
    chat = client.get("/auth/me/preferences", headers=_auth(client, "alice")).json()["chat"]
    assert chat == {**DEFAULTS["chat"], **chosen}, "the account's own choices were dropped"
    assert "no longer fit" not in caplog.text


def test_deleting_an_account_deletes_its_preferences(db, db_path):
    user = _user(db, "alice")
    db.set_preferences(user.id, UserPreferences.model_validate({"chat": OTHER}))
    assert db.delete_user(user.id)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_preferences").fetchone()[0] == 0


def test_deleting_an_account_that_chose_nothing_still_reports_it_deleted(db):
    """Most accounts never save preferences; their deletion must not read as "not found"."""
    user = _user(db, "alice")
    assert db.delete_user(user.id)
    assert not db.delete_user(user.id), "a second deletion found the account again"


def test_a_database_from_before_gets_the_table(db_path):
    """users.db on a running server predates the table: opening it must add it."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("""CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL,
            full_name TEXT, hashed_password TEXT NOT NULL, api_key TEXT UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1, role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL, updated_at TEXT, last_login TEXT)""")
    db = setup_database(db_path)
    user = _user(db, "alice")
    db.set_preferences(user.id, UserPreferences.model_validate({"chat": OTHER}))
    assert db.get_preferences(user.id).model_dump() == {"chat": OTHER}
