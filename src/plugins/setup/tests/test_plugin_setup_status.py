"""The setup status: which keys the configuration names and what state they are in, the admin's
password and the signing key. Read through the files the loader reads, never a hand-built list."""
import json
import os
import re
import secrets
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
    def test_the_local_layer_is_read_as_the_loader_reads_it(self, tmp_path):
        """PowerShell 5.1's `>` writes it as UTF-16; the loader reads that, so the panel must list its keys."""
        config = write_config(tmp_path, "auth:\n  enabled: false\n")
        (tmp_path / "local.yaml").write_bytes("plugins:\n  servers:\n    x:\n      token: ${ONLY_LOCAL_KEY}\n"
                                              .encode("utf-16"))

        assert "ONLY_LOCAL_KEY" in status.api_keys(config)

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

    def test_an_env_entry_names_the_variable_its_plugin_reads(self, tmp_path):
        """forge reads token_env's variable itself: set or not, it is a key of this configuration. A list of
        variables handed on (coding_cli's pass_env) names no key."""
        config = write_config(
            tmp_path, "includes:\n  - plugins.yaml\n",
            plugins="plugins:\n  servers:\n    forge:\n      hosts:\n        lab:\n          token_env: FORGE_TOKEN\n"
                    "          webhook_secret_env: \"HOOK_SECRET\"\n          name_env: not a variable\n"
                    "    coder:\n      pass_env: [PATH]\n")

        assert status.referenced_keys(config) == {"FORGE_TOKEN": ["plugins.servers.forge"],
                                                  "HOOK_SECRET": ["plugins.servers.forge"]}

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
        """A copied secrets.env.example must read as 'nothing set yet', whatever its values look like --
        its entries are commented out, and one uncommented without a real value must not count as set."""
        template = REPO / "config" / "secrets.env.example"
        entry = re.compile(r"#?\s*[A-Z][A-Z0-9_]*=(.*)")
        values = [m.group(1).strip() for line in template.read_text(encoding="utf-8").splitlines()
                  if (m := entry.fullmatch(line.strip()))]
        assert values, "the template has no entries -- this test would be vacuous"

        assert {status.key_state(value) for value in values} <= {"missing", "placeholder"}

    def test_the_status_carries_no_value(self, tmp_path, monkeypatch):
        config = write_config(tmp_path, "includes:\n  - llm.yaml\n",
                              llm="llm_system:\n  models:\n    fast:\n      api_key: ${FAST_KEY}\n")
        monkeypatch.setenv("FAST_KEY", "sk-or-v1-4f9a8c7e2b")

        keys = status.keys_status(config)

        assert keys == [{"name": "FAST_KEY", "state": "set", "named_in": ["llm_system.models.fast"],
                         "from_environment": True}]
        assert "4f9a8c7e2b" not in repr(keys)


#: A user database that is not there, set per test to a folder of its own: the signing tests look at no
#: accounts, least of all this machine's -- and whatever a mutant creates there is gone with the test.
NO_USERS = Path()


@pytest.fixture(autouse=True)
def _no_users_here(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "NO_USERS", tmp_path / "absent" / "users.db")


def auth(secret_key=None, username="admin", password="admin123", enabled=True, database_path=None, source_path=None):
    return SimpleNamespace(auth=SimpleNamespace(secret_key=secret_key, default_admin_username=username,
                                                default_admin_password=password, enabled=enabled,
                                                database_path=str(database_path or NO_USERS)),
                           source_path=source_path)


def written(folder: Path, key: str, started_with=None):
    """A config started with *started_with* (else *key*), loaded from a master file that names *key* now."""
    master = folder / "config.yaml"
    master.write_text(f"auth:\n  secret_key: {json.dumps(key)}\n", encoding="utf-8")
    return auth(secret_key=key if started_with is None else started_with, source_path=str(master))


#: A key of this installation's own: made now, printed nowhere.
OWN_KEY = secrets.token_urlsafe(32)
#: Other keys of no one's: made now as well, so the repository prints no key it does not list as known.
WRITTEN_KEY, ENVIRONMENT_KEY, START_KEY = (secrets.token_urlsafe(24) for _ in range(3))


