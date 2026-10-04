"""The install scripts give the admin a password of its own before the API first runs (agent_system.auth.first_admin).

Driven through a real config file, load_settings and the user database the API opens. The API's own first start is
driven once (build_app), for the password it generates; for the rest the tests hold its precondition instead (a
user exists, so it creates no default admin).
"""
from __future__ import annotations

import importlib.util
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent_system.auth import first_admin
from agent_system.auth.database import UserDatabase
from agent_system.auth.models import UserCreate, UserRole
from agent_system.auth.security import verify_password

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    """main() enters the project, as agent-api does. Here the project is a directory of this test: a default
    config is looked for in it, and with data paths relative in tests (conftest) no regression reaches data/."""
    import agent_system.paths as paths
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)
    monkeypatch.setattr(paths, "_launch_dir", None)  # enter_project remembers where the process began
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    return root


def write_config(root: Path, enabled: bool = True, username: str = "admin", password: str = "admin123") -> Path:
    path = root / "config.yaml"
    path.write_text(
        "auth:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        f"  database_path: \"{(root / 'users.db').as_posix()}\"\n"
        f"  default_admin_username: \"{username}\"\n"
        + (f"  default_admin_password: \"{password}\"\n" if password else "")
        + "  default_admin_email: \"admin@example.com\"\n",
        encoding="utf-8",
    )
    return path


def add_admin(root: Path, password: str, with_key: bool, username: str = "admin") -> UserDatabase:
    users = UserDatabase(root / "users.db")
    user = users.create_user(UserCreate(username=username, email=f"{username}@example.com", password=password,
                                        role=UserRole.ADMIN, is_active=True))
    if with_key:
        users.generate_user_api_key(user.id)
    return users


def never(username: str, refused: frozenset[str]) -> str:
    raise AssertionError(f"asked for a password for {username!r} although none is needed")


def test_a_fresh_install_creates_the_admin_with_the_new_password(tmp_path):
    cfg = write_config(tmp_path, password="MyPrivate-Secret-77")

    asked: list[frozenset[str]] = []
    done = first_admin.ensure_admin_password(cfg, lambda username, refused: asked.append(refused) or "a-new-password-1")

    users = UserDatabase(tmp_path / "users.db")
    admin = users.get_user_by_username("admin")
    assert "created" in done, done
    assert admin.role == UserRole.ADMIN and admin.is_active
    assert verify_password("a-new-password-1", admin.hashed_password)
    assert not verify_password("admin123", admin.hashed_password)
    # the API's first start creates its default admin only while list_users finds nobody (app.py)
    assert users.list_users(limit=1)
    # the published ones, and the configured one: it stands in the config file in clear text
    assert asked and {"admin123", "CHANGE_THIS_PASSWORD", "MyPrivate-Secret-77"} <= asked[0], asked


def test_an_admin_on_the_shipped_password_gets_the_new_one_and_loses_its_key(tmp_path):
    cfg = write_config(tmp_path)
    add_admin(tmp_path, "admin123", with_key=True)

    done = first_admin.ensure_admin_password(cfg, lambda username, refused: "a-new-password-1")

    admin = UserDatabase(tmp_path / "users.db").get_user_by_username("admin")
    assert verify_password("a-new-password-1", admin.hashed_password)
    assert admin.api_key is None
    assert "revoked" in done, done


def test_an_admin_with_its_own_password_keeps_it_and_its_api_key(tmp_path):
    """A run again after an update must not touch it: the worker services may hold its API key, and a new
    password revokes that key (server, 04.10.2026: every worker token stood at 401)."""
    cfg = write_config(tmp_path)
    before = add_admin(tmp_path, "an-own-password-9", with_key=True).get_user_by_username("admin")

    first_admin.ensure_admin_password(cfg, never)

    after = UserDatabase(tmp_path / "users.db").get_user_by_username("admin")
    assert (after.hashed_password, after.api_key) == (before.hashed_password, before.api_key)
    assert before.api_key, "fixture: the admin had no API key to lose"


def test_an_admin_on_the_password_its_config_names_keeps_it_and_its_api_key(tmp_path):
    """The old config said CHANGE THIS beside admin123; who did, has their own password there. It stands in the
    config in clear text, but it is the operator's choice: a run again must not revoke the key the workers hold."""
    cfg = write_config(tmp_path, password="MyPrivate-Secret-77")
    before = add_admin(tmp_path, "MyPrivate-Secret-77", with_key=True).get_user_by_username("admin")

    first_admin.ensure_admin_password(cfg, never)

    after = UserDatabase(tmp_path / "users.db").get_user_by_username("admin")
    assert (after.hashed_password, after.api_key) == (before.hashed_password, before.api_key)
    assert before.api_key, "fixture: the admin had no API key to lose"


