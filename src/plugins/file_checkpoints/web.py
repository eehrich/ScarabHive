"""The Files panel: ``GET /plugins/<instance>/`` -- what the agent changed in files, per checkpoint, as diffs.

The page only; what it shows comes from the chat's endpoints (``/chat/checkpoints``,
``/chat/checkpoints/diff``, ``/chat/rewind``), which check who owns the session.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from agent_system.ui.resources import ui_templates

if TYPE_CHECKING:
    from .hooks import FileCheckpointsPlugin

templates = ui_templates(Path(__file__).parent / "templates")


def build_router(plugin: "FileCheckpointsPlugin") -> APIRouter:
    router = APIRouter()

    @router.get(f"/plugins/{plugin.instance_name}/")
    async def panel(request: Request):
        return templates.TemplateResponse(request, "panel.html", {"plugin": plugin.instance_name})

    return router
