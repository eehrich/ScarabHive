"""The shell in a real browser: launcher, dock and windows, the pk:* host side, sessions, layout, sign-in.

The page is the real index.html (and login.html) with the real shell and chat
scripts; only the server behind it is a stub. The catalogue carries a probe
panel that records what the host sends, and two instances of one plugin. The
cookie ``stub_account`` picks what /auth/me answers: absent -- authentication
is off; ``admin`` -- signed in; ``expired`` -- 401; ``inactive`` -- 403;
``broken`` -- 500. ``stub_catalog`` makes the catalogue fail (``broken``) or
answer after a second (``slow``); ``stub_agents`` makes /agents answer after
1.5 s with ``writer`` as default (``slow``), fail (``broken``) or name a default it does not list
(``nodefault``); ``stub_list=held`` and ``stub_children=held``
hold the answer of the session list and of a branch, as they were when asked,
until POST /__stub/lists/release (GET /__stub/lists counts the held ones),
``stub_list=broken`` fails the list;
``stub_requests=running`` lists one running
request an administrator may cancel. The requests ``r-live``, ``r-live-busy``,
``r-live-files``, ``r-live-refusing`` (appends fail after half a second),
``r-live-finishing`` (an append finds it finished), ``r-live-closing`` (its
stream ends with the run after 0.8 s, an append to it is taken after 1.5 s),
``r-live-stopping`` (ends after 1.5 s), ``r-live-dropping`` (its stream drops),
``r-live-final`` (its final answer, then its stream closes), ``r-live-answered``
(its final answer, and its end 3 s later), ``r-live-cancelled``
(cancelled elsewhere), ``r-live-failing`` (its error, then its end),
``r-live-severed`` (its stream breaks as a cancel is asked for),
``r-live-honouring`` (honours a cancel at once with its cancelled event and
end), ``r-live-honouring-late`` (does so a second after the cancel is asked
for, and brings its end 1.5 s after the cancelled event), ``r-live-leaving``, every
``r-live-kept...`` and, after a slower status check, ``r-live-slow`` are still
running after a reload -- a stream followed again names its run first;
``r-live-ended`` is done, told after a second, ``r-live-unanswered`` gets no
answer, any other request is unknown. A cancel of ``r-live-stopping`` (told
after 3 s) or ``r-ending`` finds nothing; any other is cancelled and stays
cancelled for the rest of the page. Every answered cancel is counted. ``stub_cancel=fails``
makes cancels fail, ``stub_cancel=slow`` and ``slower`` answer them after 1.5
and 6 s. A session asked for while a status check is being answered is
counted. Every line typed with a slash resolves as that chat command. A message
the chat sends starts a run that goes on for a few seconds; with
``stub_stream=drops``, ``final-drops``, ``cancelled-drops``, ``late-drops``,
``question-drops``, ``closes``, ``ending``, ``answered`` or ``late-start-drops`` its stream
names ``r-dropped``, ``r-final-dropped``, ``r-cancelled-dropped``,
``r-late-dropped``, ``r-question-dropped``, ``r-closed``, ``r-ending``, ``r-answered`` or --
after 1.5 s -- ``r-late-started`` in the message's session and is cut off, cut
off after its final answer or its cancel (1.5 s in), cut off after 3 or 1.5 s, closed, brings its final answer
and end after a second, brings its final answer after half a second and its end 3 s later, or is cut off a
second after it has started; with
``stub_stream=refused`` the server refuses the run, with ``refused-late`` after a
second, with its error and then its end. With ``reasons`` a run ``r-reasons``
sends a step the way a reasoning model does -- its reasoning in three deltas
split mid-word, then the step's answer as markup and a tool call -- and ends;
with ``steps`` a run ``r-steps`` sends TWO calls, a tool scope that carries no
step of its own, the run's own start and end, which carry none either, a
sub-agent counting ITS steps, a row whose parent is never sent, and two calls to
one tool that open no scope at all. Every status
event of it carries a ``tree``, as the run's stream does since 6b6a1348. One
with files starts half a second later, names ``r-files-ended`` and brings its
final answer and end -- with ``stub_stream=stale``, ``r-files-stale`` in
``s-files-new``, and it goes on; with ``final-drops``, ``r-files-final-dropped``,
cut off after its final answer; ``refused`` refuses it. With ``stub_stream=saving``
a message's run ``r-saving``, one with files ``r-files-saving``, and the run
``r-live-saving`` followed after a reload bring their final answer, word a
second later and, 3 s after the answer, their save -- the session's title in
the list names the run -- and their end; the request of the message and of the
one with files works on half a second after it; ``saving-drops`` names
``r-saving-dropped`` (with files ``r-files-saving-dropped``), brings its final
answer and word a second later, and is cut off a second after that. /__stub/streams tells of each run's stream whether
it was read to its end or cut off by the browser. The session ``s-run`` is one
whose RUN is still in its file: an LLM call that reasoned and called a tool,
the tool's answer as the JSON string a session stores, and the call that
answered; every other session carries two plain messages. A session added through
/__stub/sessions may carry ``delay`` (seconds to answer), ``trickle`` (headers
at once, the body after that many seconds), ``fails`` (its load fails),
``delete_delay`` and ``delete_fails`` (how many deletes of it fail);
``session-query:<id>`` collects the raw query string of every load of it, which
is how a test says that the chat's load and the DELETE of a session stay the
same URL. The
Sub-Agents instance ``sam_writer``, ``context_engineer``, ``memory``, ``todo``
and ``context_usage_tracker`` are the real plugin panels; what they ask their
plugin for, memory searches included, is counted by the session it names,
answered with a number per session in every figure the panels show and, for
``s-lagging``, late. A message to a running request is appended through
/events/<id>/append.

Which sessions are running is set by a test through POST /__stub/active-runs
(session id -> request id), and ``GET /api/sessions/active`` answers from that
table, capping the ids at 200 as the real endpoint does. A value ending in
``!`` is a run the server knows of but that cannot be reconnected to
(``attachable`` false) -- and ``GET /events`` answers 409 for exactly those
request ids, as app.py does, so the stub cannot say "not attachable" here and
let the reconnect through anyway; one ending in ``~`` has answered already and
is only finishing (``answered`` true). ``stub_active=broken`` fails the poll
with 502, ``slow`` answers it after 1.2 s. A reconnect records what it was told
to catch up on: ``catch-up:<request_id>`` is ``"<catch_up>/<seen>"``.
``r-live-buffered`` holds four buffered events and sends only those past
``seen``; ``r-live-over`` reports a finished run in its reconnect and closes at
once. A session added through /__stub/sessions may carry ``live_events_seen``,
which is what the chat hands back as ``seen``.

A mutation probe replaces a served script through the environment variable
``SHELL_MUTANTS`` rather than by writing to ``static/`` -- see
:func:`_serve_mutated_sources`.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent_system.ui.catalog import Panel, build_catalog, core_panels
from agent_system.ui.resources import STATIC_DIR, ui_templates
from agent_system.ui.routes import router
from tests.ui.browser import find_browser, run_app_test_page

BROWSER = find_browser()
PAGE_TIMEOUT = 300
# the whole page runs in the first test's fixture: pytest's default of 120 s would cut it off
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

UI_TESTS = Path(__file__).resolve().parent
PLUGINS = UI_TESTS.parents[1] / "src" / "plugins"
PROBE_PAGE = "/tests/ui/shell_probe_panel.html"
USER = {"username": "ada", "full_name": "Ada Admin", "email": "ada@example.org", "role": "admin"}


def plugin_panels() -> list[Panel]:
    return [
        Panel("probe", "Probe", PROBE_PAGE, "bug", "debug", "Records the protocol messages the shell sends", ["probe"],
              contexts={"request": f"{PROBE_PAGE}?request_id={{request_id}}",
                        "session": f"{PROBE_PAGE}?session_id={{session_id}}"}),
        Panel("sam_writer", "Sub-Agents", "/plugins/sam_writer/", "workflow", "agents", "Sub-agent tasks"),
        Panel("sam_skills", "Sub-Agents", "/tests/ui/missing_panel_b.html", "workflow", "agents", "Sub-agent tasks"),
        Panel("context_engineer", "Context Engineer", "/plugins/context_engineer/", "brain", "context", "Context archive"),
        Panel("memory", "Memory", "/plugins/memory/", "database", "context", "Stored memories"),
        Panel("todo", "Todos", "/plugins/todo/", "list-todo", "agents", "Task lists"),
        Panel("context_usage_tracker", "Context & Cost Usage", "/plugins/context_usage_tracker/", "chart-column",
              "context", "Token usage"),
    ]


SHELL_MUTANTS_ENV = "SHELL_MUTANTS"


def _serve_mutated_sources(app: FastAPI) -> None:
    """Let a mutation probe replace a served file WITHOUT writing it to disk.

    ``SHELL_MUTANTS`` names a JSON file, ``{"/static/js/x.js": "<file holding the
    mutated source>"}``; those paths are then answered from that source instead of
    from the tree. A probe that swaps the real file for a few minutes instead is
    served live by whatever runs against this working tree -- the user's own shell
    reload, and any browser suite a parallel session happens to be running. That has
    cost another session half an hour of unexplained red checks once, and made this
    session's own probe look to a peer like a restore that never happened. The file
    on disk is never touched here, so an abort cannot leave a mutant behind either --
    which a ``finally`` cannot promise: it only protects what it reaches.
    """
    listed = os.environ.get(SHELL_MUTANTS_ENV)
    if not listed:
        return
    served = {path: Path(source).read_text(encoding="utf-8")
              for path, source in json.loads(Path(listed).read_text(encoding="utf-8")).items()}

    @app.middleware("http")
    async def from_memory(request: Request, call_next):
        source = served.get(request.url.path)
        if source is None:
            return await call_next(request)
        # The type comes from the path, not from a guess: served as JavaScript, a
        # mutated stylesheet is dropped by the browser without a word, and the probe
        # then measures a page with no styling at all.
        # no-store: the page is reloaded within one run, and a cached copy would
        # quietly measure the previous mutant.
        return Response(source, media_type=mimetypes.guess_type(request.url.path)[0] or "text/plain",
                        headers={"Cache-Control": "no-store"})


def stub_app() -> FastAPI:
    app = FastAPI()
    templates = ui_templates()
    now = datetime.now(timezone.utc).isoformat()
    sessions = [{"session_id": "s-1", "title": "Refactor the kit", "agent_name": "assistant",
                 "updated_at": now, "depth": 0, "has_children": True, "children": []},
                # A session whose RUN is still in it: what the model thought, what it
                # asked a tool and what came back. The shape a real file has -- measured
                # over 58 of them on 21.09.2026 -- and what a reload used to throw away.
                {"session_id": "s-run", "title": "A run read back", "agent_name": "assistant",
                 "updated_at": now, "depth": 0, "has_children": False, "children": [],
                 "run": True}]
    children = {"s-1": [{**sessions[0], "session_id": "s-1-sub", "title": "Research the icons", "depth": 1}]}
    patches: list[dict] = []
    posted: list[dict] = []
    deletes: list[str] = []
    hits: dict[str, int] = {}
    streams: dict[str, str] = {}  # request id -> "read" (to its end) or "cut" (by the browser)
    held_lists: list[asyncio.Event] = []  # session lists asked for with stub_list=held, answered on release

    def account(request: Request) -> dict:
        state = request.cookies.get("stub_account")
        if state is None:
            raise HTTPException(status_code=404)  # authentication off: no /auth routes
        if state == "expired":
            raise HTTPException(status_code=401)
        if state == "inactive":
            raise HTTPException(status_code=403, detail="Inactive user")
        if state == "broken":
            raise HTTPException(status_code=500, detail="database is locked")
        return USER

    @app.get("/")
    async def index(request: Request):
        return templates.TemplateResponse(request, "index.html")

    @app.get("/login")
    async def login(request: Request):
        return templates.TemplateResponse(request, "login.html")

    @app.get("/api/ui/catalog")  # registered before the real router: the stub wins
    async def catalog(request: Request):
        if request.cookies.get("stub_catalog") == "broken":
            raise HTTPException(status_code=500, detail="catalogue failed")
        if request.cookies.get("stub_catalog") == "slow":
            await asyncio.sleep(1)
        core = core_panels(audit_enabled=False, profiling_enabled=False, memory_profiling_enabled=False)
        return build_catalog("admin", core, plugin_panels())

    @app.get("/admin/active-sessions")
    async def active_sessions(request: Request):
        if request.cookies.get("stub_requests") == "running":
            return {"total": 1, "sessions": [{"user_id": "ada", "agent_name": "assistant", "duration_seconds": 12,
                                              "status": "running", "request_id": "r-cancel-me"}]}
        hits["active-sessions"] = hits.get("active-sessions", 0) + 1
        raise HTTPException(status_code=404)  # authentication off: no /admin routes

    @app.post("/admin/active-sessions/{request_id}/cancel")
    async def cancel_request(request_id: str):
        await asyncio.sleep(1)  # the server answers once the run has stopped
        return {"status": "cancelled", "request_id": request_id}

    checking: set[str] = set()  # requests whose status check is still being answered
    cancelled: set[str] = set()  # a cancelled run stays cancelled for the rest of the page

    @app.get("/api/requests/{request_id}/status")
    async def request_status(request_id: str):
        hits[f"status:{request_id}"] = hits.get(f"status:{request_id}", 0) + 1
        if request_id in cancelled:
            return {"status": "cancelled"}
        if request_id == "r-live-ended":  # done, told after a second
            await asyncio.sleep(1)
            return {"status": "completed", "completed": True}
        if request_id == "r-live-unanswered":
            raise HTTPException(status_code=503, detail="The job registry is unavailable")
        running = {"r-live-slow": 1, **dict.fromkeys(  # how long the check takes
            ["r-live", "r-live-busy", "r-live-refusing", "r-live-finishing", "r-live-files", "r-live-dropping",
             "r-live-closing", "r-live-stopping", "r-live-final", "r-live-cancelled", "r-live-failing", "r-live-severed",
             "r-live-honouring", "r-live-honouring-late", "r-live-leaving", "r-live-answered", "r-live-saving",
             "r-dropped"], 0.3)}
        if request_id.startswith("r-live-kept"):  # one per phase that cancels it: a cancelled run stays cancelled
            running[request_id] = 0.3
        if request_id not in running:
            return {"status": "unknown", "completed": False}  # as app.py answers for a run it does not know
        checking.add(request_id)
        try:
            await asyncio.sleep(running[request_id])
        finally:
            checking.discard(request_id)
        return {"status": "running"}

    def lag(session_id: str) -> float:
        """What a plugin panel asks for about ``s-lagging`` is answered late."""
        return 1.5 if session_id == "s-lagging" else 0

    def marker(session_id: str) -> int:
        """The number the plugin panels show: 7 for ``s-1``, 3 for any other session or none."""
        return 7 if session_id == "s-1" else 3

    @app.get("/plugins/sam_writer/")  # an instance of sub_agent_manager: its templates and static files are the plugin's
    async def sub_agent_panel(request: Request):
        panel_templates = ui_templates(PLUGINS / "sub_agent_manager" / "templates")
        return panel_templates.TemplateResponse(request, "panel.html", {"plugin": "sam_writer"})

    @app.get("/plugins/sam_writer/static/{name}")
    async def sub_agent_static(name: str):
        return FileResponse(PLUGINS / "sub_agent_manager" / "static" / name)

    @app.get("/plugins/sam_writer/sub-agents")
    async def sub_agents(session_id: str):
        hits[f"sam_writer:{session_id}"] = hits.get(f"sam_writer:{session_id}", 0) + 1
        await asyncio.sleep(lag(session_id))
        count = marker(session_id)
        return {"instances": [{"instance_id": f"agent-{session_id}", "agent_type": "writer", "status": "active"}],
                "phase": {"variable": "workflow_phase", "current": f"phase-{count}", "agents": [], "allowed_agents": []}}

    @app.get("/plugins/context_engineer/history")
    async def context_engineer_history(session_id: str = ""):
        hits[f"context_engineer:{session_id}"] = hits.get(f"context_engineer:{session_id}", 0) + 1
        await asyncio.sleep(lag(session_id))
        count = marker(session_id)
        return {"events": [], "stats": {"events": count, "tokens_saved": count, "average_reduction": None,
                                        "media_always_compacted": 0, "media_deduplicated": 0,
                                        "media_compacted_after_event": 0}}

    @app.get("/plugins/context_engineer/session")
    async def context_engineer_session(session_id: str):
        hits[f"context_engineer:{session_id}"] = hits.get(f"context_engineer:{session_id}", 0) + 1
        await asyncio.sleep(lag(session_id))
        return {"tool_results": {"count": marker(session_id), "tokens": 0}, "archived": {"count": 0, "tokens": 0},
                "core_memory": {"facts": [], "tokens": 0, "max_tokens": 2000}}

    @app.get("/plugins/{plugin}/")
    async def kit_panel(request: Request, plugin: str):  # a panel on the kit: rendered as its plugin renders it
        panel_templates = ui_templates(PLUGINS / plugin / "templates")
        return panel_templates.TemplateResponse(request, "panel.html", {"plugin": plugin})

    @app.get("/plugins/{plugin}/static/{name}")
    async def kit_panel_static(plugin: str, name: str):
        return FileResponse(PLUGINS / plugin / "static" / name)

    @app.post("/plugins/memory/memories/search")
    async def memory_search(session_id: str = ""):
        hits[f"memory:{session_id}"] = hits.get(f"memory:{session_id}", 0) + 1
        await asyncio.sleep(lag(session_id))
        return {"results": [{"memory_id": "m-1", "title": f"found-{marker(session_id)}", "content": "", "keywords": [],
                             "importance": 1, "access_count": 0, "similarity": 0.9}]}

    @app.get("/plugins/{instance}/{call:path}")
    async def plugin_call(instance: str, call: str, session_id: str = ""):
        hits[f"{instance}:{session_id}"] = hits.get(f"{instance}:{session_id}", 0) + 1
        await asyncio.sleep(lag(session_id))
        count = marker(session_id)
        tasks = [{"task_id": f"task_{number}", "title": f"Task {number}", "status": "not-started", "priority": "medium",
                  "progress": 0, "created_at": now, "started_at": None, "description": None, "tags": [],
                  "depends_on": [], "blocks": [], "is_blocked": False} for number in range(count)]
        return {"total_memories": count, "avg_importance": None, "total_accesses": count, "memories": [],
                "tasks": tasks, "total": count,
                "statistics": {"totals": {"completion_tokens": count}},
                "enabled": True, "current_phase": f"phase-{count}", "all_allowed_agents": [], "filtered_agents": []}

    @app.post("/events/{request_id}/append")
    async def append(request_id: str):
        hits[f"append:{request_id}"] = hits.get(f"append:{request_id}", 0) + 1
        if request_id == "r-live-refusing":
            await asyncio.sleep(0.5)  # long enough to type something else meanwhile
            raise HTTPException(status_code=500, detail="The message queue is broken")
        if request_id == "r-live-finishing":
            raise HTTPException(status_code=404, detail="Request not active")
        if request_id == "r-live-closing":  # taken only after the run's stream has ended
            await asyncio.sleep(1.5)
        return {"status": "appended"}

    def event(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    def run_stream():
        async def stream():
            yield ": running\n\n"
            await asyncio.sleep(5)  # the run goes on; the page usually leaves before it ends
        return StreamingResponse(stream(), media_type="text/event-stream")

    async def saving(session_id: str, request_id: str, request_goes_on: bool):
        """What a run past its answer sends while it saves and runs its hooks -- word after a second, its end
        after 3 s -- and what its save does to the session list: the session is titled by the run. The request of
        a fetch stream (``request_goes_on``) works on after the end: app.py saves once more and releases the run."""
        await asyncio.sleep(1)
        yield ": saving\n\n"
        await asyncio.sleep(2)
        for listed in sessions:
            if listed["session_id"] == session_id:
                listed["title"] = f"Saved by {request_id}"
        yield event({"type": "end"})
        if request_goes_on:
            await asyncio.sleep(0.5)

    def recorded(request_id: str, body):
        """The stream of a run, recorded as read to its end or cut off by the browser."""
        async def stream():
            try:
                async for chunk in body:
                    yield chunk
            except asyncio.CancelledError:
                streams[request_id] = "cut"
                raise
            streams[request_id] = "read"
        return StreamingResponse(stream(), media_type="text/event-stream")

    def started_stream(request_id: str, session_id: str, ending: str, start_after: float = 0):
        async def stream():
            yield ":ok\n\n"
            await asyncio.sleep(start_after)  # a run with files starts once its files are read
            yield event({"type": "start", "request_id": request_id, "session_id": session_id})
            await asyncio.sleep({"stale": 5, "ending": 1, "late-drops": 3, "question-drops": 1.5, "final-drops": 1.5,
                                 "late-start-drops": 1}.get(ending, 0.5))
            if ending == "ending":
                yield event({"type": "final", "content": "Done"})
                yield event({"type": "end"})
            if ending == "answered":  # its save and hooks take 3 s after the answer
                yield event({"type": "final", "content": "Done"})
                await asyncio.sleep(3)
                yield event({"type": "end"})
            if ending == "saving":
                yield event({"type": "final", "content": "Done"})
                async for chunk in saving(session_id, request_id, request_goes_on=True):
                    yield chunk
            if ending == "saving-drops":  # word from the save a second after the answer, then the connection breaks
                yield event({"type": "final", "content": "Done"})
                await asyncio.sleep(1)
                yield ": saving\n\n"
                await asyncio.sleep(1)
            if ending == "reasons":
                # What a reasoning model's step really sends. The deltas arrive split
                # mid-word, as providers send them, so a box that re-rendered instead of
                # appending would show the last fragment only. The `thinking` event after
                # them carries the step's ANSWER -- already through the format_output
                # hook, hence the markup -- and the tool call Status names anyway.
                for delta in ("No datetime t", "ool here, so I say ", "so."):
                    yield event({"type": "reasoning_delta", "step": 1, "delta": delta})
                yield event({"type": "thinking", "step": 1,
                             "assistant": {"content": "<p>There is no <code>datetime</code> tool.</p>",
                                           "tool_calls": [{"function": {"name": "datetime_now"}}],
                                           "content_format": "html"}})
                yield event({"type": "final", "content": "Done"})
                yield event({"type": "end"})
                return
            if ending == "steps":
                # A two-call run, shaped like the one measured through agent.run_events
                # on 2026-09-20. What matters here is what carries a step and what does
                # not: the coordinator's own start and end carry none (they are the
                # RUN's), and a tool scope carries none either -- status_scope knows the
                # tool, not the loop -- so only the order says which call set it off.
                # The request ids are the shapes the real run uses (measured): the run's
                # own loop suffixes `_nnn`, and anything with a run of its own suffixes
                # `_sub_<id>` and counts ITS OWN steps.
                yield event({"type": "status", "server": "coordinator", "phase": "start",
                             "request_id": f"{request_id}_001", "message": "started", "meta": {},
                             "tree": {"parent_id": request_id, "depth_level": 1, "child_count": 0, "is_leaf": True}})
                yield event({"type": "thinking", "step": 1})
                yield event({"type": "status", "server": "coordinator", "phase": "progress",
                             "request_id": f"{request_id}_001", "message": "step 1/30",
                             "meta": {"step": 1}, "tree": {"parent_id": request_id, "depth_level": 1, "child_count": 0, "is_leaf": True}})
                yield event({"type": "status", "server": "worker", "phase": "progress",
                             "request_id": f"{request_id}_002", "message": "Calling LLM (one)",
                             "meta": {"step": 1}, "tree": {"parent_id": request_id, "depth_level": 1, "child_count": 0, "is_leaf": True}})
                for delta in ("weighing the ", "first move."):
                    yield event({"type": "reasoning_delta", "step": 1, "delta": delta})
                yield event({"type": "thinking", "step": 1, "assistant": {
                    "content": "", "tool_calls": [{"function": {"name": "file_ops_read_file"}}]}})
                yield event({"type": "status", "server": "file_ops.read_file()", "phase": "start",
                             "request_id": f"{request_id}_003", "message": "started", "meta": {},
                             "tree": {"parent_id": request_id, "depth_level": 1, "child_count": 0, "is_leaf": True}})
                yield event({"type": "status", "server": "file_ops.read_file()", "phase": "end",
                             "request_id": f"{request_id}_003", "message": "Read README.md", "meta": {},
                             "tree": {"parent_id": request_id, "depth_level": 1, "child_count": 0, "is_leaf": True}})
                # Same request id as the scope above, and after its lines -- the shape a
                # real run sends. The result is deliberately past the page's cap.
                yield event({"type": "tool_call", "step": 1, "server": "file_ops",
                             "action": "file_ops_read_file", "request_id": f"{request_id}_003",
                             "params": {"filePath": "README.md"}})
                yield event({"type": "tool_result", "step": 1, "server": "file_ops",
                             "action": "file_ops_read_file", "request_id": f"{request_id}_003",
                             "result": {"status": "success", "note": "<b>not markup</b>",
                                        "content": "x" * 5000}})
                yield event({"type": "thinking", "step": 2})
                # A sub-agent spawned by the SECOND call, reporting ITS first step. Its
                # `meta.step` is a step of the sub-run -- taken at face value it would
                # file these lines under the parent's call 1.
                #
                # These two carry `tree`, which the run's own stream does NOT today
                # (DirectStatusHandler leaves it out, so the page's nesting machinery
                # never runs). Sent here to measure what that machinery does with the
                # shape the server would send, BEFORE anything starts sending it:
                # registerNode hides a child whose parent it does not know yet.
                # Depths and parents as utils/tree_hierarchy computes them.
                yield event({"type": "status", "server": "sub_agent_manager.spawn()", "phase": "start",
                             "request_id": f"{request_id}_sub_001", "message": "spawning", "meta": {},
                             "tree": {"parent_id": request_id, "depth_level": 1,
                                      "child_count": 1, "is_leaf": False}})
                yield event({"type": "status", "server": "sub_agent.coordinator", "phase": "start",
                             "request_id": f"{request_id}_sub_001_001",
                             "message": "sub-agent at work", "meta": {"step": 1},
                             "tree": {"parent_id": f"{request_id}_sub_001", "depth_level": 2,
                                      "child_count": 0, "is_leaf": True}})
                # An ORPHAN: its parent is never sent at all. This is the case that
                # decides whether a tree can be forwarded safely, because registerNode
                # hides a child whose parent it does not know and waits for it.
                yield event({"type": "status", "server": "orphan.worker()", "phase": "start",
                             "request_id": f"{request_id}_sub_002_001", "message": "no parent sent",
                             "meta": {},
                             "tree": {"parent_id": f"{request_id}_sub_002", "depth_level": 2,
                                      "child_count": 0, "is_leaf": True}})
                # TWO calls to the same tool that open NO status scope -- what an
                # unknown tool does. There is no row to hang them on, and only the
                # request id tells them apart.
                for nth, stamp in ((1, "19:15:22"), (2, "19:15:23")):
                    yield event({"type": "tool_call", "step": 2, "server": "datetime",
                                 "action": "datetime_now",
                                 "request_id": f"{request_id}_10{nth}",
                                 "params": {"tz": f"Europe/Berlin{nth}"}})
                    yield event({"type": "tool_result", "step": 2, "server": "datetime",
                                 "action": "datetime_now",
                                 "request_id": f"{request_id}_10{nth}",
                                 "result": {"status": "success", "current": stamp}})
                for delta in ("now I can ", "answer."):
                    yield event({"type": "reasoning_delta", "step": 2, "delta": delta})
                yield event({"type": "thinking", "step": 2, "assistant": {"content": "", "tool_calls": []}})
                yield event({"type": "thinking", "content": "Done"})  # the simplified one: no step
                yield event({"type": "final", "content": "Done"})
                # A scope that opens AFTER the answer, under a request id nothing has
                # seen yet -- what a session-end hook does. It belongs to the run: no
                # call is in flight any more. (The coordinator's own end does not test
                # this: it shares its start's request id, so addStatusEvent updates the
                # row where it already is, wherever that is.)
                yield event({"type": "status", "server": "lessons_learned.extract()", "phase": "start",
                             "request_id": f"{request_id}_009", "message": "after the answer", "meta": {},
                             "tree": {"parent_id": request_id, "depth_level": 1, "child_count": 0, "is_leaf": True}})
                yield event({"type": "status", "server": "coordinator", "phase": "end",
                             "request_id": request_id, "message": "completed (2 steps)", "meta": {}})
                yield event({"type": "end"})
                return
            if ending in ("closes", "stale", "ending", "answered", "saving"):
                return
            if ending in ("final-drops", "cancelled-drops"):
                yield event({"type": "final", "content": "Done"} if ending == "final-drops"
                            else {"type": "cancelled", "request_id": request_id, "step": 1})
                await asyncio.sleep(0.2)
            raise ConnectionAbortedError("the connection is cut off")  # the browser's reader fails mid-stream
        return recorded(request_id, stream())

    def refused_stream(after: float = 0):
        async def stream():  # a refusal the way app.py's event streams send one: `error`, and the stream closes
            yield ":ok\n\n"
            await asyncio.sleep(after)
            yield event({"type": "error", "error": "Session s-1 is currently locked by another request"})
            if after:  # refused by the run itself once the session lock did not come: its end follows
                yield event({"type": "end"})
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/events")
    async def reattach(request_id: str = "", session_id: str = "", catch_up: str = "", seen: str = ""):
        hits[f"catch-up:{request_id}"] = f"{catch_up or 'replay'}/{seen or '-'}"
        # As app.py does: only a job can be reconnected to, and a run without one
        # answers 409 -- which is what `attachable` in /api/sessions/active exists
        # to keep a client from walking into. Read from the same table the active
        # answer is built from, so the stub cannot say "attachable: false" here and
        # let the reconnect through anyway.
        if request_id in {v.rstrip("!~") for v in active_runs.values() if "!" in v}:
            raise HTTPException(status_code=409, detail="No background job for this request")
        status_of = "completed" if request_id == "r-live-over" else "running"

        async def stream():  # the server names the run first
            yield event({"type": "reconnect", "request_id": request_id, "session_id": session_id,
                         "status": status_of, "message": "Reconnected"})
            if request_id == "r-live-over":
                return  # the run had finished: the buffer is empty and the stream closes at once
            if request_id == "r-live-buffered":
                # Four events waited in the run's buffer; the client says how many of
                # them its session load already accounted for, and gets the rest.
                skip = int(seen) if (catch_up == "skip" and seen.isdigit()) else 0
                # each its own node in the status tree (a child id of the run), so four
                # events read as four lines rather than one line written over four times
                for n in range(skip + 1, 5):
                    yield event({"type": "status", "phase": "start", "message": f"buffered {n}",
                                 "request_id": f"{request_id}_{n:03d}"})
                await asyncio.sleep(3)
                return
            if request_id == "r-live-dropping":  # drops before the run's end
                await asyncio.sleep(0.5)
            elif request_id in ("r-live-closing", "r-live-stopping"):  # the run ends
                await asyncio.sleep(0.8 if request_id == "r-live-closing" else 1.5)
                yield event({"type": "end"})
            elif request_id == "r-live-final":  # the final answer, then the connection closes before the end
                await asyncio.sleep(0.5)
                yield event({"type": "final", "content": "Done"})
                await asyncio.sleep(0.5)
            elif request_id == "r-live-answered":  # the final answer, then the save and the hooks take 3 s
                await asyncio.sleep(0.5)
                yield event({"type": "final", "content": "Done"})
                await asyncio.sleep(3)
                yield event({"type": "end"})
            elif request_id == "r-live-saving":
                await asyncio.sleep(0.5)
                yield event({"type": "final", "content": "Done"})
                async for chunk in saving(session_id, request_id, request_goes_on=False):
                    yield chunk
            elif request_id == "r-live-cancelled":  # cancelled elsewhere -- another tab, the admin panel
                await asyncio.sleep(0.5)
                yield event({"type": "cancelled", "request_id": request_id, "step": 2})
                await asyncio.sleep(1.5)
            elif request_id == "r-live-failing":  # the run fails: its error, then its end
                await asyncio.sleep(0.5)
                yield event({"type": "error", "message": "Agent incomplete: max steps reached"})
                yield event({"type": "end"})
                await asyncio.sleep(0.5)
            elif request_id == "r-live-severed":  # its connection breaks as it is cancelled
                await asyncio.wait_for(cancel_asked(request_id).wait(), timeout=5)
            elif request_id == "r-live-honouring":  # honours a cancel at once: its cancelled event and end
                await asyncio.wait_for(cancel_asked(request_id).wait(), timeout=5)
                yield event({"type": "cancelled", "request_id": request_id, "step": 1})
                yield event({"type": "end"})
            elif request_id == "r-live-honouring-late":  # honours a cancel at its next step, a second later
                await asyncio.wait_for(cancel_asked(request_id).wait(), timeout=5)
                await asyncio.sleep(1)
                yield event({"type": "cancelled", "request_id": request_id, "step": 1})
                await asyncio.sleep(1.5)  # it saves its session before its end
                yield event({"type": "end"})
            else:
                await asyncio.sleep(5)  # the run goes on; the page usually leaves before it ends
        return recorded(request_id, stream())

    @app.post("/events")
    async def run(request: Request):
        body = await request.json()
        posted.append(body)
        ending = request.cookies.get("stub_stream", "")
        if ending in ("refused", "refused-late"):
            return refused_stream(1 if ending == "refused-late" else 0)
        started = {"drops": "r-dropped", "final-drops": "r-final-dropped", "cancelled-drops": "r-cancelled-dropped",
                   "late-drops": "r-late-dropped", "question-drops": "r-question-dropped", "closes": "r-closed",
                   "ending": "r-ending", "late-start-drops": "r-late-started", "answered": "r-answered",
                   "saving": "r-saving", "saving-drops": "r-saving-dropped", "reasons": "r-reasons",
                   "steps": "r-steps"}
        if ending in started:
            return started_stream(started[ending], body.get("session_id", ""), ending,
                                  start_after=1.5 if ending == "late-start-drops" else 0)
        return run_stream()

    @app.post("/run")
    async def run_with_files(request: Request):
        form = await request.form()
        stream = request.cookies.get("stub_stream")
        if stream == "refused":
            return refused_stream()
        session_id = str(form.get("session_id") or "s-files-new")
        if stream == "stale":
            return started_stream("r-files-stale", session_id, "stale", start_after=0.5)
        if stream == "final-drops":
            return started_stream("r-files-final-dropped", session_id, "final-drops", start_after=0.5)
        if stream in ("saving", "saving-drops"):
            return started_stream("r-files-saving" if stream == "saving" else "r-files-saving-dropped", session_id, stream,
                                  start_after=0.5)
        return started_stream("r-files-ended", session_id, "ending", start_after=0.5)

    asked: dict[str, asyncio.Event] = {}  # request id -> set once a cancel of it has been asked for

    def cancel_asked(request_id: str) -> asyncio.Event:
        return asked.setdefault(request_id, asyncio.Event())

    @app.post("/api/requests/{request_id}/cancel")
    async def cancel_run(request: Request, request_id: str, force: bool = False):
        key = f"cancel:{request_id}" + (":force" if force else "")
        hits[key] = hits.get(key, 0) + 1
        if request.cookies.get("stub_cancel") == "fails":
            raise HTTPException(status_code=500, detail="The job manager is unavailable")
        cancel_asked(request_id).set()  # the run sees its cancel before the answer comes
        if request.cookies.get("stub_cancel") in ("slow", "slower"):  # as a force cancel waits out its grace
            await asyncio.sleep(1.5 if request.cookies.get("stub_cancel") == "slow" else 6)
        if request_id == "r-live-stopping":  # answers once the run has ended, and another one has started
            await asyncio.sleep(3)
        hits[f"answered:{request_id}"] = hits.get(f"answered:{request_id}", 0) + 1
        if request_id in ("r-live-stopping", "r-ending"):
            return {"status": "not_found", "request_id": request_id}
        cancelled.add(request_id)
        return {"status": "cancelled", "request_id": request_id}

    @app.post("/chat/resolve")
    async def resolve_line(request: Request):  # every line the tests type with a slash is a chat command
        name, _, payload = (await request.json())["line"][1:].partition(" ")
        return {"kind": "command", "name": name, "payload": payload}

    @app.get("/__stub/posted")
    async def recorded_runs():
        return posted

    @app.get("/__stub/hits")
    async def recorded_hits():
        return hits

    @app.get("/__stub/streams")
    async def recorded_streams():
        return streams

    def held(payload: dict) -> StreamingResponse:
        """An answer held until POST /__stub/lists/release. Its headers go at once and it is not cacheable: the
        browser's cache lock would hold back the next request for the same URL until this answer is complete."""
        gate = asyncio.Event()
        held_lists.append(gate)

        async def body():
            await asyncio.wait_for(gate.wait(), timeout=10)
            yield json.dumps(payload).encode()
        return StreamingResponse(body(), media_type="application/json", headers={"Cache-Control": "no-store"})

    @app.get("/__stub/lists")
    async def held_list_count():
        return {"held": len(held_lists)}

    @app.post("/__stub/lists/release")
    async def release_lists():
        for gate in held_lists:
            gate.set()
        held_lists.clear()
        return {}

    @app.get("/__stub/deletes")
    async def recorded_deletes():
        return deletes

    @app.get("/auth/me")
    async def me(request: Request):
        return account(request)

    @app.patch("/auth/me")
    async def update_me(request: Request):
        account(request)
        patches.append(await request.json())
        return USER

    @app.post("/auth/logout")
    async def logout():
        hits["logout"] = hits.get("logout", 0) + 1
        response = JSONResponse({"message": "Logged out"})
        response.delete_cookie("stub_account")  # as the real logout drops its cookie
        return response

    @app.get("/__stub/patches")
    async def recorded_patches():
        return patches

    @app.post("/__stub/sessions")
    async def add_session(request: Request):
        sessions.append({**sessions[0], **await request.json()})
        return {}

    @app.post("/__stub/children")
    async def add_child(request: Request):
        child = await request.json()
        children[child.pop("parent")].append({**children["s-1"][0], **child})
        return {}

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "9.9.9", "uptime_seconds": 60, "python_version": "3.12", "packages": {}}

    @app.get("/chat/commands")
    async def chat_commands(agent: str = ""):
        hits[f"commands:{agent}"] = hits.get(f"commands:{agent}", 0) + 1  # the slash catalogue follows the agent
        return {"commands": [], "skills": [], "plugin_commands": []}

    @app.get("/agents")
    async def agents(request: Request):
        mode = request.cookies.get("stub_agents")
        if mode == "broken":
            raise HTTPException(status_code=500, detail="agents failed")
        if mode == "slow":
            await asyncio.sleep(1.5)
        # slow: the default is not the first row, so a picker waiting for the list has to find it
        default = {"nodefault": "missing", "slow": "writer"}.get(mode, "assistant")
        return {"agents": ["assistant", "writer"], "default": default,
                "details": [{"name": "assistant", "description": "General help", "category": "tools", "tags": ["chat"]},
                            {"name": "writer", "category": None, "tags": ["prose"],
                             "description": "Writes books from one request: plans the story, drafts every chapter and "
                                            "scene, then reviews and repairs the text until it reads well"}]}

    @app.get("/llm/profiles")
    async def profiles():
        return {"profiles": [
            {"name": "deep", "description": "Deep", "model_ref": "or-deep", "max_steps": 5,
             "provider": "openai_responses", "model": "deepseek/pro", "host": "openrouter.ai"},
            {"name": "default", "description": "Default", "model_ref": "m", "max_steps": 5,
             "provider": "openai_httpx", "model": "gpt-x", "host": None},
            {"name": "fast", "description": "Fast", "model_ref": "or-fast", "max_steps": 5,
             "provider": "openai_responses", "model": "google/flash", "host": "openrouter.ai"},
        ], "default": "default"}

    @app.get("/api/sessions/hierarchy")
    async def hierarchy(request: Request):
        listed = list(sessions)  # the list as it is when asked
        if request.cookies.get("stub_list") == "held":
            return held({"sessions": listed, "root_count": len(listed)})
        if request.cookies.get("stub_list") == "broken":
            raise HTTPException(status_code=502, detail="The server is restarting")
        return {"sessions": listed, "root_count": len(listed)}

    @app.get("/api/sessions")
    async def sessions_flat(request: Request):
        """The plain list the /sessions chat command reads. With
        ``stub_cmd_list=held`` it is held open until POST /__stub/lists/release --
        which is how a test keeps a slash command running while it does something
        else."""
        listed = list(sessions)
        if request.cookies.get("stub_cmd_list") == "held":
            return held(listed)
        return listed

    # Which sessions an agent is working in. Declared BEFORE /api/sessions/{session_id},
    # as in the real app: FastAPI matches in order, and the parameterised route would
    # take "active" for a session id.
    active_runs: dict[str, str] = {}

    @app.post("/__stub/active-runs")
    async def set_active_runs(payload: dict):
        """A test says which sessions are running; the pane and the chat both read it."""
        active_runs.clear()
        active_runs.update(payload or {})
        return {"ok": True}

    @app.get("/api/sessions/active")
    async def sessions_active(request: Request, ids: str = ""):
        hits["sessions-active"] = hits.get("sessions-active", 0) + 1
        if request.cookies.get("stub_active") == "broken":
            raise HTTPException(status_code=502, detail="the job registry is unavailable")
        if request.cookies.get("stub_active") == "slow":
            await asyncio.sleep(1.2)  # long enough for a session pick to start meanwhile
        wanted = [i for i in ids.split(",") if i][:200]  # the endpoint's own cap
        hits["sessions-active-ids"] = len(wanted)
        # A value of "<id>" is a job-backed run (attachable); "<id>!" is one the
        # server knows of but cannot be reconnected to -- a /run with files, a
        # sub-agent's run. A trailing "~" marks a run past its answer, which
        # cancel_session spares and so must whoever cancels from this answer.
        def entry(rid: str) -> dict:
            return {"request_id": rid.rstrip("!~"), "agent_name": "assistant",
                    "attachable": "!" not in rid, "answered": "~" in rid}
        return {"active": {sid: entry(active_runs[sid]) for sid in wanted if sid in active_runs}}

    @app.get("/api/sessions/{session_id}/children")
    async def session_children(request: Request, session_id: str):
        listed = list(children.get(session_id, []))  # the branch as it is when asked
        if request.cookies.get("stub_children") == "held":
            return held({"sessions": listed})
        return {"sessions": listed}

    @app.get("/api/sessions/{session_id}")
    async def session(request: Request, session_id: str, descendants: bool = False):  # off unless asked for, as the endpoint has it
        hits[f"session:{session_id}"] = hits.get(f"session:{session_id}", 0) + 1
        # The RAW query, not the parsed flag: what the delete path needs is that the
        # chat's load and the DELETE of the same session are the same URL, and
        # `?descendants=false` parses to exactly the same False as sending nothing.
        # Every query this session was loaded with, so a second load cannot hide a
        # first one that asked for more.
        key = f"session-query:{session_id}"
        hits[key] = hits.get(key, []) + [request.url.query]
        if checking:
            hits["session-while-checking"] = hits.get("session-while-checking", 0) + 1
        if session_id == "s-unsaved":
            await asyncio.sleep(1)
        found = next((s for s in [*sessions, *children["s-1"]] if s["session_id"] == session_id), None)
        if found is None:
            raise HTTPException(status_code=404)
        if not found.get("trickle"):
            await asyncio.sleep(found.get("delay", 0))
        if found.get("fails"):
            raise HTTPException(status_code=500, detail="The session file is unreadable")
        user_message = {"role": "user", "content": [
            {"type": "text", "text": "Build the kit"},
            # stored without their data
            {"type": "image", "name": "sketch.png"},
            {"type": "audio", "name": "briefing.wav"},
        ]} if found.get("attachments") else {"role": "user", "content": "Build the kit"}
        if found.get("run"):
            # Two LLM calls: one that reasoned and called a tool, one that answered.
            # The tool's answer is a JSON STRING, as a session file stores it, and it
            # carries a tag so the page cannot be rendering it as markup.
            messages = [
                {"role": "user", "content": "Read the readme"},
                {"role": "assistant", "content": "", "reasoning_content": "weighing the first move.",
                 "tool_calls": [{"id": "call_1", "type": "function",
                                 "function": {"name": "file_ops_read_file",
                                              "arguments": '{"filePath": "README.md"}'}}]},
                {"role": "tool", "tool_call_id": "call_1", "name": "file_ops_read_file",
                 "content": '{"status": "success", "note": "<b>not markup</b>"}'},
                {"role": "assistant", "content": "Done", "content_format": "text"},
            ]
        else:
            messages = [user_message,
                        {"role": "assistant", "content": "Done", "content_format": "text"}]
        stored = {**found, "llm_profile": "default", "created_at": now, "context_vars": {},
                  "messages": messages}
        if not found.get("trickle"):
            return stored

        async def body():  # a large session: the headers come at once, the body takes its time
            yield b" "
            await asyncio.sleep(found["trickle"])
            yield json.dumps(stored).encode()
        return StreamingResponse(body(), media_type="application/json")

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        doomed = next((s for s in sessions if s["session_id"] == session_id), {})
        await asyncio.sleep(doomed.get("delete_delay", 0))
        if doomed.get("delete_fails", 0) > 0:
            doomed["delete_fails"] -= 1
            raise HTTPException(status_code=500, detail="The session file is locked")
        deletes.append(session_id)
        sessions[:] = [s for s in sessions if s["session_id"] != session_id]
        return {}

    _serve_mutated_sources(app)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/ui", StaticFiles(directory=UI_TESTS), name="ui-tests")
    return app


