"""Who may see what a plugin keeps per session.

Plugins record by session id and keep no owner (the usage tracker, the
context engineer's stores, the summarizer's events), and their panels answered
anyone signed in about any session -- or all of them at once. A session is
held against its owner as the app knows it. Read-only views fail closed: a
session whose owner nobody knows is shown to nobody but an admin.

Known gap, left for the per-user separation of plugin data
(the data in the user's own path,
not a WHERE on an owner): the rows carry no owner, so an id another user's
DELETED session had, taken for a new session of one's own, brings that
user's old rows along.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request

from agent_system import app_state
from agent_system.auth.models import User, UserRole


def viewer(current_user: Optional[User]) -> str:
    """The user a request acts as: the signed-in user, else "anonymous" -- whose sessions it
    sees (the rule of /sessions) and whose runs, chats and appends it makes."""
    return current_user.username if current_user else "anonymous"


def sees_everything(request: Request, current_user: Optional[User]) -> bool:
    """An admin sees every session, and so does everyone while authentication is
    off (one person uses the instance)."""
    auth = getattr(getattr(request.app.state, "config", None), "auth", None)
    if auth is not None and not getattr(auth, "enabled", True):
        return True
    return getattr(current_user, "role", None) == UserRole.ADMIN


def require_everything(request: Request, current_user: Optional[User], what: str) -> None:
    """403 unless the request sees every session -- for what acts on all of them (clearing, pruning)."""
    if not sees_everything(request, current_user):
        raise HTTPException(status_code=403, detail=f"{what} is for admins.")


async def may_see_session(request: Request, current_user: Optional[User], session_id: Optional[str]) -> bool:
    """Whether the data of *session_id* may be shown; None asks for every
    session at once, which is an admin's (403 for anyone else).

    The owner as the app knows it: a run of this process that has the session
    names its user (its first turn is not on disk yet), otherwise the session
    store answers under the viewer. Another user's session is not shown -- the
    caller answers it as it answers one that does not exist. Without a session
    store in the process the answer is 503, not an empty page.
    """
    if sees_everything(request, current_user):
        return True
    if session_id is None:
        raise HTTPException(status_code=403,
                            detail="The data of every session is for admins -- pick one of your sessions.")
    user_id = viewer(current_user)
    from agent_system.services.background_job_manager import get_background_job_manager
    running = (await get_background_job_manager().active_sessions()).get(session_id)
    if running is not None and running.get("user_id") is not None:
        return running["user_id"] == user_id
    sessions = getattr(app_state.session_service, "session_manager", None)
    if sessions is None:
        raise HTTPException(status_code=503, detail="The session store is not available in this process.")
    return sessions.belongs_to(user_id, session_id)