def test_every_admin_on_a_published_password_gets_a_new_one_in_one_run(tmp_path):
    """The default admin created under another name with the shipped password -- the config names no password
    any more -- and the shipped name on the docs' example password: the scripts start the API after one run."""
    cfg = write_config(tmp_path, username="root", password="")
    add_admin(tmp_path, "admin123", with_key=False, username="root")
    add_admin(tmp_path, "CHANGE_THIS_PASSWORD", with_key=False, username="admin")

    asked: list[str] = []
    first_admin.ensure_admin_password(cfg, lambda username, refused: asked.append(username) or "a-new-password-1")

    users = UserDatabase(tmp_path / "users.db")
    for name in ("root", "admin"):
        assert verify_password("a-new-password-1", users.get_user_by_username(name).hashed_password), name
    assert sorted(asked) == ["admin", "root"], asked


def test_without_authentication_nothing_is_created(tmp_path):
    cfg = write_config(tmp_path, enabled=False)

    first_admin.ensure_admin_password(cfg, never)

    assert not (tmp_path / "users.db").exists()


def test_without_a_terminal_a_password_is_generated_and_shown_once(tmp_path):
    """An unattended run, stdin on the null device. On Windows isatty() says yes for NUL, and a prompt there
    waited on the console for good: the run has to finish on its own."""
    cfg = write_config(tmp_path)

    run = subprocess.run([sys.executable, "-m", "agent_system.auth.first_admin", str(cfg)], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=60, cwd=tmp_path)

    assert run.returncode == 0, run.stderr
    shown = re.findall(r"Admin login: admin / (\S+)", run.stdout)
    assert len(shown) == 1, run.stdout
    admin = UserDatabase(tmp_path / "users.db").get_user_by_username("admin")
    assert verify_password(shown[0], admin.hashed_password)


def test_a_typed_password_is_checked_and_confirmed(monkeypatch, capsys):
    typed = iter(["short", "admin123", "long-enough-1", "a-typo-here", "long-enough-1", "long-enough-1"])
    monkeypatch.setattr(first_admin.getpass, "getpass", lambda prompt="": next(typed))

    assert first_admin._typed("admin", frozenset({"admin123"})) == "long-enough-1"

    err = capsys.readouterr().err
    assert err.count("Not usable") == 2 and "known: published" in err and "differ" in err, err
    assert next(typed, None) is None, "not every entry was read"


def test_enter_on_the_prompt_leaves_it_to_a_generated_password(monkeypatch):
    monkeypatch.setattr(first_admin.getpass, "getpass", lambda prompt="": "")
    assert first_admin._typed("admin", frozenset()) == ""


@pytest.mark.parametrize("ends_it", [KeyboardInterrupt, EOFError])
def test_an_aborted_prompt_ends_the_run_without_an_admin(tmp_path, monkeypatch, capsys, ends_it):
    """Ctrl+C or Ctrl+D at the prompt: exit status 1, on which the install scripts stop rather than start the
    API on an admin that may still open with admin123."""
    cfg = write_config(tmp_path)
    monkeypatch.setattr(first_admin, "_on_a_terminal", lambda: True)

    def ended(prompt=""):
        raise ends_it
    monkeypatch.setattr(first_admin.getpass, "getpass", ended)

    try:
        status = first_admin.main([str(cfg)])
    except KeyboardInterrupt:  # escaping, it would end the whole pytest session instead of this test
        pytest.fail("the abort escaped main")
    assert status == 1

    assert "aborted" in capsys.readouterr().err
    assert not UserDatabase(tmp_path / "users.db").list_users(limit=1)


def test_an_abort_at_the_second_admin_still_shows_the_first_ones_new_password(tmp_path, monkeypatch, capsys):
    """Two admins to fix: the password generated for the first is stored, and the first's old login with it
    gone. Unshown, that account was locked out -- a run again no longer touches it."""
    cfg = write_config(tmp_path, username="root", password="")
    add_admin(tmp_path, "admin123", with_key=False, username="root")
    add_admin(tmp_path, "CHANGE_THIS_PASSWORD", with_key=False, username="admin")
    monkeypatch.setattr(first_admin, "_on_a_terminal", lambda: True)
    entries = iter([""])  # Enter at root's prompt: generate one; then Ctrl+C at admin's

    def typed(prompt=""):
        try:
            return next(entries)
        except StopIteration:
            raise KeyboardInterrupt from None
    monkeypatch.setattr(first_admin.getpass, "getpass", typed)

    try:
        status = first_admin.main([str(cfg)])
    except KeyboardInterrupt:
        pytest.fail("the abort escaped main")

    out, err = capsys.readouterr()
    shown = re.findall(r"Admin login: root / (\S+)", out)
    assert status == 1 and len(shown) == 1, (status, out)
    users = UserDatabase(tmp_path / "users.db")
    assert verify_password(shown[0], users.get_user_by_username("root").hashed_password)
    assert verify_password("CHANGE_THIS_PASSWORD", users.get_user_by_username("admin").hashed_password)
    assert "changed already: root" in err, err


