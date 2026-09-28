"""The routes that answer a question a run put to a person (core.run_questions).

A plugin that asks mounts them under its own prefix (``question_router``):

``POST <prefix>/answer`` -- the answer; its body is the plugin's, with a
``question_id``, and the plugin checks it against the question (``take``).
``GET <prefix>/pending`` -- the open questions the caller may answer.

Who may answer: the user the run belongs to, or an admin -- the rule the app
applies to acting on a run (``_refuse_foreign_request``: stopping it, writing
into it). While authentication is off there is one user, and everyone is that
user. A question whose run names no user is an admin's.

Only a person's sign-in counts: an access token (Bearer or the chat's cookie),
never an API key. A key belongs to a program, and a program that answers the
questions put to a person makes the question pointless.

No ``from __future__ import annotations`` here: the answer route's body type
is the plugin's model, and FastAPI reads it from the annotation at definition.
"""
from typing import Any, Callable, Dict, Optional, Type

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from agent_system.auth.models import User, UserRole
from agent_system.core.run_questions import NOT_WAITING, AnswerRejected, Question, QuestionBroker

#: What ``take`` gets: the question, the posted body, and who answers ("anonymous" with auth off).
TakeAnswer = Callable[[Question, Any, str], Dict[str, Any]]


def auth_enabled(request: Request) -> bool:
    """Off only when the app's config says so; anything unclear counts as on."""
    auth = getattr(getattr(request.app.state, "config", None), "auth", None)
    return not (auth is not None and getattr(auth, "enabled", True) is False)


async def person(request: Request) -> Optional[User]:
    """The signed-in person answering, or None while authentication is off."""
    if not auth_enabled(request):
        return None
    from agent_system.auth.database import get_db
    from agent_system.auth.dependencies import bearer_scheme, get_token_user

    user = await get_token_user(request, await bearer_scheme(request), get_db())
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Inactive user")
    return user


def may_answer(enabled: bool, user: Optional[User], question: Question) -> bool:
    """Whether ``user`` may answer ``question`` (see the module docstring)."""
    if not enabled:
        return True
    if user is None:
        return False
    if getattr(user, "role", None) == UserRole.ADMIN:
        return True
    return question.owner is not None and user.username == question.owner


def question_router(prefix: str, broker: QuestionBroker, answer_url: str,
                    body_model: Type[BaseModel], take: TakeAnswer) -> APIRouter:
    """``/answer`` and ``/pending`` under ``prefix`` for the questions of
    ``broker``. ``body_model`` has a ``question_id``; ``take`` hands the
    question its answer (raising AnswerRejected when the body does not fit it)
    and returns what the route answers."""
    router = APIRouter(prefix=prefix)

    async def answer(request: Request, body: body_model) -> Dict[str, Any]:  # type: ignore[valid-type]
        """Hand a waiting question a person's answer."""
        user = await person(request)
        question = broker.get(body.question_id)  # type: ignore[attr-defined]
        if question is None:
            raise HTTPException(status_code=404, detail=NOT_WAITING)
        if not may_answer(auth_enabled(request), user, question):
            raise HTTPException(status_code=403, detail="Only the user whose run asks, or an admin, may answer.")
        try:
            return take(question, body, user.username if user is not None else "anonymous")
        except AnswerRejected as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None

    async def pending(request: Request,
                      session_id: Optional[str] = Query(default=None),
                      request_id: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        """The open questions the caller may answer; ``request_id`` also takes
        the questions of the runs under it."""
        user = await person(request)
        enabled = auth_enabled(request)
        questions = [
            {**q.to_public(), "answer_url": answer_url}
            for q in broker.pending()
            if may_answer(enabled, user, q)
            and (session_id is None or q.session_id == session_id)
            and (request_id is None or q.request_id == request_id or q.request_id.startswith(f"{request_id}_"))
        ]
        return {"count": len(questions), "questions": questions}

    router.add_api_route("/answer", answer, methods=["POST"])
    router.add_api_route("/pending", pending, methods=["GET"])
    return router