class TestTheSigningKey:
    @staticmethod
    def verdict(result):
        return result["shared_signing_key"], result["signing_key_needs_restart"], result["configured_signing_key_known"]

    @pytest.mark.parametrize("key", [*status.SHIPPED_SIGNING_KEYS, "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION", "", "   "])
    def test_the_printed_keys_the_models_default_and_an_empty_one_are_known(self, key, tmp_path):
        """An unset ${AUTH_SECRET_KEY} expands to "": jose signs with it, and the API refuses to start with it -- a
        restart does not fix it either."""
        assert self.verdict(status.auth_status(written(tmp_path, key))) == (True, None, True)

    def test_an_own_key_is_not_shared_even_when_the_master_names_it(self, tmp_path):
        """The master file on disk is where one writes one's own key today; what counts is whether it is known."""
        assert self.verdict(status.auth_status(written(tmp_path, OWN_KEY))) == (False, None, False)

    def test_the_api_signs_with_its_start_key_until_a_restart(self, tmp_path):
        """set_jwt_config runs once, at start: an own key written into the file since signs nothing yet."""
        result = status.auth_status(written(tmp_path, OWN_KEY), signing_key=status.SHIPPED_SIGNING_KEYS[0])

        assert self.verdict(result) == (True, True, False), result

    def test_a_known_key_a_restart_would_apply_is_told_apart(self, tmp_path):
        """A pull brought the shipped key back: what runs is the installation's own, and the restart is what makes
        every login forgeable -- not the remedy. Seen in the file, whatever a reload did."""
        result = status.auth_status(written(tmp_path, status.SHIPPED_SIGNING_KEYS[0], started_with=OWN_KEY),
                                    signing_key=OWN_KEY)

        assert self.verdict(result) == (False, True, True), result

    def test_its_own_key_running_and_on_disk_is_all_well(self, tmp_path):
        result = status.auth_status(written(tmp_path, OWN_KEY), signing_key=OWN_KEY)

        assert self.verdict(result) == (False, False, False), result

    @pytest.mark.parametrize("master", ["auth: [not, a, section\n", "auth: nope\n", "auth:\n", "- a\n- list\n"],
                             ids=["broken", "no-mapping", "null", "a-list"])
    @pytest.mark.parametrize("running, shared", [(OWN_KEY, False), (status.SHIPPED_SIGNING_KEYS[0], True)],
                             ids=["own", "shipped"])
    def test_a_file_that_does_not_load_tells_nothing_of_the_restart(self, tmp_path, master, running, shared):
        """A restart would not start with it either (load_settings raises on each): what signs after it cannot be
        told, what signs now can."""
        config = written(tmp_path, OWN_KEY)
        Path(config.source_path).write_text(master, encoding="utf-8")

        assert self.verdict(status.auth_status(config, signing_key=running)) == (shared, None, None)

    @pytest.mark.parametrize("master", [
        f'auth:\n  secret_key: "{WRITTEN_KEY}"\n',
        'auth:\n  enabled: true\n',
        'paths: {}\n',
        '',
        'auth:\n  secret_key: "${SETUP_TEST_SIGNING_KEY}"\n',
        'auth:\n  secret_key: "${SETUP_TEST_UNSET_KEY}"\n',
    ], ids=["literal", "no-key", "no-auth", "empty", "variable", "unset-variable"])
    def test_the_key_read_is_the_one_the_loader_loads_from_the_file_now(self, tmp_path, monkeypatch, master):
        """What a restart applies is what load_settings makes of the file as it is now -- defaults and ${VAR}
        included -- not the key the process started with."""
        from agent_system.config.settings import load_settings
        monkeypatch.setenv("SETUP_TEST_SIGNING_KEY", ENVIRONMENT_KEY)
        monkeypatch.delenv("SETUP_TEST_UNSET_KEY", raising=False)
        started = load_settings(write_config(tmp_path, f'auth:\n  secret_key: "{OWN_KEY}"\n'))
        write_config(tmp_path, master)

        assert status.configured_signing_key(started) == load_settings(started.source_path).auth.secret_key

    @pytest.fixture
    def secrets_env(self, tmp_path, monkeypatch):
        """A config naming ${SETUP_TEST_FILE_KEY}, and a secrets.env beside it: as the loader reads them, in a
        process of this test's own -- what it took from the file, and the variable, go with the test."""
        from agent_system.config import environment, settings
        monkeypatch.setattr(environment, "_secrets_from_file", {})
        # set by the loader below; as they were again after the test
        monkeypatch.setenv(settings.SECRETS_FROM_FILE_ENV, os.environ.get(settings.SECRETS_FROM_FILE_ENV, ""))
        monkeypatch.setenv("SETUP_TEST_FILE_KEY", "")
        monkeypatch.delenv("SETUP_TEST_FILE_KEY")
        write_config(tmp_path, 'auth:\n  secret_key: "${SETUP_TEST_FILE_KEY}"\n')
        return tmp_path / "secrets.env"

    def test_a_key_written_into_secrets_env_since_the_start_is_the_one_a_restart_applies(self, secrets_env):
        """The file is read once per process: the key the process has is "", a restart takes the file's."""
        from agent_system.config.settings import load_settings
        started = load_settings(str(secrets_env.parent / "config.yaml"))
        secrets_env.write_text(f"SETUP_TEST_FILE_KEY={OWN_KEY}\n", encoding="utf-8")

        assert (started.auth.secret_key, status.configured_signing_key(started)) == ("", OWN_KEY)

    def test_a_key_the_process_took_from_secrets_env_is_read_there_again(self, secrets_env):
        """A pull changed the tracked file to a shipped key: this process still has the old one, a restart the new."""
        from agent_system.config.settings import load_settings
        secrets_env.write_text(f"SETUP_TEST_FILE_KEY={OWN_KEY}\n", encoding="utf-8")
        started = load_settings(str(secrets_env.parent / "config.yaml"))
        secrets_env.write_text(f'SETUP_TEST_FILE_KEY="{status.SHIPPED_SIGNING_KEYS[0]}"\n', encoding="utf-8")

        assert started.auth.secret_key == OWN_KEY, "the loader did not take the key from the file"
        assert status.configured_signing_key(started) == status.SHIPPED_SIGNING_KEYS[0]

    def test_the_real_environment_still_wins_over_secrets_env(self, secrets_env, monkeypatch):
        from agent_system.config.settings import load_settings
        monkeypatch.setenv("SETUP_TEST_FILE_KEY", OWN_KEY)
        started = load_settings(str(secrets_env.parent / "config.yaml"))
        secrets_env.write_text(f"SETUP_TEST_FILE_KEY={status.SHIPPED_SIGNING_KEYS[0]}\n", encoding="utf-8")

        assert status.configured_signing_key(started) == OWN_KEY

    def test_a_process_this_one_starts_knows_what_came_from_the_file(self, secrets_env):
        """A woken run gets the API's environment, secrets.env's values in it (session_presence.spawn_wake): no real
        environment there either -- a restart takes the file's."""
        import subprocess
        from agent_system.config.settings import load_settings
        master = str(secrets_env.parent / "config.yaml")
        secrets_env.write_text(f"SETUP_TEST_FILE_KEY={OWN_KEY}\n", encoding="utf-8")
        load_settings(master)  # the key taken from the file, as the API takes it
        secrets_env.write_text(f"SETUP_TEST_FILE_KEY={status.SHIPPED_SIGNING_KEYS[0]}\n", encoding="utf-8")
        script = ("import sys\nfrom agent_system.config.settings import load_settings\n"
                  "from plugins.setup.status import configured_signing_key\n"
                  "print(configured_signing_key(load_settings(sys.argv[1])))\n")

        child = subprocess.run([sys.executable, "-c", script, master], capture_output=True, text=True, timeout=90,
                               env={**os.environ, "PYTHONPATH": str(REPO / "src")}, cwd=REPO)

        assert child.returncode == 0 and child.stdout.strip(), child.stderr[-800:]
        assert child.stdout.strip().splitlines()[-1] == status.SHIPPED_SIGNING_KEYS[0], child.stdout[-300:]

    def test_a_name_set_again_to_another_value_is_real_environment(self, secrets_env, monkeypatch):
        """A starter that sets a name the file gave (a terminal's env_vars, `KEY=x agent-cli`) means that value: a
        start the same way gets it again, whatever the file says."""
        from agent_system.config.settings import load_settings
        secrets_env.write_text(f"SETUP_TEST_FILE_KEY={status.SHIPPED_SIGNING_KEYS[0]}\n", encoding="utf-8")
        started = load_settings(str(secrets_env.parent / "config.yaml"))
        monkeypatch.setenv("SETUP_TEST_FILE_KEY", OWN_KEY)

        assert status.configured_signing_key(started) == OWN_KEY

    @pytest.mark.skipif(os.name != "nt", reason="the environment is blind to case on Windows only")
    def test_a_name_the_file_writes_in_lower_case_is_the_same_variable(self, secrets_env):
        """On Windows os.environ takes `setup_test_file_key` for SETUP_TEST_FILE_KEY, at a start as well."""
        from agent_system.config.settings import load_settings
        secrets_env.write_text(f"setup_test_file_key={START_KEY}\n", encoding="utf-8")
        started = load_settings(str(secrets_env.parent / "config.yaml"))
        secrets_env.write_text(f"setup_test_file_key={OWN_KEY}\n", encoding="utf-8")

        assert started.auth.secret_key == START_KEY, "the loader did not take it for the variable"
        assert status.configured_signing_key(started) == OWN_KEY

    def test_a_secrets_file_that_cannot_be_read_is_gone_without(self, tmp_path, monkeypatch):
        """A start warns and goes on without it (_load_secrets_file): the key the master names is still told."""
        from agent_system.config import environment
        (tmp_path / "secrets.env").write_text("ANY=value\n", encoding="utf-8")

        def unreadable(path):
            raise PermissionError(13, "Permission denied", str(path))
        monkeypatch.setattr(environment, "_read_secrets_file", unreadable)

        assert status.configured_signing_key(written(tmp_path, OWN_KEY)) == OWN_KEY

    def test_a_key_that_does_not_load_is_not_logged(self, tmp_path, caplog):
        """A validation error quotes the value -- here the key itself -- and the panel asks on every refresh."""
        import logging
        config = written(tmp_path, OWN_KEY)
        Path(config.source_path).write_text("auth:\n  secret_key: 8237492384792384\n", encoding="utf-8")
        caplog.set_level(logging.DEBUG)

        assert status.configured_signing_key(config) is None
        assert caplog.records and "8237492384792384" not in caplog.text, caplog.text

    def test_a_config_from_no_file_is_judged_by_its_own_key(self):
        assert status.configured_signing_key(auth(secret_key=OWN_KEY)) == OWN_KEY

    def test_every_key_the_repository_prints_is_known(self):
        """A key a commit put into a file is known to whoever has the history, for good -- a doc's example, a
        template's snippet to paste (every file type, whatever it is now). Outside the tests: their own were listed
        once from the history, and a test may make up more. This branch's history only: a stash may hold this
        machine's secrets.env, and the working tree does."""
        import re
        import subprocess
        log = subprocess.run(["git", "log", "HEAD", "-p", "--no-color", "--no-ext-diff", "--format=",
                              "-G", "(secret_key|SECRET_KEY)", "--", ".", ":(exclude)tests/**", ":(exclude)**/tests/**"],
                             cwd=REPO, capture_output=True, check=True).stdout.decode("utf-8", "replace")
        added = "\n".join(line[1:] for line in log.splitlines() if line.startswith("+") and not line.startswith("+++"))
        # a value, not a placeholder (${VAR}, <generated_secret>) nor code (secret_key=config.auth.secret_key)
        quoted = re.compile(r"""\w*(?:secret_key|SECRET_KEY)["']?\s*[:=]\s*["']([^"'$<>{}\s]{8,})["']""")
        bare = re.compile(r"""^[\s>-]*(?:export\s+)?\w*(?:secret_key|SECRET_KEY)\s*(?::\s*|=)"""
                          r"""([^\s"'$<>{}\[\](),#`]{8,})\s*(?:#.*)?$""", re.M)
        assert bare.search("auth:\n  secret_key: an-unquoted-key-0123\n"), "the scan misses a key written bare"
        printed = {match.group(1) for pattern in (quoted, bare) for match in pattern.finditer(added)}
        printed = {key for key in printed if "..." not in key}  # an abbreviation in prose, no key
        assert len(printed) >= 10, f"found {printed}: the scan finds too little -- this test would be vacuous"

        unlisted = printed - set(status.SHIPPED_SIGNING_KEYS) - {"CHANGE_THIS_SECRET_KEY_IN_PRODUCTION"}
        assert not unlisted, f"printed in the repository, missing from SHIPPED_SIGNING_KEYS: {unlisted}"


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

    def test_an_admin_on_the_configured_password_is_flagged(self, users):
        """The config holds it in clear text: the panel says so, though the install scripts leave that admin alone."""
        self.add(users, "root", "MyPrivate-Secret-77")

        result = self.status_of(users, username="root", password="MyPrivate-Secret-77")

        assert (result["admin"], result["default_admin_password"]) == ("root", True), result

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