@pytest.fixture(scope="module")
def results():
    return run_app_test_page(BROWSER, stub_app(), "tests/ui/shell_tests.html", timeout=PAGE_TIMEOUT)


EXPECTED = [
    'the shell starts for the owner when auth is off',
    'an empty message leaves the welcome in place',
    'the message form never reloads the page, not even before the chat has taken it over',
    'attaching files is a button the keyboard reaches',
    'a mangled pin or recent list leaves the launcher working',
    'the launcher lists the catalogue by category under its button',
    'instances of one plugin fold into one launcher row, lined up with the others, and only the instances indent',
    'the launcher search matches description and keywords',
    'a panel opens docked and the host answers pk:ready with pk:init',
    "a panel's dialog is shown over the whole app",
    "a panel's title and toast reach the shell",
    'a second docked panel takes the front and the first is told it is hidden',
    'a dock tab is chosen with the keyboard',
    'a framed kit panel leaves its title to the tab and keeps its content',
    'a docked panel detaches into a window and docks back',
    'a window gets its size back once the browser window grows again',
    'loading a session names it in the header and tells the panels',
    'an open branch of the session tree stays open and current when the list refreshes',
    'a session started from the chat is named once the server has named it',
    'the last session clicked wins, not the last answer',
    'a session still loading does not replace a newer choice',
    'clicking the open session during a run does not stop the run',
    'deleting the open session asks about its run before anything is deleted',
    'the open session is deleted once its run has stopped, and the welcome does not offer it',
    'the start page forgets a session deleted while it shows',
    'a session list asked for before a delete and answered after it does not bring the session back',
    'deleting a session is a choice: a pick after it wins, a pick of it does not stay open',
    'a delete that fails takes nothing from a session on its way',
    'a load of the open session still on its way does not open it again once it is deleted',
    'a message sent while the open session is being deleted does not bring it back',
    'a note written while the open session is being deleted stays',
    'after a failed delete of the open session, deleting it again leaves the chat alone',
    'while the run of a session being deleted stops, a pick elsewhere and a message there keep their place and a message keeps the session',
    'a stream naming the open session while its delete asks about the run does not keep the session',
    'a session opened by a load on its way while its delete runs takes no message',
    'after New, deleting the session left behind leaves the chat alone',
    'a message sent while a pick is on its way stays when that pick is deleted',
    '/new during a run opens the new session and leaves the run going',
    'a slash command that ends while a newer line resolves leaves that line its guard against a second Send',
    'files sent while a request runs stay attached, and so does their message',
    'a message sent before the running request has started stays in the input',
    'a run whose stream was cut off is left to a reload, which follows it again',
    'a run whose stream was cut off is let go once the chat shows another session',
    'a reattached run whose stream drops is left to a reload',
    'a run that ends -- a final answer, a cancel, an error, a stream that closes -- is forgotten without a connection notice, and offers no Stop and takes no message while its stream stays open',
    'a run with files shows its answer, is not stored for a reload and leaves the stored run alone; files it did not send stay attached, and a cut after its answer is no failure',
    'after a reload a stored run that has ended is forgotten, one the server could not tell about stays stored',
    'Stop leaves the end of a run to its stream and holds the messages sent meanwhile; a late answer leaves the next run alone',
    'deleting the open session whose run loses its stream while it stops cancels the run and goes',
    'deleting a session cancels the run it asks about: one named and cut off while the run question is open is cancelled, one past its answer -- asked about or not -- is not',
    'a session being deleted does not open again',
    'leaving a session during its run: a pick or New takes the chat at once and leaves the run working, the last click wins, and a message goes to the session it was typed in',
    'a run let go of past its answer is read to its end, and its save shows in the session list: a message, one with files beside a new run, and a run followed again after a reload',
    'a run let go of whose stream breaks while it saves leaves the session list alone: a message, and one with files',
    'picks while a run goes on: each takes the chat at once, the last wins, and none of them stops the run',
    'a session that works on after its run ends is followed without a reload',
    'a run works on while another session is read, and the chat picks it up again on return',
    'leaving a session with a file run does not cut that run short',
    'a delete finds the run of a session that is running somewhere else',
    'a run the chat cannot follow is marked but not attached to',
    'coming back shows what the run sent while away, and shows it once',
    'a run that ended while the viewer was away is not reported as a lost connection',
    'the session of a run being read for its end is not attached to a second time',
    'a click on another session is not swallowed by an attach that answers late',
    'a run past its answer is left alone when its session is deleted',
    'a failed activity poll leaves the marks as they were',
    'a run asked to stop is still known as stopping after a session switch',
    'the chat loads a session at the same URL the delete uses',
    'the sessions pane marks the sessions an agent is working in',
    'switching away never stops the run, whatever its stream is doing; a delete after a cancel asks nothing',
    'deleting the session of a run whose connection was lost cancels that run first',
    'a restored message names image and audio parts stored without their data',
    'the theme button cycles the theme and every panel follows',
    'an open settings panel shows the theme chosen in the header',
    'the palette finds a panel and opens it',
    'Ctrl+K in the open palette starts its search afresh',
    'the palette does not open over an open question',
    'the system panel asks for a tab it may not show only once',
    'a cancel on its way stays disabled when the request list is drawn anew',
    'the palette lists instances of one plugin once and narrows to them',
    'in the palette a name stays whole beside a long description, and a long name leaves its hint room',
    'a session offers the panels that open on a session',
    'the session panel is the one that asks for the sub-session tree',
    'the thinking box carries the reasoning, and not the answer a second time',
    'collapse all closes every open branch and keeps the focus it was pressed with',
    'each LLM call keeps its own reasoning, its own tool lines, and folds when the next one starts',
    'a sub-agents lines nest under the call that spawned it, none go missing, and what was still running when the run ended says so',
    'clicking a tool status line unfolds what it was asked and what it answered, as one block per call',
    'a session read back from disk brings its run with it, not just the answer',
    'a session panel pinned from a link can follow the chat again',
    'a request id in the chat offers the panels that take a request',
    'a panel with unsaved input is only closed or reloaded once the viewer agrees',
    'the plugin panels follow the session the chat switches to, not a slower answer for the one before, and New',
    'a docked panel leaves the header on screen at laptop widths',
    'a stored session that is gone starts a new one',
    'a session picked while the shell restores the stored one wins',
    '/new while the stored session is on its way leaves nothing of it to continue',
    'a message sent while the stored session is on its way stays in the chat',
    'a pick that fails before the stored session is restored leaves a start page',
    'a stored session deleted while the shell restores it does not stay open',
    'a run still going after a reload keeps the chat and names its session',
    'while it checks whether a run is still going after a reload, the shell asks the server for no session',
    'a session picked while a run reattaches after a reload does not take the chat from the run',
    'a message to a run reattached after a reload goes to that run',
    'a message a running request does not take goes back into the input',
    'a message to a running request that has just finished comes back into the input',
    'a message a run takes only after its stream has ended leaves the controls idle',
    'while it checks whether a run is still going after a reload, the composer is held',
    'a read-only session picked while a run reattaches after a reload leaves the run writable',
    'the layout comes back after a reload',
    'the sub-agent panel shows the session the shell restores after a reload',
    'from the launcher a panel a link sent somewhere starts over, one that went there itself stays',
    'on a narrow screen the dock steps aside for the chat and comes back',
    'on a narrow screen restored windows wait behind the chat',
    'on a narrow screen the sessions sheet steps aside for the session picked',
    'on a narrow screen a sheet opened while the shell starts stays open when the panels come back',
    'on a narrow screen a window fills the screen: it is not dragged and takes its size on a wide one',
    "the kit page's theme buttons switch the whole shell",
    'a new window takes the first free step down, not the count of windows',
    'the docked tabs sort by drag and drop and by Shift+arrow, keep their order after a reload, and a cancelled drag changes nothing',
    'the agent and profile pickers open on the current choice, search, group by a remembered grouping and pick by click or keyboard',
    'a picker says when its list is on its way and shows it when it comes, says when it failed, and falls back only to what it lists',
    'the sessions pane takes the width it was dragged or keyed to, keeps it after a reload, and stays within its bounds',
    'closing the last panel hides the dock',
    'a mangled stored layout does not stop the shell',
    'a failing catalogue leaves the chat working',
    'a signed-in user sees their account and can save the profile',
    'logging out forgets the layout of the user who left',
    'an expired or deactivated sign-in goes to the login page and comes back',
    'a server failure at start is shown, not a dead page',
    'the login page returns only to this site',
    'without authentication the login page lets the owner straight in',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_shell(results, name):
    assert results.get(name) == "ok", results


def test_every_check_the_page_ran_is_one_this_list_knows(results):
    """The list is what turns a check into a test. A check named here but gone from
    the page fails loudly (nothing answers for it); one added to the page and not
    added here ran and was never looked at -- it could have been failing for weeks.
    Six were in exactly that state once, which is why this is here."""
    unexpected = sorted(set(results) - set(EXPECTED))
    assert not unexpected, f"checks the page ran that EXPECTED does not name: {unexpected}"
