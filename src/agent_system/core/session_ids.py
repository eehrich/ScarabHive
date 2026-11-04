"""What a session id and a user id may be, and where a user's sessions are.

Pure rules, no state: the session store (``services/session_paths.py``,
``services/session_service.py``) re-exports them under their old names, and
the layers below it -- session presence, the agent's result helpers, the
agent's tool sessions -- import them from here instead of reaching up into
``services/``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

#: What a session id may be: letters, digits, underscores and hyphens -- it
#: becomes a file name (``<id>.json``, ``<id>.lock``, ``.subs.<id>.index.json``).
SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

#: A session under this prefix lives for one call, and SessionService never
#: writes it: a stateless run (``collect_final_result`` without a session id,
#: the stateless calls of the openai_api plugin) promises to leave nothing
#: behind, and the agent saves every session it runs at the end of each turn and
#: by checkpoint. What does reach the disk: the parent record the sub-agent
#: manager creates through SessionManager when such a run starts sub-agents
#: (``Coordinator Session``), with their sessions below it.
EPHEMERAL_SESSION_PREFIX = "ephemeral-"


def is_ephemeral_session(session_id: Optional[str]) -> bool:
    return bool(session_id) and str(session_id).startswith(EPHEMERAL_SESSION_PREFIX)


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
