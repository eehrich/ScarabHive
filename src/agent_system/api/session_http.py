"""A session store's failures as HTTP answers, for every route that reads or writes a stored session.

By the kind of failure, not by "anything went wrong": a corrupt record read as
"no such session" would send someone looking for a session id that is right
there in their list. One mapping, so the session routes and the chat commands
cannot drift apart on which failure is a 404 and which a 403.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


@contextmanager
def session_errors_as_http(session_id: str, *, failed_to: Optional[str] = None,
                           not_found: Optional[str] = None,
                           forbidden: str = "Access denied") -> Iterator[None]:
    """SessionNotFoundError as 404, SessionPermissionError as 403, an HTTPException as itself.

    ``failed_to`` ("load", "delete", ...) also answers anything else as a 500,
    logged with its traceback; without it, anything else passes through.
    """
    from agent_system.services.session_manager import SessionNotFoundError, SessionPermissionError

    try:
        yield
    except HTTPException:
        raise
    except SessionNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=not_found or f"Session {session_id} not found")
    except SessionPermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=forbidden)
    except Exception as e:
        if failed_to is None:
            raise
        logger.exception("Failed to %s session %s: %s", failed_to, session_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
