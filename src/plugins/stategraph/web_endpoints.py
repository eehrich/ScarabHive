"""The State Graph panel and its JSON API: thin handlers over the plugin's service (``server.service``).

Every route is for admins (docs/stategraph_design.md §8.3): a machine holds Python and runs agents and tools, so
whoever may save or run one may run code on the server. The route rules in the config should say so as well; the
check here holds whatever they say, as the agent editor's does. Without auth the app has one user, its owner. Not
the wait questions' ``/answer`` and ``/pending``: the person whose request waits answers it, and question_routes
lets only them or an admin.
A write must arrive as JSON, so a plain cross-site form post cannot reach it.

Errors: the service raises ``ServiceError(status, message)``, answered as that status with the message as detail.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from agent_system.auth.database import get_db
from agent_system.auth.dependencies import bearer_scheme, get_optional_user
from agent_system.api.question_routes import question_router
from agent_system.auth.models import UserRole
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

#: The largest body a callback URL takes (its event's data).
CALLBACK_BODY = 64 * 1024
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


def _tags(text: Optional[str]) -> Optional[dict[str, str]]:
    """``name:value,name:value`` as tags; a value is matched as text (a number as JSON writes it)."""
    if not text:
        return None
    tags = {}
    for item in text.split(","):
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise HTTPException(status_code=422, detail=f"tags: name:value,name:value -- not {item!r}")
        tags[name.strip()] = value.strip()
    return tags


def _ctx_frames(text: Optional[str]) -> Optional[set[str]]:
    """``ctx`` of the run read: None (every frame's), ``none``, or a JSON list of frame prefixes."""
    if text is None:
        return None
    if text == "none":
        return set()
    try:
        prefixes = json.loads(text)
    except ValueError:
        prefixes = None
    if not isinstance(prefixes, list) or not all(isinstance(prefix, str) for prefix in prefixes):
        raise HTTPException(status_code=422, detail='ctx: "none" or a JSON list of frame prefixes, e.g. [""]')
    return set(prefixes)


def _seqs(text: Optional[str]) -> set[int]:
    """``seqs`` of the run read: journal seqs, comma-separated, at most 50."""
    if not text:
        return set()
    try:
        found = {int(item) for item in text.split(",") if item.strip()}
    except ValueError:
        found = None
    if found is None or len(found) > 50 or any(seq < 1 for seq in found):
        raise HTTPException(status_code=422, detail="seqs: at most 50 journal seqs, comma-separated (1,5,9)")
    return found


class StateGraphWebEndpoints:
    def __init__(self, server: Any):
        self.server = server
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        router = create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(),
                                      handler_class=self)
        router.include_router(self._wait_answers())
        return router

    def _wait_answers(self) -> APIRouter:
        """``/answer`` and ``/pending`` of the wait questions: the person whose request waits answers, or an admin
        (question_routes) -- as a reply in the conversation answers an ``on_wait: ask`` wait, not the admins' panel."""
        return question_router("", self.server.wait_questions, self.server.wait_answer_url)

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

    async def _call(self, method: str, *args: Any, off_loop: bool = False, **kwargs: Any) -> Any:
        """``server.service.<method>(...)``, awaited if it is a coroutine; its refusals become HTTP errors.
        ``off_loop``: a plain method that reads much runs in a worker thread."""
        # imported here: the service module brings the engine, which the panel page itself does not need
        from .model.yamledit import EditError
        from .service import ServiceError

        try:
            call = getattr(self.server.service, method)
            result = await asyncio.to_thread(call, *args, **kwargs) if off_loop else call(*args, **kwargs)
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
        """The list as the files' heads say it: cheap. Its problem counts come apart (``api_machine_checks``)."""
        await self._user(request)
        return await self._call("list_machines")

    async def api_machine_checks(self, request: Request):
        await self._user(request)
        # every machine loaded and validated, in a worker thread: seconds that must not hold up the other requests;
        # one computation for the panels and the tool list_machines (server.checked_machines, API-2)
        return await self.server.checked_machines()

    async def api_create_machine(self, request: Request):
        await self._user(request)
        body = await self._body(request)
        return await self._call("create_machine", _field(body, "id", str, required=True),
                                title=_field(body, "title", str), off_loop=True)

    async def api_get_machine(self, request: Request, machine_id: str):
        await self._user(request)
        return await self._call("get_machine", machine_id, off_loop=True)  # loads and validates: seconds (API-2)

    async def api_save_machine(self, request: Request, machine_id: str):
        await self._user(request)
        body = await self._body(request)
        files = _field(body, "files", dict, required=True)
        if not all(isinstance(path, str) and isinstance(text, str) for path, text in files.items()):
            raise HTTPException(status_code=422, detail="files: {relative path: text}")
        return await self._call("save_machine", machine_id, files,
                                expected_versions=_field(body, "expected_versions", dict),
                                force=bool(_field(body, "force", bool)), off_loop=True)

    async def api_delete_machine(self, request: Request, machine_id: str):
        await self._user(request)
        body = await self._body(request)
        return await self._call("delete_machine", machine_id, _field(body, "expected_version", str, required=True),
                                off_loop=True)  # loads every machine to find its importers

    async def api_edit_machine(self, request: Request, machine_id: str):
        await self._user(request)
        body = await self._body(request)
        drafts = _field(body, "drafts", dict)  # the panel's unsaved files: the edit goes onto them, not to disk
        return await self._call("edit_machine", machine_id, _field(body, "op", dict, required=True),
                                _field(body, "expected_version", str, required=drafts is None), drafts=drafts)

    async def api_save_layout(self, request: Request, machine_id: str):
        await self._user(request)
        body = await self._body(request)
        return await self._call("save_layout", machine_id, _field(body, "layout", dict, required=True))

    async def api_validate(self, request: Request):
        await self._user(request)
        body = await self._body(request)
        return await self._call("validate", files=_field(body, "files", dict), yaml=_field(body, "yaml", str),
                                machine_id=_field(body, "machine_id", str), off_loop=True)

    async def api_kinds(self, request: Request):
        await self._user(request)
        return await self._call("kinds")

    # ------------------------------------------------------------------ callback URLs (no login: the token is the key)
    # The token comes in the query (/callback?token=...: one path, which network.remote_paths can list) or, for a URL
    # made before, in the path; FastAPI reads it from whichever the route leaves it in.

    async def callback_page(self, request: Request, token: str):
        """What the URL would send, and a button that sends it: a GET must not -- mail scanners open links."""
        from fastapi.responses import HTMLResponse
        from html import escape

        found = await self._call("callback", token)
        event, machine = escape(str(found["event"])), escape(str(found["machine_id"]))
        return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width, initial-scale=1"><title>Send {event}</title></head><body style="font-family: sans-serif;
