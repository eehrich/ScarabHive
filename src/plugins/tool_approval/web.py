"""The routes that answer a waiting question.

``POST /plugins/<instance>/answer`` -- ``{"question_id", "decision", "reason"}``,
decision one of allow_once / allow_session / deny.
``GET /plugins/<instance>/pending`` -- the open questions the caller may answer.

Who may answer: the user the run belongs to, or an admin -- the rule the app
applies to acting on a run (``_refuse_foreign_request``: stopping it, writing
into it). While authentication is off there is one user, and everyone is that
user. A question whose run names no user is an admin's.

Only a person's sign-in counts: an access token (Bearer or the chat's cookie),
never an API key. A key belongs to a program, and a program that answers the
questions put to a person makes the question pointless.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agent_system.auth.models import User, UserRole

from .broker import AnswerRejected, Question

if TYPE_CHECKING:
    from .hooks import ToolApprovalPlugin


class AnswerBody(BaseModel):
    question_id: str = Field(min_length=1, max_length=64)
    decision: str
    reason: str = Field(default="", max_length=4000)


def _auth_enabled(request: Request) -> bool:
    """Off only when the app's config says so; anything unclear counts as on."""
    auth = getattr(getattr(request.app.state, "config", None), "auth", None)
    return not (auth is not None and getattr(auth, "enabled", True) is False)


async def _person(request: Request) -> Optional[User]:
    """The signed-in person answering, or None while authentication is off."""
    if not _auth_enabled(request):
        return None
    from agent_system.auth.database import get_db
    from agent_system.auth.dependencies import bearer_scheme, get_token_user

    user = await get_token_user(request, await bearer_scheme(request), get_db())
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Inactive user")
    return user


def may_answer(auth_enabled: bool, user: Optional[User], question: Question) -> bool:
    """Whether ``user`` may answer ``question`` (see the module docstring)."""
    if not auth_enabled:
        return True
    if user is None:
        return False
    if getattr(user, "role", None) == UserRole.ADMIN:
        return True
    return question.owner is not None and user.username == question.owner


def build_router(plugin: "ToolApprovalPlugin") -> APIRouter:
    router = APIRouter(prefix=f"/plugins/{plugin.instance_name}")
    broker = plugin.broker

    @router.post("/answer")
    async def answer(request: Request, body: AnswerBody) -> Dict[str, Any]:
        """Hand a waiting call a person's decision."""
        user = await _person(request)
        question = broker.get(body.question_id)
        if question is None:
            raise HTTPException(status_code=404, detail=(
                "No such question is waiting -- it was answered, timed out or its run ended."))
        if not may_answer(_auth_enabled(request), user, question):
            raise HTTPException(status_code=403, detail="Only the user whose run asks, or an admin, may answer.")
        try:
            broker.answer(body.question_id, body.decision, body.reason,
                          answered_by=user.username if user is not None else "anonymous")
        except AnswerRejected as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None
        return {"status": "ok", "question_id": question.id, "decision": body.decision}

    @router.get("/pending")
    async def pending(request: Request,
                      session_id: Optional[str] = Query(default=None),
                      request_id: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        """The open questions the caller may answer; ``request_id`` also takes
        the questions of the runs under it."""
        user = await _person(request)
        enabled = _auth_enabled(request)
        questions = [
            {**q.to_public(), "answer_url": plugin.answer_url}
            for q in broker.pending()
            if may_answer(enabled, user, q)
            and (session_id is None or q.session_id == session_id)
            and (request_id is None or q.request_id == request_id or q.request_id.startswith(f"{request_id}_"))
        ]
        return {"count": len(questions), "questions": questions}

    return router
