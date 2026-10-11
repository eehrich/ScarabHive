"""The Forge panel and its status route: the repositories, the policy and the webhook's work of every user.

Admins only, checked here whatever the route rules say: it lists every user's sessions that work on a ticket.
A user sees their own part with ``/forge`` (the tool's ``repo status``) -- one overview, ``server._overview``.
Without auth the app has one user, its owner.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from agent_system.auth.database import get_db
from agent_system.auth.dependencies import bearer_scheme, get_optional_user
from agent_system.auth.models import UserRole
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates


class ForgeWebEndpoints:
    def __init__(self, server: Any):
        self.server = server
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(),
                                    handler_class=self)

    async def _viewer(self, request: Request) -> Optional[str]:
        """The admin asking (their username: the sessions' user id), or None when the app runs without auth;
        anyone else is refused."""
        auth = getattr(getattr(request.app.state, "config", None), "auth", None)
        if auth is not None and not auth.enabled:
            return None
        user = await get_optional_user(request, await bearer_scheme(request), request.headers.get("X-API-Key"),
                                       get_db())
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if user.role != UserRole.ADMIN:
            raise HTTPException(status_code=403, detail="The forge panel lists every user's sessions: admins only")
        return user.username

    async def render_panel(self, request: Request):
        viewer = await self._viewer(request)
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name,
                                                                       "viewer": viewer or ""})

    async def api_status(self, request: Request) -> dict[str, Any]:
        await self._viewer(request)
        answer = await asyncio.to_thread(self.server._overview)
        answer["allowed_users"] = sorted(self.server.allowed_users)
        return answer
