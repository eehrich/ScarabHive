"""The routes that answer a waiting approval.

``POST /plugins/<instance>/answer`` -- ``{"question_id", "choices", "text"}``:
``choices`` one decision (allow_once / allow_session / deny, as the question
offers them), ``text`` a reason, with Deny only.
``GET /plugins/<instance>/pending`` -- the open questions the caller may answer.

The body, who may answer, and that only a person's sign-in counts (never an
API key): ``agent_system.api.question_routes``, shared with every plugin that asks.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

from agent_system.api.question_routes import question_router

if TYPE_CHECKING:
    from .hooks import ToolApprovalPlugin


def build_router(plugin: "ToolApprovalPlugin") -> APIRouter:
    return question_router(f"/plugins/{plugin.instance_name}", plugin.broker, plugin.answer_url)
