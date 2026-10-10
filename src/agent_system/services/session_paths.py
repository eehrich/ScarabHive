"""Where a user's sessions are: the rules that turn a user id and a session id into a path.

Its own module because more than the session manager names these files: its
index (``services/session_index.py``) and the session archive's walk over a
user directory do too, and a rule kept in three places is three rules the day
one of them changes. ``SessionManager._sanitize_user_id`` and
``_validate_session_id`` stay as the names the rest of the system calls.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Optional

#: What a session id may be: letters, digits, underscores and hyphens -- it
#: becomes a file name (``<id>.json``, ``<id>.lock``, ``.subs.<id>.index.json``).
SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def sanitize_user_id(user_id: str) -> str:
    """Sanitize user_id to prevent directory traversal.

    Args:
        user_id: Raw user identifier

    Returns:
        Sanitized user ID safe for filesystem use
    """
    return user_id.replace('..', '_').replace('/', '_').replace('\\', '_')


def validate_session_id(session_id: str) -> str:
    """Validate and sanitize session ID.

    Args:
        session_id: Raw session ID

    Returns:
        Sanitized session ID

    Raises:
        ValueError: If session ID contains invalid characters
    """
    # Session IDs should be alphanumeric with underscores/hyphens only
    if not SESSION_ID_PATTERN.match(session_id):
        raise ValueError(f"Invalid session ID format: {session_id!r}")
    return session_id


def user_dir_of(root: Path, user_id: str) -> Path:
    """A user's directory under ``root``: the session store, or the archive next to it."""
    return root / sanitize_user_id(user_id)


def session_files_anywhere(
    root: Path, session_id: str, *, skip_user: Optional[str] = None,
) -> Iterator[Path]:
    """The file of ``session_id`` in every user directory under ``root`` that has one.

    The scan behind every question about a session whose owner is not known
    yet -- is the id taken, whose is it, is it someone else's. Lazy, in
    directory order: a caller that stops at the first file looks at no
    further directory. ``skip_user`` leaves one directory out by its name.
    """
    for user_dir in root.iterdir():
        if not user_dir.is_dir() or user_dir.name == skip_user:
            continue
        session_file = user_dir / f"{session_id}.json"
        if session_file.exists():
            yield session_file
