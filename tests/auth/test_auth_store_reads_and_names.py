"""The user store and the names and roles read against it.

* A name is one name in any case -- in the database too, so two registrations at once cannot both pass.
* The agent gate reads the store where UserDatabase keeps it (a ``data/...`` path in the data directory).
* One ranking of roles (enforcement.role_rank / meets_role) and one account-name pattern (models.USERNAME_RE).
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing, contextmanager

import pytest

from agent_system.auth import agent_access, database
from agent_system.auth.enforcement import has_role, meets_role, role_rank
from agent_system.auth.models import USERNAME_RE, UserCreate, UserRole
from agent_system.config.models import AuthConfig
from agent_system.paths import DATA_DIR_ENV


def _new(name: str, email: str) -> UserCreate:
    return UserCreate(username=name, email=email, password="correct-horse")


# -- case-insensitive uniqueness ------------------------------------------------

def test_the_database_refuses_a_name_that_differs_only_in_case(tmp_path):
    users = database.UserDatabase(tmp_path / "users.db")
    users.create_user(_new("Alice", "a@example.com"))
    with closing(sqlite3.connect(users.db_path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users (username, email, hashed_password, created_at) "
                     "VALUES ('alice', 'b@example.com', 'x', '2026-01-01')")


def test_an_insert_the_check_did_not_see_coming_is_a_value_error(tmp_path, monkeypatch):
    """The check and the insert are two statements: the account made in between is refused by the index."""
    users = database.UserDatabase(tmp_path / "users.db")
    users.create_user(_new("Alice", "a@example.com"))
    connect = users._get_connection
    calls = 0

    def check_sees_nothing():
        nonlocal calls
        calls += 1
        if calls == 1:  # the name check, as if it ran before the other registration's insert
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE users (username TEXT)")

            @contextmanager
            def empty():
                try:
                    yield conn
                finally:
                    conn.close()
            return empty()
        return connect()

    monkeypatch.setattr(users, "_get_connection", check_sees_nothing)
    with pytest.raises(ValueError, match="Username 'alice' already exists"):
        users.create_user(_new("alice", "b@example.com"))


def test_an_email_taken_meanwhile_names_the_email(tmp_path, monkeypatch):
    users = database.UserDatabase(tmp_path / "users.db")
    users.create_user(_new("alice", "a@example.com"))
    monkeypatch.setattr(users, "get_user_by_email", lambda email: None)  # the check missed it
    with pytest.raises(ValueError, match="Email 'a@example.com' already exists"):
        users.create_user(_new("bob", "a@example.com"))


def test_a_store_that_already_holds_case_twins_still_opens(tmp_path, caplog):
    path = tmp_path / "users.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
                        email TEXT UNIQUE NOT NULL, full_name TEXT, hashed_password TEXT NOT NULL,
                        api_key TEXT UNIQUE, is_active INTEGER NOT NULL DEFAULT 1,
                        role TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL, updated_at TEXT,
                        last_login TEXT)""")
        for name in ("Bob", "bob"):
            conn.execute("INSERT INTO users (username, email, hashed_password, created_at) VALUES (?, ?, 'x', ?)",
                         (name, f"{name}@example.com", "2026-01-01T00:00:00+00:00"))
        conn.commit()

    with caplog.at_level(logging.WARNING, logger="agent_system.auth.database"):
        users = database.UserDatabase(path)

    assert users.get_user_by_username("Bob") and users.get_user_by_username("bob")
    assert any("differ only in case" in r.message for r in caplog.records)
    with pytest.raises(ValueError):  # the check before the insert still holds
        users.create_user(_new("BOB", "c@example.com"))


# -- the agent gate reads the store UserDatabase opens --------------------------

def test_the_agent_gate_reads_the_store_in_the_data_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "elsewhere"))
    monkeypatch.chdir(tmp_path)  # a working directory without data/users.db
    users = database.UserDatabase("data/users.db")
    assert users.db_path == tmp_path / "elsewhere" / "users.db"
    users.create_user(UserCreate(username="bob", email="b@example.com", password="correct-horse",
                                 role=UserRole.USER))
    monkeypatch.setattr(database, "_db", None)  # a process without its own store: the file is read

    auth = AuthConfig(enabled=True, database_path="data/users.db")
    assert agent_access.agent_run_denial("user", "bob", auth) is None
    assert "has the role 'user'" in agent_access.agent_run_denial("admin", "bob", auth)


def test_user_db_path_is_what_user_database_opens(tmp_path, monkeypatch):
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    assert database.user_db_path("data/users.db") == tmp_path / "users.db"
    assert database.user_db_path(None) == tmp_path / "users.db"
    assert database.user_db_path(tmp_path / "x.db") == tmp_path / "x.db"


# -- one ranking, one name pattern ----------------------------------------------

@pytest.mark.parametrize("role, rank", [
    ("guest", 1), ("user", 2), ("admin", 3), ("Admin", 3), (" admin ", 3),
    (UserRole.ADMIN, 3), (UserRole.GUEST, 1), ("root", 0), (None, 0), (3, 0),
])
def test_role_rank(role, rank):
    assert role_rank(role) == rank


@pytest.mark.parametrize("role, min_role, ok", [
    ("admin", "user", True), ("user", "user", True), ("guest", "user", False),
    (UserRole.USER, "admin", False), ("user", None, True), ("nobody", "guest", False),
])
def test_meets_role(role, min_role, ok):
    assert meets_role(role, min_role) is ok


def test_has_role_and_the_agent_gate_rank_alike():
    class Account:
        role = UserRole.USER
        username = "bob"
        is_active = True

    auth = AuthConfig(enabled=True, database_path="/nonexistent/users.db")
    for needed in ("guest", "user", "admin"):
        assert has_role(Account(), needed) is (agent_access.agent_run_denial(needed, Account(), auth) is None)


@pytest.mark.parametrize("name, ok", [
    ("alice", True), ("A_b-9", True), ("a.b", False), ("ä", False), ("a b", False), ("alice\n", False),
])
def test_the_account_name_pattern_is_what_creating_one_accepts(name, ok):
    assert bool(USERNAME_RE.fullmatch(name)) is ok
    try:
        UserCreate(username=name + "xx", email="x@example.com", password="correct-horse")
        created = True
    except ValueError:
        created = False
    if name != "alice\n":
        assert created is ok
