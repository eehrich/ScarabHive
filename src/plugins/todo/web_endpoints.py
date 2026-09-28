"""Web endpoints of the TODO plugin: the Todos panel and the calls it makes.

A session's task list is its user's (agent_system/auth/session_access.py): another user's session answers as one
without tasks, and changing or deleting in it as a task that is not there -- they read and changed any session whose
id they knew.
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import User
from agent_system.auth.session_access import may_see_session
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

SESSION = Query(..., description="The session whose task list")


def answered(result: dict) -> dict:
    """The server answers a refusal like a result; here it becomes an error, which the panel shows rather than takes for done."""
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=result["message"])
    if result["status"] == "status_changed":
        raise HTTPException(status_code=409, detail=result["message"])
    if result["status"] == "rejected":  # the server's message offers cascade=True, which these endpoints do not take
        raise HTTPException(status_code=409, detail=f"Other tasks depend on {result['task_id']}: {', '.join(result['dependent_tasks'])}")
    return result


#: What list_todos answers for a session without tasks (all of its defaults): another user's session answers so.
NO_TASKS = {"total_count": 0, "filtered_count": 0, "returned_count": 0, "offset": 0, "limit": None,
            "has_more": False, "next_offset": None, "tasks": [], "filters_applied": {}}


async def not_there(request: Request, current_user: Optional[User], session_id: str, task_id: str) -> None:
    """404 for a task of another user's session, as for a task that is not there."""
    if not await may_see_session(request, current_user, session_id):
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")


class TodoWebFactory:
    """Web UI factory for TODO management."""

    def __init__(self, server):
        self.server = server
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(), handler_class=self)

    async def get_panel(self, request: Request):
        """Render the panel; its script and stylesheet are the plugin's static assets."""
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name})

    async def get_tasks(self, request: Request, session_id: str = SESSION,
                        current_user: Optional[User] = Depends(get_optional_user)) -> dict:
        if not await may_see_session(request, current_user, session_id):
            return dict(NO_TASKS)
        return await self.server.list_todos(context={"session_id": session_id})

    # only from the statuses the panel offers the action in: a card out of date must not reopen a task an agent finished
    # (a task not started that came to wait on another meanwhile is started and blocked at once, as for an agent)
    async def start_task(self, request: Request, task_id: str, session_id: str = SESSION,
                         current_user: Optional[User] = Depends(get_optional_user)) -> dict:
        await not_there(request, current_user, session_id, task_id)
        return answered(await self.server.update_todo(task_id=task_id, new_status="in-progress", only_from=["not-started"],
                                                      context={"session_id": session_id}))

    async def complete_task(self, request: Request, task_id: str, session_id: str = SESSION,
                            current_user: Optional[User] = Depends(get_optional_user)) -> dict:
        await not_there(request, current_user, session_id, task_id)
        return answered(await self.server.update_todo(task_id=task_id, new_status="completed",
                                                      only_from=["not-started", "in-progress", "blocked"],
                                                      context={"session_id": session_id}))

    async def delete_task(self, request: Request, task_id: str, session_id: str = SESSION,
                          current_user: Optional[User] = Depends(get_optional_user)) -> dict:
        """Without its dependents: a task others depend on is refused."""
        await not_there(request, current_user, session_id, task_id)
        return answered(await self.server.delete_todo(task_id=task_id, context={"session_id": session_id}))
