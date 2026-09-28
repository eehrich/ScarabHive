"""The agent role gate's one decision: who may run an agent that requires a role.

auth/agent_access.agent_run_denial is asked by every entry that starts a run --
the endpoints, the SAM, Agent.run_events as the backstop -- so each branch here
is a door. Fail closed: what cannot be identified or ranked is refused.

The user store is a tmp UserDatabase, never data/users.db.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from agent_system.auth import database
from agent_system.auth import agent_access
from agent_system.auth.agent_access import agent_run_denial, local_operator_trusted, may_run_agent
from agent_system.auth.enforcement import AnonymousUser
from agent_system.auth.models import User, UserCreate, UserRole
from agent_system.config.models import AnonymousAccessConfig, AuthConfig


#: Where no user store is: the AuthConfig default ("data/users.db", relative to the
#: working directory) would reach the real store of the checkout the tests run in.
NO_STORE_FILE = "/nonexistent/agent-gate-test/users.db"


def _auth(**kwargs) -> AuthConfig:
    kwargs.setdefault("database_path", NO_STORE_FILE)
    return AuthConfig(enabled=True, **kwargs)


def _account(name: str, role: UserRole, active: bool = True) -> User:
    return User(id=1, username=name, email=f"{name}@example.com", role=role, is_active=active,
                created_at="2026-01-01T00:00:00")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The process's user store (database._db) with root (admin), bob (user), gus (guest), idle (inactive admin)."""
    users = database.UserDatabase(tmp_path / "users.db")
    monkeypatch.setattr(database, "_db", users)
    for name, role, active in (("root", UserRole.ADMIN, True), ("bob", UserRole.USER, True),
                               ("gus", UserRole.GUEST, True), ("idle", UserRole.ADMIN, False)):
        users.create_user(UserCreate(username=name, email=f"{name}@example.com", password="correct-horse",
                                     role=role, is_active=active))
    return users


@pytest.fixture
def no_store(monkeypatch):
    """No store set up in this process: the gate must not ask one (and must not create one)."""
    monkeypatch.setattr(database, "_db", None)


class _StoreNobodyMayAsk:
    def get_user_by_username(self, username):
        raise AssertionError(f"the gate asked the user store for {username!r}")


# ---------------------------------------------------------------- no gate

def test_no_min_role_is_no_gate_and_asks_no_store(monkeypatch):
    monkeypatch.setattr(database, "_db", _StoreNobodyMayAsk())
    assert agent_run_denial(None, None, _auth()) is None
    assert agent_run_denial(None, "bob", _auth()) is None


@pytest.mark.parametrize("auth", [None, AuthConfig(enabled=False)])
def test_without_auth_there_is_no_role_to_compare(auth, monkeypatch):
    monkeypatch.setattr(database, "_db", _StoreNobodyMayAsk())
    assert agent_run_denial("admin", None, auth) is None
    assert agent_run_denial("admin", "bob", auth) is None


# ---------------------------------------------------------------- an account object

@pytest.mark.parametrize("role, gate, allowed", [
    (UserRole.ADMIN, "admin", True),
    (UserRole.USER, "admin", False),
    (UserRole.USER, "user", True),
    (UserRole.GUEST, "user", False),
    (UserRole.GUEST, "guest", True),
])
def test_an_account_object_is_ranked_by_its_role(role, gate, allowed):
    reason = agent_run_denial(gate, _account("someone", role), _auth())
    assert (reason is None) is allowed, reason
    if not allowed:
        assert gate in reason and role.value in reason, reason


def test_an_inactive_account_object_is_refused_whatever_its_role():
    reason = agent_run_denial("guest", _account("root", UserRole.ADMIN, active=False), _auth())
    assert reason is not None and "inactive" in reason, reason


def test_an_anonymous_user_object_answers_to_its_configured_role():
    assert agent_run_denial("guest", AnonymousUser(role="guest"), _auth()) is None
    assert agent_run_denial("user", AnonymousUser(role="guest"), _auth()) is not None


# ---------------------------------------------------------------- a name

