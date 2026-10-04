"""The admin an installation starts with, given a password of its own before the API first runs.

On a start that finds no user the API creates auth.default_admin_username with auth.default_admin_password, or
with a generated one it shows on the console once. config/config.yaml shipped admin123 there until 10/2026: an
admin created then still opens with it, for everyone on the network. The install scripts run this before the
API starts. Only the password's hash reaches the user database; nothing is written in clear text.

    python -m agent_system.auth.first_admin [config.yaml]

Asked on a terminal (twice; Enter generates one), generated and shown once without one. A publicly known
password is refused. Ctrl+C (and Ctrl+D, except on Windows, where getpass takes it as a character) ends the run
with exit status 1, and the install scripts stop there rather than start the API on an admin that may still open
with admin123.
"""
from __future__ import annotations

import getpass
import secrets
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import TypeAdapter, ValidationError

from agent_system.auth.models import Password, UserCreate, UserRole, UserUpdate, validation_reasons

#: Passwords the repository has printed for the default admin: config/config.yaml shipped admin123 until
#: 10/2026, docs/multi_user_authentication.md's example config CHANGE_THIS_PASSWORD. The history keeps them
#: known for good. A password changed in the config after the first start changes nothing: the admin is
#: created once, while there are no users.
PUBLISHED_PASSWORDS = ("admin123", "CHANGE_THIS_PASSWORD")
SHIPPED_USERNAME = "admin"

_PASSWORD = TypeAdapter(Password)


def published_admin_logins(auth: Any) -> list[tuple[str, str]]:
    """Each published password under the shipped username and under the configured one: an older install
    may have created its default admin under another name, with the shipped password."""
    names = dict.fromkeys(name for name in (auth.default_admin_username, SHIPPED_USERNAME) if name)
    return [(name, password) for name in names for password in PUBLISHED_PASSWORDS]


def known_admin_logins(auth: Any) -> list[tuple[str, str]]:
    """The published logins, after the configured default admin's: that password stands in the config file
    in clear text."""
    logins = published_admin_logins(auth)
    if auth.default_admin_username and auth.default_admin_password:
        logins.insert(0, (auth.default_admin_username, auth.default_admin_password))
    return logins


def admins_opened_by(logins: list[tuple[str, str]], users: Any) -> list[str]:
    """The active admins one of *logins* opens, each once, in the order of *logins*. *users* answers
    ``get_user_by_username``; a database that cannot be read raises (sqlite3.Error)."""
    from agent_system.auth.security import verify_password
    opened: list[str] = []
    for username, password in logins:
        if username in opened:
            continue
        user = users.get_user_by_username(username)
        # a deactivated account cannot log in, whatever its password
        if user and user.is_active and user.role == UserRole.ADMIN and verify_password(password, user.hashed_password):
            opened.append(username)
    return opened


def password_problem(password: str, refused: frozenset[str] = frozenset()) -> Optional[str]:
    """Why *password* will not do, or None: what the user database refuses, and the *refused* ones."""
    if password in refused:
        return "that password is known: published, or in the config file in clear text"
    try:
        _PASSWORD.validate_python(password)
    except ValidationError as error:
        return "; ".join(e["msg"] for e in error.errors())  # never the input: pydantic's own text echoes it
    return None


