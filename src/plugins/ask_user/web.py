"""The routes that answer the model's question.

``POST /plugins/<instance>/answer`` -- ``{"question_id", "choices", "text"}``:
``choices`` the options picked (by their text, as the question lists them),
``text`` what the person typed; one of the two at least.
``GET /plugins/<instance>/pending`` -- the open questions the caller may answer.

Who may answer, and that only a person's sign-in counts (never an API key):
``agent_system.api.question_routes``, shared with tool_approval.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agent_system.api.question_routes import question_router
from agent_system.core.run_questions import Question

from .questions import MAX_OPTIONS, MAX_TEXT_CHARS

if TYPE_CHECKING:
    from .server import AskUserServer


class AnswerBody(BaseModel):
    question_id: str = Field(min_length=1, max_length=64)
    choices: List[str] = Field(default_factory=list, max_length=MAX_OPTIONS)
    text: str = Field(default="", max_length=MAX_TEXT_CHARS * 2)


def build_router(server: "AskUserServer") -> APIRouter:
    def take(question: Question, body: AnswerBody, answered_by: str) -> Dict[str, Any]:
        server.broker.answer(body.question_id, body.choices, body.text, answered_by=answered_by)
        return {"status": "ok", "question_id": question.id}

    return question_router(f"/plugins/{server.name}", server.broker, server.answer_url, AnswerBody, take)
