"""The routes that answer a question a run put to a person (core.run_questions).

A plugin that asks mounts them under its own prefix (``question_router``):

``POST <prefix>/answer`` -- the answer in the form every client sends
(``AnswerBody``: the question, the ``value`` of each choice picked, the text
written); the question's kind checks it (``QuestionBroker.take``).
``GET <prefix>/pending`` -- the open questions the caller may answer, each
with its ``form`` and the ``answer_url`` to post to.

Mounted at ``/plugins/<instance>``: the web chat posts an answer to
``/plugins/<instance>/answer`` and to no other path (syncQuestionActions).

Who may answer: the user the run belongs to, or an admin -- the rule the app
applies to acting on a run (``_refuse_foreign_request``: stopping it, writing
into it). While authentication is off there is one user, and everyone is that
user. A question whose run names no user is an admin's.

Only a person's sign-in counts: an access token (Bearer or the chat's cookie),
never an API key. A key belongs to a program, and a program that answers the
questions put to a person makes the question pointless.

No ``from __future__ import annotations`` here: FastAPI reads the answer
route's body type from the annotation at definition.
"""
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agent_system.auth.models import User, UserRole
from agent_system.core.run_questions import NOT_WAITING, AnswerRejected, Question, QuestionBroker


class AnswerBody(BaseModel):
    """An answer as every client sends it. The bounds keep a request small; what fits
    the question is the kind's own check (``take``)."""

    question_id: str = Field(min_length=1, max_length=64)
    choices: List[Annotated[str, Field(max_length=1000)]] = Field(default_factory=list, max_length=64)
    text: str = Field(default="", max_length=100_000)


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


def question_router(prefix: str, broker: QuestionBroker, answer_url: str) -> APIRouter:
    """``/answer`` and ``/pending`` under ``prefix`` for the questions of
    ``broker``: the answer goes to the kind's ``take``, which refuses what does
    not fit the question (AnswerRejected)."""
    router = APIRouter(prefix=prefix)

    async def answer(request: Request, body: AnswerBody) -> Dict[str, Any]:
        """Hand a waiting question a person's answer."""
        user = await person(request)
        question = broker.get(body.question_id)
        if question is None:
            raise HTTPException(status_code=404, detail=NOT_WAITING)
        if not may_answer(auth_enabled(request), user, question):
            raise HTTPException(status_code=403, detail="Only the user whose run asks, or an admin, may answer.")
        try:
            broker.take(body.question_id, body.choices, body.text,
                        answered_by=user.username if user is not None else "anonymous")
        except AnswerRejected as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None
        return {"status": "ok", "question_id": question.id}

    async def pending(request: Request,
                      session_id: Optional[str] = Query(default=None),
                      request_id: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        """The open questions the caller may answer; ``request_id`` also takes
        the questions of the runs under it, ``session_id`` those asked in the
        sessions of the agents it called as tools (their own, below it), at
        every level below it (session_chain)."""
        from ..servers.agent.components.session_tracking import session_chain

        user = await person(request)
        enabled = auth_enabled(request)
        questions = [
            {**q.to_public(), "answer_url": answer_url}
            for q in broker.pending()
            if may_answer(enabled, user, q)
            and (session_id is None or session_id in session_chain(q.session_id or ""))
            and (request_id is None or q.request_id == request_id or q.request_id.startswith(f"{request_id}_"))
        ]
        return {"count": len(questions), "questions": questions}

    router.add_api_route("/answer", answer, methods=["POST"])
    router.add_api_route("/pending", pending, methods=["GET"])
    return router