def ensure_admin_password(cfg_path: Path, new_password: Callable[[str, frozenset[str]], str],
                          changed: Optional[list[str]] = None) -> str:
    """Give the admin a password of its own, unless it has one. *new_password* is asked for it, with the
    username and the known passwords it must not be, only where one is needed. Returns what was done;
    *changed*, if given, gets each account's name as soon as its new password is stored -- an abort or error
    at a later account leaves the earlier ones changed.

    No user yet: the admin is created as the API would create it, with the new password -- the API then finds a
    user and creates none. Each active admin a published password still opens gets a new one, which also
    revokes its API key (made while anyone could log in). Otherwise nothing changes: a run again after an
    update must not touch an admin, whose API key the worker services may hold -- not even one on the
    password the config names, which is the operator's own choice.
    """
    from agent_system.auth.database import UserDatabase
    from agent_system.config.settings import load_settings

    changed = [] if changed is None else changed
    auth = load_settings(str(cfg_path)).auth
    if not (auth and auth.enabled):
        return "authentication is off: there is no login to protect"
    refused = frozenset(password for _, password in known_admin_logins(auth))
    # the path as the API resolves it (app.py: setup_database, a UserDatabase of the same path)
    users = UserDatabase(Path(auth.database_path) if auth.database_path else None)
    if not users.list_users(limit=1):
        username = auth.default_admin_username
        users.create_user(UserCreate(
            username=username, email=auth.default_admin_email, password=new_password(username, refused),
            full_name="Administrator", role=UserRole.ADMIN, is_active=True,
        ))
        changed.append(username)
        return f"admin account {username!r} created with a password of its own"
    done = []
    for admin in admins_opened_by(published_admin_logins(auth), users):
        user = users.get_user_by_username(admin)
        users.update_user(user.id, UserUpdate(password=new_password(admin, refused)))
        changed.append(admin)
        revoked = (f"; its API key is revoked -- whatever used it needs a new one (agent-cli users "
                   f"generate-api-key {admin})" if user.api_key else "")
        done.append(f"admin account {admin!r} had a published password and has a new one{revoked}")
    return "\n".join(done) or "no admin opens with a published password: nothing to do"


def _on_a_terminal() -> bool:
    """Whether someone can type the password: stdin is a terminal. On Windows isatty() says yes for NUL too (a
    character device), and getpass then waits on the console for good -- an unattended run would hang there;
    only a console handle counts."""
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    import msvcrt
    mode = ctypes.c_ulong()
    return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(mode)))


def _typed(username: str, refused: frozenset[str]) -> str:
    """A password typed twice on the terminal, or "" for one to be generated. An abort (KeyboardInterrupt,
    EOFError) is main's to handle."""
    # flushed: the prompt goes to the terminal directly, this line through stdout's buffer
    print(f"Choose a password for the admin account {username!r} (at least 8 characters; Enter: generate one).",
          flush=True)
    while True:
        first = getpass.getpass("Password: ")
        if not first:
            return ""
        problem = password_problem(first, refused)
        if problem:
            print(f"Not usable: {problem}", file=sys.stderr)
            continue
        if getpass.getpass("Again: ") != first:
            print("The two entries differ -- once more.", file=sys.stderr)
            continue
        return first


def main(argv: list[str]) -> int:
    if len(argv) > 1 or (argv and argv[0].startswith("-")):
        print("usage: python -m agent_system.auth.first_admin [config.yaml]  "
              "(default: AGENT_CONFIG_PATH, else config/config.yaml)", file=sys.stderr)
        return 2
    from agent_system.paths import default_config_path, enter_project, user_path
    named = user_path(argv[0]) if argv else None  # typed where the person stands
    # Run from the project, as agent-api does: a relative auth.database_path ("data/users.db") is the
    # project's, not the one of wherever this was started -- there it made a second admin beside the API's.
    enter_project()
    cfg_path = named or default_config_path()
    if not cfg_path.is_file():
        print(f"no configuration at {cfg_path}", file=sys.stderr)
        return 1
    generated: dict[str, str] = {}
    changed: list[str] = []

    def new_password(username: str, refused: frozenset[str]) -> str:
        typed = _typed(username, refused) if _on_a_terminal() else ""
        if typed:
            return typed
        generated[username] = secrets.token_urlsafe(12)
        return generated[username]

    # With several admins to fix, an abort or error at one leaves the earlier ones changed: their names are
    # told, and a password generated for them is shown whatever follows -- else that account is locked out.
    status = 1
    try:
        print(ensure_admin_password(cfg_path, new_password, changed))
        status = 0
    except (KeyboardInterrupt, EOFError):
        print(f"\naborted: not every admin has a password of its own yet{_already(changed)}", file=sys.stderr)
    except ValidationError as error:
        print(f"admin password not set: {validation_reasons(error)}{_already(changed)}", file=sys.stderr)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"admin password not set: {error}{_already(changed)}", file=sys.stderr)
    finally:
        for username in changed:
            if username in generated:
                print(f"Admin login: {username} / {generated[username]}  -- write it down, it is stored nowhere "
                      "in clear text.")
    return status


def _already(changed: list[str]) -> str:
    return f"; changed already: {', '.join(changed)}" if changed else ""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
