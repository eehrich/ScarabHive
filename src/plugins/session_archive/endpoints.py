"""The Session Archive panel and its API.

The archive itself belongs to the app (``agent_system.services.session_archive``):
it is the app's sweep that writes it, and the ``SessionManager`` it deletes
through. This plugin is only its face -- every handler here resolves the
requesting user and then asks that service about THAT user's archive. There is
no path from here into someone else's conversations.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import User
from agent_system.plugins.schema_router import create_schema_router
from agent_system.services.session_archive import (
    ArchiveError,
    ArchiveNotFound,
    SessionArchive,
)
from agent_system.ui.resources import ui_templates

logger = logging.getLogger(__name__)


def _archive(request: Request) -> SessionArchive:
    """The app's archive service, or a straight answer that there is none."""
    service = getattr(request.app.state, "session_archive", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Session archive is not available")
    return service


def _user_id(current_user: Optional[User]) -> str:
    """Whose archive the request is about -- the same rule as /sessions."""
    return current_user.username if current_user else "anonymous"


class SessionArchiveWebEndpoints:
    def __init__(self, plugin):
        self.plugin = plugin
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(
            plugin_name=self.plugin.name,
            schema=self.plugin.get_schema_data(),
            handler_class=self,
        )

    async def render_panel(self, request: Request):
        return self.templates.TemplateResponse(
            request, "panel.html", {"plugin": self.plugin.name})

    async def list_archived(
        self,
        request: Request,
        current_user: Optional[User] = Depends(get_optional_user),
    ):
        service = _archive(request)
        user_id = _user_id(current_user)
        return {
            "user_id": user_id,
            "retention_days": service.retention_days,
            "archived": await service.list_archived(user_id),
        }

    async def restore_archived(
        self,
        request: Request,
        root_session_id: str,
        current_user: Optional[User] = Depends(get_optional_user),
    ):
        service = _archive(request)
        user_id = _user_id(current_user)
        try:
            result = await service.restore(user_id, root_session_id)
        except ArchiveNotFound as error:
            raise HTTPException(status_code=404, detail=str(error))
        except ArchiveError as error:
            # A conflict, not a typo: the tree is archived, but something of it
            # is live again. The text has to be readable in the toast.
            raise HTTPException(status_code=409, detail=str(error))
        logger.info("User %s restored archived session %s", user_id, root_session_id)
        return result

    async def forget_archived(
        self,
        request: Request,
        root_session_id: str,
        current_user: Optional[User] = Depends(get_optional_user),
    ):
        service = _archive(request)
        user_id = _user_id(current_user)
        try:
            result = await service.forget(user_id, root_session_id)
        except ArchiveNotFound as error:
            raise HTTPException(status_code=404, detail=str(error))
        logger.info("User %s deleted archived session %s", user_id, root_session_id)
        return result

    async def sweep_now(
        self,
        request: Request,
        dry_run: bool = False,
        current_user: Optional[User] = Depends(get_optional_user),
    ):
        """Archive what is due for this user right now, without waiting for the sweep."""
        service = _archive(request)
        user_id = _user_id(current_user)
        try:
            report = await service.archive_user(user_id, dry_run=dry_run)
        except ArchiveError as error:
            raise HTTPException(status_code=409, detail=str(error))
        return report.as_dict()