def test_without_a_path_it_takes_the_config_the_api_takes(tmp_path, monkeypatch):
    """No argument: AGENT_CONFIG_PATH, as agent-api reads it -- else the admin landed in another users.db
    than the one the API opens, and the API created its own beside it."""
    monkeypatch.setenv("AGENT_CONFIG_PATH", str(write_config(tmp_path)))
    monkeypatch.setattr(first_admin, "_on_a_terminal", lambda: False)

    assert first_admin.main([]) == 0

    assert UserDatabase(tmp_path / "users.db").get_user_by_username("admin")


def test_the_default_config_is_the_projects_wherever_the_shell_stands(project):
    from agent_system.paths import default_config_path
    assert default_config_path() == project / "config" / "config.yaml"


def test_run_from_elsewhere_a_relative_users_db_is_the_projects(project, tmp_path, monkeypatch):
    """agent-api runs from the project, so its "data/users.db" is the project's. Run from another directory, the
    admin went into that directory's data/ -- beside an API that then created one of its own."""
    (project / "config" / "config.yaml").write_text(
        'auth:\n  enabled: true\n  database_path: "data/users.db"\n', encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(first_admin, "_on_a_terminal", lambda: False)

    assert first_admin.main([]) == 0

    assert UserDatabase(project / "data" / "users.db").get_user_by_username("admin")
    assert not (elsewhere / "data").exists()


def first_api_start(tmp_path: Path, auth_lines: str = "") -> str:
    """build_app on an empty user database, as the API's very first start; returns its log file."""
    from agent_system import app as app_mod
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        'name: "probe-hive"\nnetwork:\n  ssl_verify: true\n'
        "logging:\n  enabled: true\n  level: DEBUG\n  file_api: logs/api-probe.log\n  rotation_enabled: false\n"
        f"auth:\n  enabled: true\n  secret_key: \"{'7f' * 32}\"\n"
        f"  database_path: \"{(tmp_path / 'users.db').as_posix()}\"\n{auth_lines}",
        encoding="utf-8")
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        app_mod.build_app(str(cfg))
    finally:  # build_app rebinds the root logger, to this test's captured stream and file among others
        for handler in root.handlers[:]:
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(level)
    return (tmp_path / "logs" / "api-probe.log").read_text(encoding="utf-8")


def test_a_generated_first_password_reaches_the_console_and_not_the_log_file(tmp_path, capsys):
    """Without the install scripts the API's first start generates the password. Logged, it stayed readable in
    logs/api.log for as long as the file lived, and a log level above WARNING dropped it unseen."""
    log = first_api_start(tmp_path)

    shown = re.findall(r"Password: (\S+)", capsys.readouterr().err)
    assert len(shown) == 1, "the generated password was not shown"
    admin = UserDatabase(tmp_path / "users.db").get_user_by_username("admin")
    assert verify_password(shown[0], admin.hashed_password)
    assert "Default admin" in log, "fixture: the bootstrap's lines did not reach the log file"
    assert shown[0] not in log, "the password went into the log file"


def test_a_refused_configured_password_stays_out_of_the_log_file(tmp_path):
    """A configured password the user model refuses (too short) fails the first start's admin; pydantic's own
    text of that failure carries the input, and it was logged as it was."""
    log = first_api_start(tmp_path, '  default_admin_password: "Zq7-x9"\n')

    assert "Failed to create default admin user" in log, "fixture: the admin was not refused"
    assert "Zq7-x9" not in log, "the refused password went into the log file"


def test_the_shipped_configuration_names_no_admin_password():
    """A password in the shipped config is everyone's: without the install scripts the API's first start would
    create the admin with it. Without one, the API generates one and shows it on the console."""
    import yaml
    auth = yaml.safe_load((REPO / "config" / "config.yaml").read_text(encoding="utf-8"))["auth"]
    assert not auth.get("default_admin_password")


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_every_module_an_install_script_runs_is_there(script):
    """The scripts run their steps as `python -m <module>`: each one named there has to exist, and this one
    has to run (its usage on a wrong argument)."""
    modules = re.findall(r"-m\s+(agent_system\.[\w.]+)", (REPO / script).read_text(encoding="utf-8"))
    assert "agent_system.auth.first_admin" in modules, modules
    for module in modules:
        assert importlib.util.find_spec(module), f"{script} runs {module}, which does not exist"
    run = subprocess.run([sys.executable, "-m", "agent_system.auth.first_admin", "--help"],
                         capture_output=True, text=True, timeout=60, cwd=REPO)
    assert run.returncode == 2 and "usage" in run.stderr, (run.returncode, run.stderr)
