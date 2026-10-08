"""For tests that sign a token for an account of the real data/users.db, the way login signs it.

A token names the account's password generation (``gen``); one without it counts as 0 and stops signing in once
that account's password has been changed -- which the setup asks for on a fresh install. It is signed with the key
the real config names, which the install replaces as well (signing_key).
"""
import functools
import sqlite3


def token_generation(user_id: int) -> int:
    """The account's password generation in data/users.db, read-only (a plain connect creates a missing file)."""
    try:
        with sqlite3.connect("file:data/users.db?mode=ro", uri=True) as db:
            row = db.execute("select generation from token_generations where user_id = ?", (user_id,)).fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):  # a store no server of this version has opened yet: no change counted
            return 0
        raise
    return row[0] if row else 0


def signing_key() -> str:
    """The key build_app() signs logins with: config/config.yaml's published one, or this installation's own where
    config/local.yaml names it -- install.sh and `python -m agent_system.config.local_layer signing-key` write it
    there, and the local layer wins. A token signed with the published key is refused (401) on such a machine."""
    from agent_system.paths import default_config_path

    return _signing_key(str(default_config_path()))


@functools.lru_cache(maxsize=None)
def _signing_key(config_path: str) -> str:
    from agent_system.config.settings import load_settings

    return load_settings(config_path).auth.secret_key