@pytest.fixture
def local_process():
    """What agent_cli.main and agent_run.main do around everything they run."""
    with local_operator_trusted():
        yield


@pytest.fixture
def api_process(monkeypatch):
    """The API never opts in; set explicitly so a test that drove a CLI entry point cannot leak it here."""
    monkeypatch.setattr(agent_access, "_local_operator_trusted", False)


def test_the_local_operator_passes_every_gate_in_a_local_process(no_store, tmp_path, local_process):
    auth = _auth(database_path=str(tmp_path / "absent.db"))
    assert agent_run_denial("admin", "cli_user", auth) is None


def test_the_local_operator_passes_while_the_store_holds_no_account_of_that_name(store, local_process):
    assert agent_run_denial("admin", "cli_user", _auth()) is None


def test_in_the_api_process_the_operators_name_is_a_name_like_any_other(store, api_process):
    """No account holds it, so it is refused -- a run that carries "cli_user" into the API is nobody's."""
    reason = agent_run_denial("guest", "cli_user", _auth())
    assert reason is not None and "cli_user" in reason, reason


def test_a_run_another_process_starts_is_judged_as_that_process_will(store, api_process):
    """A wake spawns agent-cli: the API asks ahead with local_operator=True, agent-cli's own answer."""
    assert agent_run_denial("admin", "cli_user", _auth(), local_operator=True) is None
    assert agent_run_denial("admin", "cli_user", _auth(), local_operator=False) is not None


def test_the_trust_ends_with_its_scope(store, api_process):
    with local_operator_trusted():
        assert agent_run_denial("admin", "cli_user", _auth()) is None
    assert agent_run_denial("admin", "cli_user", _auth()) is not None


@pytest.mark.parametrize("entry", ["agent_system.agent_cli", "agent_system.agent_run"])
def test_the_local_entry_points_trust_the_operator_while_they_run(entry, monkeypatch, api_process):
    import importlib

    module = importlib.import_module(entry)
    seen = []
    monkeypatch.setattr(module, "_main", lambda: seen.append(agent_access._local_operator_trusted))

    module.main()

    assert seen == [True], f"{entry}.main ran without trusting the local operator"
    assert agent_access._local_operator_trusted is False, "the trust outlived the entry point"


@pytest.mark.parametrize("through", ["process store", "file"])
def test_an_older_account_under_the_operators_name_answers_with_its_own_role(store, monkeypatch, through,
                                                                           local_process):
    """The name is reserved at registration only, and only since the reservation exists: an account made
    before it logs in as "cli_user", and its runs carry that name into the SAM and the tool path."""
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute("INSERT INTO users (username, email, hashed_password, is_active, role, created_at) "
                     "VALUES ('cli_user', 'old@example.com', 'x', 1, 'user', '2025-01-01T00:00:00')")
        conn.commit()
    auth = _auth()
    if through == "file":
        monkeypatch.setattr(database, "_db", None)
        auth = _auth(database_path=str(store.db_path))

    assert agent_run_denial("admin", "cli_user", auth) is not None
    assert agent_run_denial("user", "cli_user", auth) is None


def test_a_store_that_cannot_be_read_is_refused_even_for_the_operator(no_store, tmp_path, local_process):
    broken = tmp_path / "users.db"
    broken.write_bytes(b"this is not a sqlite database" * 64)
    auth = _auth(database_path=str(broken))

    assert agent_run_denial("guest", "cli_user", auth) is not None
    assert agent_run_denial("guest", "root", auth) is not None


def test_anonymous_is_refused_while_anonymous_access_is_off(no_store):
    reason = agent_run_denial("guest", "anonymous", _auth())
    assert reason is not None and "anonymous" in reason, reason


def test_anonymous_answers_to_the_configured_anonymous_role(no_store):
    auth = _auth(anonymous_access=AnonymousAccessConfig(enabled=True, role="user"))
    assert agent_run_denial("user", "anonymous", auth) is None
    assert agent_run_denial("admin", "anonymous", auth) is not None


