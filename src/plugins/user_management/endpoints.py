"""The Users panel and its API: the accounts of the auth database, administered by admins."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from agent_system.auth.database import get_db
from agent_system.auth.dependencies import bearer_scheme, get_token_user
from agent_system.auth.models import UserCreate, UserRole, UserUpdate
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

if TYPE_CHECKING:
    from agent_system.auth.database import UserDatabase
    from agent_system.auth.models import User, UserInDB

logger = logging.getLogger(__name__)


def public(user: "UserInDB") -> dict:
    """What the panel may see of an account: never the password hash or the API key."""
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at,
        "last_login": user.last_login,
        "has_api_key": bool(user.api_key),
    }


class UserManagementWebEndpoints:
    def __init__(self, plugin):
        self.plugin = plugin
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.plugin.name, schema=self.plugin.get_schema_data(), handler_class=self)

    async def render_panel(self, request: Request):
        return self.templates.TemplateResponse(
            request, "panel.html", {"plugin": self.plugin.name, "auth_enabled": self.plugin.auth_enabled})

    async def _admin(self, request: Request) -> tuple["User", "UserDatabase"]:
        """The requesting admin, checked against the database here, whatever the route rules in the config say.

        Token auth only (no API key): that path is the one that awaits nothing, so from this check to the write no
        other request runs in between -- which is what keeps the self-protection below sufficient.
        """
        if not self.plugin.auth_enabled:
            raise HTTPException(status_code=403, detail="User management needs authentication to be enabled")
        db = get_db()
        user = await get_token_user(request, await bearer_scheme(request), db)
        if not user.is_active or user.role != UserRole.ADMIN:
            raise HTTPException(status_code=403, detail="Admin privileges required")
        return user, db

    async def list_users(self, request: Request):
        me, db = await self._admin(request)
        return {"me": me.id, "users": [public(user) for user in db.list_users(limit=db.count_users())]}

    async def create_user(self, request: Request, user: UserCreate):
        admin, db = await self._admin(request)
        # A POST without a Content-Type is a simple cross-site request, and FastAPI reads its body as JSON anyway.
        if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
            raise HTTPException(status_code=415, detail="Send the account as JSON")
        try:
            created = db.create_user(user)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))
        logger.info("Admin %s created user %s", admin.username, created.username)
        return public(created)

    async def update_user(self, request: Request, user_id: int, update: UserUpdate):
        admin, db = await self._admin(request)
        # The requester is an active admin, so refusing to demote or deactivate oneself always leaves one.
        if user_id == admin.id and (update.role not in (None, UserRole.ADMIN) or update.is_active is False):
            raise HTTPException(status_code=409, detail="You cannot take your own admin role or deactivate yourself")
        try:
            updated = db.update_user(user_id, update)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))
        if updated is None:
            raise HTTPException(status_code=404, detail="User not found")
        logger.info("Admin %s updated user %s", admin.username, updated.username)
        return public(updated)

    async def delete_user(self, request: Request, user_id: int):
        admin, db = await self._admin(request)
        if user_id == admin.id:
            raise HTTPException(status_code=409, detail="You cannot delete your own account")
        if not db.delete_user(user_id):
            raise HTTPException(status_code=404, detail="User not found")
        logger.info("Admin %s deleted user %s", admin.username, user_id)
        return {"deleted": user_id}
