"""The routes that answer the model's question.

``POST /plugins/<instance>/answer`` -- ``{"question_id", "choices", "text"}``:
``choices`` the options picked (by their text, as the question lists them),
``text`` what the person typed; one of the two at least.
``GET /plugins/<instance>/pending`` -- the open questions the caller may answer.

The body, who may answer, and that only a person's sign-in counts (never an
API key): ``agent_system.api.question_routes``, shared with every plugin that asks.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

from agent_system.api.question_routes import question_router

if TYPE_CHECKING:
    from .server import AskUserServer


def build_router(server: "AskUserServer") -> APIRouter:
    return question_router(f"/plugins/{server.name}", server.broker, server.answer_url)
