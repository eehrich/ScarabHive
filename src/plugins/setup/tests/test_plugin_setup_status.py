"""The setup status: which keys the configuration names and what state they are in, the admin's
password and the signing key. Read through the files the loader reads, never a hand-built list."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.setup import status

REPO = Path(__file__).resolve().parents[4]


def write_config(root: Path, master: str, **includes: str) -> str:
    (root / "config.yaml").write_text(master, encoding="utf-8")
    for name, text in includes.items():
        (root / f"{name}.yaml").write_text(text, encoding="utf-8")
    return str(root / "config.yaml")


class TestTheKeysTheConfigurationNames:
    def test_a_key_is_found_where_the_loader_reads_it_and_a_comment_names_nothing(self, tmp_path):
        config = write_config(
            tmp_path,
            "includes:\n  - llm.yaml\n  - plugins.yaml\n",
            llm="llm_system:\n  models:\n    fast:\n      api_key: ${FAST_KEY}\n"
                "      headers: {x-key: \"${FAST_KEY}\"}\n"  # twice in one section: named once
                "      # api_key: ${COMMENTED_KEY}\n    other:\n      api_key: \"${FAST_KEY}\"\n",
            plugins="plugins:\n  servers:\n    search:\n      config:\n        key: \"Bearer ${SEARCH_KEY}\"\n",
        )

        found = status.referenced_keys(config)

        assert found == {"FAST_KEY": ["llm_system.models.fast", "llm_system.models.other"],
                         "SEARCH_KEY": ["plugins.servers.search"]}

    def test_a_file_the_master_does_not_include_names_nothing(self, tmp_path):
        config = write_config(tmp_path, "includes:\n  - llm.yaml\n",
                              llm="llm_system: {}\n", stray="plugins:\n  x: ${STRAY_KEY}\n")

        assert status.referenced_keys(config) == {}

    def test_what_the_loader_does_not_expand_names_no_key(self, tmp_path):
        """${lower} stays as written in the loader (settings._ENV_PLACEHOLDER): no key is asked of it."""
        config = write_config(tmp_path, "includes:\n  - llm.yaml\n",
                              llm="llm_system:\n  models:\n    fast:\n      api_key: ${not_a_key}\n")

        assert status.referenced_keys(config) == {}


class TestTheStateOfAKey:
    @pytest.mark.parametrize("value, state", [
        (None, "missing"), ("", "missing"), ("   ", "missing"),
        ("sk-or-v1-...", "placeholder"), ("tvly-...", "placeholder"),
        ("sk-or-v1-4f9a8c7e2b", "set"),
    ])
    def test_state(self, value, state):
        assert status.key_state(value) == state

    def test_the_template_sets_no_key(self):
        """A copied secrets.env.example must read as 'nothing set yet', whatever its values look like."""
        template = REPO / "config" / "secrets.env.example"
        values = [line.partition("=")[2].strip() for line in template.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.lstrip().startswith("#") and "=" in line]
        assert values, "the template has no entries -- this test would be vacuous"

        assert {status.key_state(value) for value in values} <= {"missing", "placeholder"}

    def test_the_status_carries_no_value(self, tmp_path, monkeypatch):
        config = write_config(tmp_path, "includes:\n  - llm.yaml\n",
                              llm="llm_system:\n  models:\n    fast:\n      api_key: ${FAST_KEY}\n")
        monkeypatch.setenv("FAST_KEY", "sk-or-v1-4f9a8c7e2b")

        keys = status.keys_status(config)

        assert keys == [{"name": "FAST_KEY", "state": "set", "named_in": ["llm_system.models.fast"]}]
        assert "4f9a8c7e2b" not in repr(keys)


#: A user database that is not there, set per test to a folder of its own: the signing tests look at no
#: accounts, least of all this machine's -- and whatever a mutant creates there is gone with the test.
NO_USERS = Path()


@pytest.fixture(autouse=True)
def _no_users_here(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "NO_USERS", tmp_path / "absent" / "users.db")


def auth(secret_key=None, username="admin", password="admin123", enabled=True, database_path=None):
    return SimpleNamespace(auth=SimpleNamespace(secret_key=secret_key, default_admin_username=username,
                                                default_admin_password=password, enabled=enabled,
                                                database_path=str(database_path or NO_USERS)))


class TestTheSigningKey:
    @pytest.mark.parametrize("key", [*status.SHIPPED_SIGNING_KEYS, "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION", "", None])
    def test_the_shipped_keys_the_models_default_and_none_are_known(self, key):
        """An unset ${AUTH_SECRET_KEY} expands to "": the API signs with it, and so can anyone."""
        assert status.auth_status(auth(secret_key=key))["shared_signing_key"] is True

    def test_an_own_key_is_not_shared_even_when_the_master_names_it(self):
        """The master file on disk is where one writes one's own key today; what counts is whether it is known."""
        assert status.auth_status(auth(secret_key="generated-for-this-installation"))["shared_signing_key"] is False

    def test_the_api_signs_with_its_start_key_until_a_restart(self):
        """set_jwt_config runs once, at start: a key reloaded into the config since signs nothing yet."""
        result = status.auth_status(auth(secret_key="generated-for-this-installation"),
                                    signing_key=status.SHIPPED_SIGNING_KEYS[0])

        assert (result["shared_signing_key"], result["signing_key_needs_restart"]) == (True, True), result

    def test_a_known_key_a_restart_would_apply_is_to_fix_now(self):
        """A pull and a reload brought the shipped key back: the running one is the installation's own until the
        restart that makes every login forgeable."""
        own = "generated-for-this-installation"
        result = status.auth_status(auth(secret_key=own), signing_key=own,
                                    reloaded=auth(secret_key=status.SHIPPED_SIGNING_KEYS[0]))

        assert (result["shared_signing_key"], result["signing_key_needs_restart"]) == (True, True), result

    def test_without_the_key_it_signs_with_a_restart_cannot_be_told(self):
        result = status.auth_status(auth(secret_key="generated-for-this-installation"))

        assert (result["shared_signing_key"], result["signing_key_needs_restart"]) == (False, None), result


