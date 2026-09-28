"""For tests that sign a token for an account of the real data/users.db, the way login signs it.

A token names the account's password generation (``gen``); one without it counts as 0 and stops signing in once
that account's password has been changed -- which the setup asks for on a fresh install.
"""
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
