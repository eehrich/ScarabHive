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
from agent_system.auth.security import PUBLISHED_SIGNING_KEYS
from agent_system.config.local_layer import signing_key_at_restart
from agent_system.config.settings import (_ENV_PLACEHOLDER, LOCAL_CONFIG, config_files, local_text,
                                          set_by_the_environment)
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


#: Every signing key the repository has printed: the list the start check refuses or reports
#: (agent_system.auth.security). test_every_key_the_repository_prints_is_known holds every literal
#: key a commit of this branch put into a file outside the tests against it.
SHIPPED_SIGNING_KEYS = PUBLISHED_SIGNING_KEYS


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
            # the local layer as the loader reads it: UTF-16 too (PowerShell 5.1's `>`)
            text = local_text(path) if path.name == LOCAL_CONFIG else path.read_text(encoding="utf-8")
            data = yaml_io.safe_load(text)
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
    """The state of every key the configuration names, sorted by name. Never the value. ``from_environment``: set by
    the real environment, which a secrets file -- the Setup panel's too -- cannot change."""
    return [{"name": name, "state": key_state(os.environ.get(name)), "named_in": sections,
             "from_environment": set_by_the_environment(name)}
            for name, sections in sorted(api_keys(config_path).items())]


def api_keys(config_path: Optional[str] = None) -> dict[str, list[str]]:
    """referenced_keys, less what only the auth section names: the signing key is no API key. It is made with the
    panel's own button, a restart applies it, and a short one entered as a key would stop the next start."""
    return {name: sections for name, sections in referenced_keys(config_path).items()
            if not all(section.split(".")[0] == "auth" for section in sections)}


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
    from agent_system.auth.first_admin import admins_opened_by, known_admin_logins
    users = user_database(auth) if auth.enabled else None
    if users is None:
        return None, None
    try:
        admins = admins_opened_by(known_admin_logins(auth), users)
    except sqlite3.Error as error:  # locked, no users table: cannot be told -- a bug stays loud
        logger.warning("setup status: the user database could not be read: %s", error)
        return None, None
    return (admins[0], True) if admins else (None, False)


def configured_signing_key(config: Any) -> Optional[str]:
    """The signing key a restart applies: ``auth.secret_key`` as the config files say it now.

    Read from the files, never from a config in memory: the API signs with its
    start key until a restart, whatever a reload does, and only the master and
    the local layer can set auth (local_layer.signing_key_at_restart). ``${VAR}``
    expands as it would in a process started now -- the secrets files as they
    read now included. The loaded key where the config came from no file; None
    where a file or the auth section would not load, as a restart would not either.
    """
    path = getattr(config, "source_path", None)
    if path is None:
        return config.auth.secret_key
    try:
        # a master or an auth section that is no mapping raises here, as it fails the loader
        return signing_key_at_restart(Path(path))
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
