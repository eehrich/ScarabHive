"""Where a user's sessions are: the rules that turn a user id and a session id into a path.

Its own module because more than the session manager names these files: its
index (``services/session_index.py``) and the session archive's walk over a
user directory do too, and a rule kept in three places is three rules the day
one of them changes. The id rules themselves are in ``core/session_ids.py``;
``SessionManager._sanitize_user_id`` and
``_validate_session_id`` stay as the names the rest of the system calls.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

# The pure rules live in core/ so the layers below services/ can use them;
# re-exported here under the names the session store has always imported.
from ..core.session_ids import (  # noqa: F401
    SESSION_ID_PATTERN,
    sanitize_user_id,
    user_dir_of,
    validate_session_id,
)


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
