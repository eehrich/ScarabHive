"""The State Graph panel and its JSON API: thin handlers over the plugin's service (``server.service``).

Every route is for admins (docs/stategraph_design.md §8.3): a machine holds Python and runs agents and tools, so
whoever may save or run one may run code on the server. The route rules in the config should say so as well; the
check here holds whatever they say, as the agent editor's does. Without auth the app has one user, its owner.
A write must arrive as JSON, so a plain cross-site form post cannot reach it.

Errors: the service raises ``ServiceError(status, message)``, answered as that status with the message as detail.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from agent_system.auth.database import get_db
from agent_system.auth.dependencies import bearer_scheme, get_optional_user
from agent_system.auth.models import UserRole
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

#: Arguments control_run takes besides the action (schema.yaml, tool control_run).
CONTROL_ARGS = frozenset({"state", "machine", "at_step", "definition", "breakpoints", "watchpoints", "expr", "path",
                          "steps", "pause", "mocks"})


def _field(body: dict[str, Any], key: str, kind: type | tuple[type, ...], *, required: bool = False) -> Any:
    value = body.get(key)
    if value is None:
        if required:
            raise HTTPException(status_code=422, detail=f"{key} is required")
        return None
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        names = " or ".join(t.__name__ for t in (kind if isinstance(kind, tuple) else (kind,)))
        raise HTTPException(status_code=422, detail=f"{key}: {names} expected")
    return value


class StateGraphWebEndpoints:
    def __init__(self, server: Any):
        self.server = server
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(),
                                    handler_class=self)

    # ------------------------------------------------------------------ plumbing

    async def _user(self, request: Request) -> Optional[str]:
        """The admin asking (their username), or None when the app runs without auth; anyone else is refused."""
        auth = getattr(getattr(request.app.state, "config", None), "auth", None)
        if auth is not None and not auth.enabled:
            return None
        user = await get_optional_user(request, await bearer_scheme(request), request.headers.get("X-API-Key"),
                                       get_db())
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if user.role != UserRole.ADMIN:
            raise HTTPException(status_code=403, detail="State machines run Python on the server: admins only")
        return user.username

    async def _body(self, request: Request) -> dict[str, Any]:
        if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
            raise HTTPException(status_code=415, detail="Send the request as JSON")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="The body is not valid JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="The body must be a JSON object")
        return body

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """``server.service.<method>(...)``, awaited if it is a coroutine; its refusals become HTTP errors."""
        # imported here: the service module brings the engine, which the panel page itself does not need
        from .model.yamledit import EditError
        from .service import ServiceError

        try:
            result = getattr(self.server.service, method)(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except ServiceError as exc:
            raise HTTPException(status_code=getattr(exc, "status", 500),
                                detail=getattr(exc, "message", str(exc))) from None
        except EditError as exc:
            raise HTTPException(status_code=422, detail=exc.message) from None
        return result

    # ------------------------------------------------------------------ the panel

    async def render_panel(self, request: Request):
        # the viewer: a run's sessions are its user's, and the chat opens only the viewer's own
        viewer = await self._user(request)
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name, "viewer": viewer or ""})

    # ------------------------------------------------------------------ machines

    async def api_list_machines(self, request: Request):
        await self._user(request)
        return await self._call("list_machines")

    async def api_create_machine(self, request: Request):
        await self._user(request)
        body = await self._body(request)
        return await self._call("create_machine", _field(body, "id", str, required=True),
                                title=_field(body, "title", str))

    async def api_get_machine(self, request: Request, machine_id: str):
        await self._user(request)
        return await self._call("get_machine", machine_id)

    async def api_save_machine(self, request: Request, machine_id: str):
        await self._user(request)
        body = await self._body(request)
        files = _field(body, "files", dict, required=True)
        if not all(isinstance(path, str) and isinstance(text, str) for path, text in files.items()):
            raise HTTPException(status_code=422, detail="files: {relative path: text}")
        return await self._call("save_machine", machine_id, files,
                                expected_versions=_field(body, "expected_versions", dict),
                                force=bool(_field(body, "force", bool)))

    async def api_delete_machine(self, request: Request, machine_id: str):
        await self._user(request)
        body = await self._body(request)
        return await self._call("delete_machine", machine_id, _field(body, "expected_version", str, required=True))

    async def api_edit_machine(self, request: Request, machine_id: str):
        await self._user(request)
        body = await self._body(request)
        return await self._call("edit_machine", machine_id, _field(body, "op", dict, required=True),
                                _field(body, "expected_version", str, required=True))

    async def api_save_layout(self, request: Request, machine_id: str):
        await self._user(request)
        body = await self._body(request)
        return await self._call("save_layout", machine_id, _field(body, "layout", dict, required=True))

    async def api_validate(self, request: Request):
        await self._user(request)
        body = await self._body(request)
        return await self._call("validate", files=_field(body, "files", dict), yaml=_field(body, "yaml", str),
                                machine_id=_field(body, "machine_id", str))

    async def api_kinds(self, request: Request):
        await self._user(request)
        return await self._call("kinds")

    async def api_catalog(self, request: Request):
        """What the inspector's fields offer: the agents a machine may run, the tools its runner may call, the
        decision profiles."""
        await self._user(request)
        found = self.server._catalog()
        tools = await self.server._runner_tools("*")
        return {"agents": found["agents"], "profiles": found["decision_profiles"],
                "tools": [{"name": tool["name"], "description": tool["description"]} for tool in tools]}

    # ------------------------------------------------------------------ runs

    async def api_list_runs(self, request: Request, machine_id: Optional[str] = None,
                            limit: int = Query(50, ge=1, le=500), status: Optional[str] = None,
                            before: Optional[str] = None):
        await self._user(request)
        return await self._call("list_runs", machine_id=machine_id, limit=limit, status=status or None,
                                before=before or None)

    async def api_start_run(self, request: Request):
        user = await self._user(request)
        body = await self._body(request)
        return await self._call(
            "start_run", _field(body, "machine_id", str, required=True),
            params=_field(body, "params", dict), mocks=_field(body, "mocks", dict),
            mock_only=bool(_field(body, "mock_only", bool)),
            breakpoints=_field(body, "breakpoints", list) or [], watchpoints=_field(body, "watchpoints", list) or [],
            pause_at_start=bool(_field(body, "pause_at_start", bool)), user_id=user)

    async def api_get_run(self, request: Request, run_id: str, steps: int = Query(50, ge=1, le=500)):
        user = await self._user(request)
        return await self._call("get_run", run_id, steps=steps, user_id=user)

    async def api_run_journal(self, request: Request, run_id: str, after: int = Query(0, ge=0),
                              limit: int = Query(200, ge=1, le=2000), kinds: Optional[str] = None):
        await self._user(request)
        wanted = [kind for kind in (kinds or "").split(",") if kind] or None
        return await self._call("journal", run_id, after=after, limit=limit, kinds=wanted)

    async def api_control_run(self, request: Request, run_id: str):
        user = await self._user(request)
        body = await self._body(request)
        action = _field(body, "action", str, required=True)
        unknown = sorted(set(body) - CONTROL_ARGS - {"action"})
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown arguments {', '.join(unknown)} for control_run")
        # resume and terminate run as the run's own user, a fork is a new run of the admin asking (service)
        arguments = {key: value for key, value in body.items() if key != "action"}
        return await self._call("control_run", run_id, action, **arguments, user_id=user)

    async def api_send_event(self, request: Request, run_id: str):
        user = await self._user(request)
        body = await self._body(request)
        return await self._call("send_event", run_id, _field(body, "name", str, required=True),
                                data=body.get("data"), frame=_field(body, "frame", str), user_id=user)
