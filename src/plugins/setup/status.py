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
from agent_system.config.settings import _ENV_PLACEHOLDER, config_files, environment_at_restart, expand_env
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

#: Every signing key the repository has printed -- config.yaml's, the examples in the docs,
#: reviews and templates, the tests' -- found in its history on 28.09.2026. The history keeps
#: them known for good, whatever the files say today; the model's own default and an empty key
#: are too. test_every_key_the_repository_prints_is_known holds every literal key a commit of this
#: branch put into a file outside the tests against this list.
SHIPPED_SIGNING_KEYS = (
    "published-signing-key-replace-with-your-own-0000000000",
    "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION_USE_RANDOM_STRING",
    "your-secret-key-here-CHANGE-IN-PRODUCTION-min-32-chars",
    "your-secret-key-min-32-chars",
    "your-secret-here",
    "your-generated-secret",
    "YOUR_VERY_LONG_RANDOM_SECRET_KEY_HERE",
    "e4c8f2b9a7d3e1f5c6b8a2d9e7f1c3b5a8d2e6f9c1b4a7d3e8f2c5b9a1d6e3f7",
    "generate-secure-random-key",
    "generated-secure-key",
    "test-secret-key",
    "test-secret-key-12345",
    "test-secret-key-for-jwt",
    "test-secret-key-do-not-use-in-production",
    "test-key-min-32-chars-long-secure",
    "secure-secret-key-32chars!",
    "not-the-secret-" * 4,
    "your-secure-key-here-min-32-chars",  # user_management's auth_disabled.html offered it to paste, for months
    "test-only-secret-not-the-config-one",
    "test-only-secret-not-the-config-one-0123456789",
    "jwt-signing-key-42",
    "generated-for-this-installation",
    "own-key-of-this-installation-0123456789",
    "reloaded-own-key-0123456789abcdef",
)


def referenced_keys(config_path: Optional[str] = None) -> dict[str, list[str]]:
    """Every ``${VAR}`` the loaded config files name, with the sections naming it.

    Read from the files the loader reads, parsed -- a commented-out line names
    nothing -- and matched as the loader expands them; a ``*_env`` entry whose
    value is a variable's name names that one. A section is the first
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
            if str(key).endswith("_env") and isinstance(value, str):
                # the variable a plugin reads itself (forge's token_env: FORGE_GITLAB_TOKEN); matched below as the
                # loader matches a placeholder, so a value that is no variable's name names nothing
                value = f"${{{value}}}"
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


def auth_status(config: Any, signing_key: Optional[str] = None) -> dict[str, Any]:
    """Whether an admin still opens with a publicly known password, and whether tokens
    are signed with a known key -- anyone who has it forges a login.

    *config* is the configuration the process started with: whether logins are
    checked, against which user database and with which key, is settled at start.
    *signing_key* is the key it signs with, which only the API with authentication
    has; the key a restart applies is read from the config file
    (configured_signing_key). The admin's password is ``None`` without
    authentication, or where no user database is there or can be read; the
    signing key is judged all the same -- it counts once authentication is on.
    """
    auth = config.auth
    admin, known_password = _admin_with_a_known_password(auth)
    signing = _signing_key_state(configured_signing_key(config), signing_key)
    return {
        "admin": admin or auth.default_admin_username,
        "default_admin_password": known_password,
        "shared_signing_key": signing["shared"],
        "signing_key_needs_restart": signing["needs_restart"],
        "configured_signing_key_known": signing["configured_known"],
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


def configured_signing_key(config: Any) -> Optional[str]:
    """The signing key a restart applies: ``auth.secret_key`` as the config file says it now.

    Read from the file, never from a config in memory: the API signs with its
    start key until a restart, whatever a reload does, and only the master file
    can set auth (settings.master_data_dir). ``${VAR}`` expands as it would in a
    process started now -- secrets.env as it reads now included
    (settings.environment_at_restart). The loaded key where the config came from
    no file; None where the file or its auth section would not load, as a restart
    would not either.
    """
    from agent_system.config.models import AuthConfig
    path = getattr(config, "source_path", None)
    if path is None:
        return config.auth.secret_key
    try:
        master = yaml_io.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        # a master or an auth section that is no mapping raises here, as it fails the loader
        section = master.get("auth", {})
        return AuthConfig(**expand_env(section, environ=environment_at_restart(path))).secret_key
    except Exception as error:  # unreadable, broken YAML, an auth section that fails validation
        # Only the kind: a validation error quotes the value, which may be the key. The panel shows the state,
        # and the loader reports the error in full at the next start.
        logger.debug("setup status: the auth section of %s does not load (%s)", path, type(error).__name__)
        return None


def _signing_key_state(configured: Optional[str], signing_key: Optional[str]) -> dict[str, Optional[bool]]:
    """Whether a known key signs tokens, whether the config file names another key than the
    running one -- which only a restart applies -- and whether that one is known: a restart then
    breaks what runs, rather than fixes it. None where it cannot be told: the running key outside
    the API (the configured one is judged in its place), the configured one where the file does
    not load."""
    from agent_system.config.models import AuthConfig
    running = configured if signing_key is None else signing_key
    printed = {AuthConfig.model_fields["secret_key"].default, *SHIPPED_SIGNING_KEYS}

    def known(key: Optional[str]) -> Optional[bool]:
        # a blank key too: jose signs with it -- the API refuses to start with one, a restart does not fix it
        return None if key is None else (not key.strip() or key in printed)
    return {"shared": known(running),
            "needs_restart": None if signing_key is None or configured is None else running != configured,
            "configured_known": known(configured)}