class TestTheAdminsPassword:
    @pytest.fixture
    def users(self, tmp_path, monkeypatch):
        """The configured user database, set up as the API sets it up."""
        from agent_system.auth import database
        db = database.UserDatabase(tmp_path / "users.db")
        monkeypatch.setattr(database, "_db", db)
        return db

    @staticmethod
    def add(users, username, password, admin=True, active=True):
        from agent_system.auth.models import UserCreate, UserRole
        users.create_user(UserCreate(username=username, email=f"{username}@example.com", password=password,
                                     role=UserRole.ADMIN if admin else UserRole.USER, is_active=active))

    @staticmethod
    def status_of(users, **fields):
        return status.auth_status(auth(database_path=users.db_path, **fields))

    def test_the_default_password_is_seen(self, users):
        self.add(users, "admin", "admin123")

        assert self.status_of(users)["default_admin_password"] is True

    def test_a_default_changed_in_the_config_after_the_first_start_still_finds_the_shipped_one(self, users):
        """The admin is created once, with the default of that day: the config's later value is not its password."""
        self.add(users, "admin", "admin123")

        result = self.status_of(users, password="a-new-default-nobody-used")

        assert (result["admin"], result["default_admin_password"]) == ("admin", True), result

    def test_a_renamed_default_admin_does_not_hide_the_shipped_one(self, users):
        self.add(users, "admin", "admin123")

        result = self.status_of(users, username="root", password="whatever-configured")

        assert (result["admin"], result["default_admin_password"]) == ("admin", True), result

    def test_a_changed_password_is_seen(self, users):
        self.add(users, "admin", "a-long-own-password")

        assert self.status_of(users)["default_admin_password"] is False

    @pytest.mark.parametrize("account", ["a plain user of that name", "a deactivated admin", "no such account"])
    def test_what_cannot_log_in_as_an_admin_leaves_none_open(self, users, account):
        """Asked and answered: nothing opens -- not "cannot be checked", which the panel shows as Unknown."""
        if account != "no such account":
            self.add(users, "admin", "admin123", admin=account == "a deactivated admin",
                     active=account != "a deactivated admin")

        assert self.status_of(users)["default_admin_password"] is False

    def test_without_authentication_it_is_not_asked(self, users):
        self.add(users, "admin", "admin123")

        assert self.status_of(users, enabled=False)["default_admin_password"] is None

    def test_a_process_that_set_up_none_reads_the_configured_one_and_writes_nothing(self, users, tmp_path, monkeypatch):
        """agent-cli sets up no user database; a plugin's get_db() may open one at the default path meanwhile.
        The configured file is the one that answers -- read, not set up."""
        import sqlite3

        from agent_system.auth import database
        self.add(users, "admin", "admin123")
        stray = database.UserDatabase(tmp_path / "elsewhere" / "users.db")  # get_db()'s default, not configured
        self.add(stray, "admin", "a-long-own-password")
        monkeypatch.setattr(database, "_db", stray)
        # a database from before user_preferences: setting it up (UserDatabase.__init__) would add the table
        connection = sqlite3.connect(users.db_path)
        connection.execute("DROP TABLE user_preferences")
        connection.commit()
        connection.close()
        before = users.db_path.read_bytes()

        assert self.status_of(users)["default_admin_password"] is True
        assert database._db is stray and users.db_path.read_bytes() == before

    def test_the_reader_creates_no_file_where_the_database_went(self, tmp_path):
        """Checked for, then gone before it is read: a read-only open fails instead of leaving an empty users.db."""
        import sqlite3
        gone = tmp_path / "users.db"

        with pytest.raises(sqlite3.OperationalError):
            status.ReadOnlyUsers(gone).get_user_by_username("admin")

        assert not gone.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="a UNC path is a Windows path")
    def test_the_reader_reads_a_database_on_a_share(self, users):
        """A data folder on a share: Path.as_uri() made \\\\server\\share a URI authority, which sqlite refuses."""
        self.add(users, "admin", "admin123")
        resolved = users.db_path.resolve()
        share = Path(f"\\\\localhost\\{resolved.drive[0]}$") / resolved.relative_to(resolved.anchor)
        if not share.is_file():
            pytest.skip("the administrative share is not reachable here")

        assert status.ReadOnlyUsers(share).get_user_by_username("admin") is not None

    def test_without_a_user_database_it_cannot_tell_and_opens_none(self, tmp_path, monkeypatch):
        from agent_system.auth import database
        monkeypatch.setattr(database, "_db", None)

        assert status.auth_status(auth(database_path=tmp_path / "users.db"))["default_admin_password"] is None
        assert database._db is None, "the status opened a user database of its own"
        assert not list(tmp_path.rglob("*.db")), "the status created a user database"