margin: 2rem; max-width: 32rem"><h1>Send {event}?</h1><p>This sends the event <b>{event}</b> to a run of the state
machine <b>{machine}</b>. The link works once.</p><button id="send">Send {event}</button><p id="said"></p><script>
document.getElementById('send').onclick = async (e) => {{ e.target.disabled = true;
  const said = document.getElementById('said');
  try {{
    const answer = await fetch(location.href, {{ method: 'POST', headers: {{ 'content-type': 'application/json' }}, body: '{{}}' }});
    if (answer.status === 202) {{ said.textContent = 'Handed to the run, which has not said yet what came of it. The link is used.'; return; }}
    if (answer.ok) {{ said.textContent = 'Sent.'; return; }}
    let why = answer.status + ' ' + answer.statusText;  // a proxy's or a crash's page is no JSON
    try {{ const detail = (await answer.json()).detail; why = typeof detail === 'string' ? detail : JSON.stringify(detail) || why; }} catch (_) {{}}
    said.textContent = 'Not sent: ' + why;
  }} catch (_) {{
    said.textContent = 'Not sent: the server could not be reached. Try again.';
  }}
  e.target.disabled = false;  // tried again: a link used meanwhile says so
}};
</script></body></html>""", headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})

    async def callback_send(self, request: Request, token: str):
        await self._call("callback", token)  # an unknown, used or expired URL reads no body
        body = b""
        async for chunk in request.stream():  # counted as it comes, not by its content-length: that can lie
            body += chunk
            if len(body) > CALLBACK_BODY:
                raise HTTPException(status_code=413, detail=f"the body may be {CALLBACK_BODY} bytes at most")
        data = None
        if body.strip():
            try:
                parsed = json.loads(body)
            except (ValueError, RecursionError):  # not JSON, or nested deeper than the parser goes
                raise HTTPException(status_code=422, detail="the body is JSON: {\"data\": ...}") from None
            if not isinstance(parsed, dict) or set(parsed) - {"data"}:
                raise HTTPException(status_code=422, detail="the body is a JSON object {\"data\": ...}")
            data = parsed.get("data")
        result = await self._call("use_callback", token, data)
        if result.get("outcome") == "unknown":  # handed to the process that holds the run, which did not answer
            from fastapi.responses import JSONResponse
            return JSONResponse(result, status_code=202)
        return result

    async def api_catalog(self, request: Request, machine_id: Optional[str] = None):
        """What the inspector's fields offer: the agents a machine may run, the tools its runner may call (the
        open machine's -- runners.py), the decision profiles."""
        await self._user(request)
        found = self.server._catalog("*", machine_id)
        tools = await self.server._runner_tools("*", machine_id)
        return {"agents": found["agents"], "profiles": found["decision_profiles"],
                "tools": [{"name": tool["name"], "description": tool["description"]} for tool in tools]}

    # ------------------------------------------------------------------ runs

    async def api_list_runs(self, request: Request, machine_id: Optional[str] = None,
                            limit: int = Query(50, ge=1, le=500), status: Optional[str] = None,
                            before: Optional[str] = None, nested: bool = False, tags: Optional[str] = None):
        """``tags``: ``name:value,name:value`` -- only runs with all of them (FEAT-7); without machine_id, the runs of
        every machine."""
        await self._user(request)
        return await self._call("list_runs", machine_id=machine_id, limit=limit, status=status or None,
                                before=before or None, nested=nested, off_loop=True,
                                **({"tags": _tags(tags)} if tags else {}))

    async def api_start_run(self, request: Request):
        user = await self._user(request)
        body = await self._body(request)
        return await self._call(
            "start_run", _field(body, "machine_id", str, required=True),
            params=_field(body, "params", dict), mocks=_field(body, "mocks", dict),
            mock_only=bool(_field(body, "mock_only", bool)),
            breakpoints=_field(body, "breakpoints", list) or [], watchpoints=_field(body, "watchpoints", list) or [],
            pause_at_start=bool(_field(body, "pause_at_start", bool)), user_id=user, tags=_field(body, "tags", dict))

    async def api_get_run(self, request: Request, run_id: str, steps: int = Query(50, ge=1, le=500),
                          after: Optional[int] = Query(None, ge=0), row_chars: Optional[int] = Query(None, ge=20),
                          ctx: Optional[str] = None, seqs: Optional[str] = None):
        """The run and its journal rows -- for a poll, cheap on request (UI-1): ``after`` only the rows after that
        seq; ``row_chars`` every text in the rows cut at that many characters; ``ctx``: ``none`` (no frame's ctx)
        or a JSON list of the frame prefixes whose ctx comes along; ``seqs``: these rows too, read again (``1,5``,
        at most 50) -- an activity's row ends in place, under the seq it started with, so ``after`` never brings
        it. Without them, the whole answer as before."""
        user = await self._user(request)
        wanted = _ctx_frames(ctx)
        again = _seqs(seqs)
        extra = {"after": after} if after is not None else {}
        row = await self._call("get_run", run_id, steps=steps, user_id=user, frames=True,  # the panel's frame picker
                               off_loop=True, **extra)
        if again:
            service = self.server.service
            missing = sorted(again - {entry["seq"] for entry in row.get("journal") or []})

            def read_again() -> list[dict[str, Any]]:
                # one thread hop for all of them, and one scan of the ended activities (_marked), not one per seq;
                # a row that is gone (a compacted journal) is left out
                found = [r for r in (service.run_store.row_at(run_id, seq) for seq in missing) if r is not None]
                return service._marked(run_id, row.get("status"), found)

            row["journal"] = sorted([*(row.get("journal") or []), *await asyncio.to_thread(read_again)],
                                    key=lambda entry: entry["seq"])
        if row_chars is not None:
            from .server import _capped

            row["journal"] = [{**entry, "data": _capped(entry.get("data"), row_chars)}
                              for entry in row.get("journal") or []]
        if wanted is not None and isinstance(row.get("view"), dict):
            row["view"] = {**row["view"], "frames": [
                frame if frame.get("prefix", "") in wanted else {**frame, "ctx": None, "ctx_left_out": True}
                for frame in row["view"].get("frames") or []]}
        return row

    async def api_run_journal(self, request: Request, run_id: str, after: int = Query(0, ge=0),
                              limit: int = Query(200, ge=1, le=2000), kinds: Optional[str] = None,
                              before: Optional[int] = Query(None, ge=1), state: Optional[str] = None,
                              seq: Optional[int] = Query(None, ge=1)):
        """A page of the journal: after a seq, or with ``before`` the last rows before one (UI-16); ``state``
        narrows it; ``seq``: that one row, whole."""
        await self._user(request)
        wanted = [kind for kind in (kinds or "").split(",") if kind] or None
        extra = {key: value for key, value in (("before", before), ("state", state or None), ("seq", seq))
                 if value is not None}
        # whole rows of a long run (a history page of 200, the result loader's 500): MBs to decode (API-2/UI-1)
        return await self._call("journal", run_id, after=after, limit=limit, kinds=wanted, off_loop=True, **extra)

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
        return await self._call("deliver_event", run_id, _field(body, "name", str, required=True),
                                data=body.get("data"), frame=_field(body, "frame", str), user_id=user)
