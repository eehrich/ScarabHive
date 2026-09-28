"""What an installation still lacks: its keys, the admin's password, its own signing key.

Read-only. The panel and the setup agent act on it; nothing here writes.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from agent_system.auth import database
from agent_system.config.settings import _ENV_PLACEHOLDER, config_files
from agent_system.utils import yaml_io

logger = logging.getLogger(__name__)


class ReadOnlyUsers(database.UserDatabase):
    """A users.db read and never set up: UserDatabase's own __init__ writes its schema into
    the file, and its connection creates one where there is none."""

    def __init__(self, path: Path) -> None:  # no super().__init__: that writes
        self.db_path = path

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        # An empty authority: Path.as_uri() turns \\server\share into file://server/..., which sqlite refuses.
        posix = self.db_path.resolve().as_posix()
        uri = "file://" + urllib.parse.quote(posix if posix.startswith("/") else "/" + posix)
        connection = sqlite3.connect(f"{uri}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()


#: The admin account config/config.yaml ships. A password changed in the config after the
#: first start changes nothing: the admin is created once, while there are no users.
SHIPPED_ADMINS = (("admin", "admin123"),)

#: The signing keys config/config.yaml has shipped. The repository's history keeps them known
#: for good, whatever the file says today; the model's own default and an empty key are too.
SHIPPED_SIGNING_KEYS = ("published-signing-key-replace-with-your-own-0000000000",
                        "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION_USE_RANDOM_STRING")


def referenced_keys(config_path: Optional[str] = None) -> dict[str, list[str]]:
    """Every ``${VAR}`` the loaded config files name, with the sections naming it.

    Read from the files the loader reads, parsed -- a commented-out line names
    nothing -- and matched as the loader expands them. A section is the first
    three steps of the path to the value (``llm_system.models.openrouter-base``,
    ``plugins.servers.tavily_search``): where the variable is written, not every
    entry that inherits it through ``extends``.
    """
    found: dict[str, list[str]] = {}
    for path in config_files(config_path):
        try:
            data = yaml_io.safe_load(path.read_text(encoding="utf-8"))
        except Exception as error:  # the loader skips a broken file too
            logger.debug("setup status: %s not read: %s", path, error)
            continue
        _collect(data, (), found)
    return found


def _collect(node: Any, trail: tuple[str, ...], found: dict[str, list[str]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _collect(value, (*trail, str(key)), found)
    elif isinstance(node, list):
        for item in node:
            _collect(item, trail, found)
    elif isinstance(node, str):
        for name in _ENV_PLACEHOLDER.findall(node):
            sections = found.setdefault(name, [])
            section = ".".join(trail[:3])
            if section not in sections:
                sections.append(section)


def key_state(value: Optional[str]) -> str:
    """``missing``, ``placeholder`` or ``set``.

    A placeholder is what a copied template leaves (``sk-or-v1-...``): set, so
    the loader's "unset variable" warning never fires, and refused by the
    provider as an unknown user. No key contains three dots.
    """
    if not value or not value.strip():
        return "missing"
    if "..." in value:
        return "placeholder"
    return "set"


def keys_status(config_path: Optional[str] = None) -> list[dict[str, Any]]:
    """The state of every key the configuration names, sorted by name. Never the value."""
    return [{"name": name, "state": key_state(os.environ.get(name)), "named_in": sections}
            for name, sections in sorted(referenced_keys(config_path).items())]


def user_database(auth: Any) -> Any:
    """The user database *auth* names, or None where there is none yet.

    The API's own where it set that one up; else the file, opened to read only.
    Never get_db(), which creates a users.db of its own at the default path: a
    CLI process has none set up -- also the one the API starts to wake a web
    user's session (session_presence.wake_command) -- and without
    authentication a chat opens one at the default path, not the one configured.
    """
    from agent_system.paths import resolve_data_path
    path = resolve_data_path(auth.database_path)
    if database._db is not None and Path(database._db.db_path).resolve() == path.resolve():
        return database._db
    return ReadOnlyUsers(path) if path.is_file() else None


def auth_status(config: Any, signing_key: Optional[str] = None, reloaded: Any = None) -> dict[str, Any]:
    """Whether an admin still opens with a publicly known password, and whether tokens
    are signed with a known key -- anyone who has it forges a login.

    *config* is the configuration the process started with: whether logins are
    checked, against which user database and with which key, is settled at start.
    *signing_key* is the key it signs with, which only the API with authentication
    has; *reloaded* a configuration a reload set since, whose key a restart applies.
    The admin's password is ``None`` without authentication, or where no user
    database is there or can be read; the signing key is judged all the same --
    it counts once authentication is on.
    """
    auth = config.auth
    admin, known_password = _admin_with_a_known_password(auth)
    signing = _signing_key_state((reloaded or config).auth, signing_key)
    return {
        "admin": admin or auth.default_admin_username,
        "default_admin_password": known_password,
        "shared_signing_key": signing["shared"],
        "signing_key_needs_restart": signing["needs_restart"],
    }


def _admin_with_a_known_password(auth: Any) -> tuple[Optional[str], Optional[bool]]:
    """(the admin a known password opens, True), else (None, False); (None, None) where
    no user database can be asked."""
    from agent_system.auth.models import UserRole
    from agent_system.auth.security import verify_password
    users = user_database(auth) if auth.enabled else None
    if users is None:
        return None, None
    candidates = list(SHIPPED_ADMINS)
    if auth.default_admin_username and auth.default_admin_password:
        candidates.insert(0, (auth.default_admin_username, auth.default_admin_password))
    for username, password in candidates:
        try:
            user = users.get_user_by_username(username)
        except sqlite3.Error as error:  # locked, no users table: cannot be told -- a bug stays loud
            logger.warning("setup status: the user database could not be read: %s", error)
            return None, None
        # a deactivated account cannot log in, whatever its password
        if user and user.is_active and user.role == UserRole.ADMIN and verify_password(password, user.hashed_password):
            return username, True
    return None, False


def _signing_key_state(auth: Any, signing_key: Optional[str]) -> dict[str, Optional[bool]]:
    """Whether a known key signs tokens -- or will after a restart -- and whether the
    configuration names another key than the running one, which only a restart applies."""
    from agent_system.config.models import AuthConfig
    configured = auth.secret_key or ""
    running = configured if signing_key is None else signing_key
    # an empty key signs too: jose takes "" as a key
    known = {"", AuthConfig.model_fields["secret_key"].default, *SHIPPED_SIGNING_KEYS}
    return {"shared": running in known or configured in known,
            "needs_restart": None if signing_key is None else running != configured}