@pytest.mark.parametrize("name, gate, allowed", [
    ("root", "admin", True),
    ("bob", "admin", False),
    ("bob", "user", True),
    ("gus", "user", False),
    ("idle", "guest", False),     # inactive, although an admin
    ("nobody", "guest", False),   # no such account
])
def test_a_name_answers_to_its_accounts_current_role_in_the_store(store, name, gate, allowed):
    reason = agent_run_denial(gate, name, _auth())
    assert (reason is None) is allowed, reason


def test_a_role_changed_in_the_store_counts_from_the_next_ask(store):
    """A run woken hours later answers to the role the account has NOW."""
    from agent_system.auth.models import UserUpdate

    assert agent_run_denial("admin", "bob", _auth()) is not None
    store.update_user(store.get_user_by_username("bob").id, UserUpdate(role=UserRole.ADMIN))
    assert agent_run_denial("admin", "bob", _auth()) is None


def test_without_a_store_in_this_process_the_configured_file_is_read(store, monkeypatch):
    """agent-cli (a woken run) sets up no store: the gate reads the configured file itself."""
    path = store.db_path
    monkeypatch.setattr(database, "_db", None)
    auth = _auth(database_path=str(path))
    assert agent_run_denial("admin", "root", auth) is None
    assert agent_run_denial("admin", "bob", auth) is not None
    assert agent_run_denial("guest", "idle", auth) is not None
    assert agent_run_denial("guest", "nobody", auth) is not None


def test_a_missing_store_file_is_refused_and_not_created(no_store, tmp_path):
    path = tmp_path / "users.db"  # its folder exists: a store opened for writing would be made there
    reason = agent_run_denial("guest", "root", _auth(database_path=str(path)))
    assert reason is not None, "no store to ask, and the name was let through"
    assert not path.exists(), "the gate created a user store"


# ---------------------------------------------------------------- fail closed

class _RecordingStore:
    def __init__(self):
        self.asked = []

    def get_user_by_username(self, username):
        self.asked.append(username)
        return None


@pytest.mark.parametrize("who", [None, "", "  "])
def test_an_unidentified_caller_is_refused_without_asking_the_store(who, monkeypatch):
    recording = _RecordingStore()
    monkeypatch.setattr(database, "_db", recording)
    assert agent_run_denial("guest", who, _auth()) is not None
    assert recording.asked == [], "an empty name was looked up as an account"


def test_an_unknown_required_role_is_refused_even_to_an_admin(store):
    assert agent_run_denial("superuser", _account("root", UserRole.ADMIN), _auth()) is not None
    assert agent_run_denial("superuser", "root", _auth()) is not None


def test_an_unknown_account_role_in_the_store_is_refused(store, monkeypatch):
    """Through the process's store (whose row parser rejects it) and through the file."""
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute("UPDATE users SET role = 'owner' WHERE username = 'root'")
        conn.commit()
    assert agent_run_denial("guest", "root", _auth()) is not None

    monkeypatch.setattr(database, "_db", None)
    assert agent_run_denial("guest", "root", _auth(database_path=str(store.db_path))) is not None


def test_an_object_with_an_unknown_role_is_refused():
    class Odd:
        username = "odd"
        role = "owner"
        is_active = True

    assert agent_run_denial("guest", Odd(), _auth()) is not None


def test_a_caller_of_no_known_shape_is_refused():
    assert agent_run_denial("guest", 42, _auth()) is not None


def test_may_run_agent_is_the_denial_answered_yes_or_no():
    assert may_run_agent("admin", _account("root", UserRole.ADMIN), _auth()) is True
    assert may_run_agent("admin", _account("bob", UserRole.USER), _auth()) is False


def test_an_operator_named_account_the_store_cannot_parse_is_refused(store, local_process):
    """The process store's row parser rejects an unknown role: that is an unreadable account, not a missing one --
    missing would mean "the local operator"."""
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute("INSERT INTO users (username, email, hashed_password, is_active, role, created_at) "
                     "VALUES ('cli_user', 'old@example.com', 'x', 1, 'owner', '2025-01-01T00:00:00')")
        conn.commit()

    assert agent_run_denial("guest", "cli_user", _auth()) is not None
