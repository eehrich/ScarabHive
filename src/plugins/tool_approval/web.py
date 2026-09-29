"""The routes that answer a waiting approval.

``POST /plugins/<instance>/answer`` -- ``{"question_id", "decision", "reason"}``,
decision one of allow_once / allow_session / deny.
``GET /plugins/<instance>/pending`` -- the open questions the caller may answer.

Who may answer, and that only a person's sign-in counts (never an API key):
``agent_system.api.question_routes``, shared with every plugin that asks.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agent_system.api.question_routes import question_router
from agent_system.core.run_questions import Question

if TYPE_CHECKING:
    from .hooks import ToolApprovalPlugin


class AnswerBody(BaseModel):
    question_id: str = Field(min_length=1, max_length=64)
    decision: str
    reason: str = Field(default="", max_length=4000)


def build_router(plugin: "ToolApprovalPlugin") -> APIRouter:
    def take(question: Question, body: AnswerBody, answered_by: str) -> Dict[str, Any]:
        plugin.broker.answer(body.question_id, body.decision, body.reason, answered_by=answered_by)
        return {"status": "ok", "question_id": question.id, "decision": body.decision}

    return question_router(f"/plugins/{plugin.instance_name}", plugin.broker, plugin.answer_url,
                           AnswerBody, take)
