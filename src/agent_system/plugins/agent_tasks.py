"""AI tasks of a plugin panel: which work an agent may do, whether a person chose to let it, and starting it.

A plugin names its tasks -- the work an agent can take on, each with a default agent -- and keeps a person's choice
per task in its own SQLite file: ``person`` (the panel offers no agent) or ``ai`` (the panel offers to start the chosen
agent). Work only a person may do is named too (``PersonOnly``), so the panel can say so instead of staying silent.

An agent is started the way the chat starts one: a run on ``/run`` in a session of its own, under the identity of
the person who clicked (their cookie or key is passed on), so every gate of /run applies -- auth, the agent's role
gate, the LLM permission. The run goes on in the background. The panel follows it where the person is: it asks for
the run (``GET {base}/ai/runs/{session_id}``) -- its state and what the agent said and did so far, read from the
session as the person may read it -- and shows that beside the work, with no need to switch to the chat. A person
answers the agent there too (``POST {base}/ai/runs/{session_id}/reply``): the same session goes on with their text.

Each run is kept in the plugin's SQLite file next to the choices, so a restart keeps the list. A run of an earlier
process that was still running when it ended reads as ``interrupted``: runs go on in the process that started them.

Every run also tells what it is doing as run events (``agent_system.run_events``): started, a heartbeat while it
works, its end -- and the steps a hook plugin reports for it. ``GET /api/agent-runs/stream`` streams
them; the panel no longer has to wait for the end to know the agent is alive.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from agent_system.chat_actions import last_answer, message_role, message_text, one_line, tool_call_summary, tool_calls_of
from agent_system.llm.tls import httpx_verify
from agent_system.plugins.sqlite_guard import connection
from agent_system.run_events import RunReporter
from agent_system.utils.id import short_id

logger = logging.getLogger(__name__)

MODES = ("person", "ai")
#: Request headers that carry who is asking; passed on to /run, nothing else.
_IDENTITY = ("cookie", "authorization", "x-api-key")
#: Runs started here, kept referenced until they end (a bare task may be collected while it runs).
_RUNNING: set[asyncio.Task] = set()
#: The sessions a run of this process is working on now.
_LIVE: set[str] = set()
#: The run events that end a run, kept referenced until written.
_ENDING: set[asyncio.Task] = set()
#: The request id of each run of this process at work, by session: what a cancel names (/api/requests/{id}/cancel).
_REQUESTS: dict[str, str] = {}
#: The job of each run of this process at work, by session: cancelled itself when no job of the server knows the id.
_JOBS: dict[str, asyncio.Task] = {}
#: Who asked to cancel a run, by session, until its end is recorded.
_CANCELLED_BY: dict[str, str] = {}
#: This process, as the runs it started name it: a run of another one still marked running was cut off with it.
PROCESS = uuid.uuid4().hex
#: The runs a plugin keeps, the newest; older ones are dropped.
MAX_RUNS = 200
RUN_FIELDS = ("session_id", "task", "agent", "title", "who", "started", "ended", "state", "error", "summary",
              "process", "turns")
#: The steps of a run shown at most, the newest; a step's text at most this long.
MAX_STEPS, MAX_TEXT = 200, 4000
#: How long a start waits for a run refused at once, in seconds (tests set it shorter).
START_WAIT = 3.0
#: The headings that end the answer of a run whose work a person checks: the panel shows each part on its own
#: ("what was changed", "what could go wrong", "how it was checked"), so the person can judge it without the code.
REVIEW_SECTIONS = ("### Changed", "### Could go wrong", "### Checked")


def review_note(language: str) -> str:
    """What a review task's text asks at its end: three short sections in plain words, in the panel's language."""
    tongue = "German" if language.lower().startswith("de") else "English"
    return ("\n\nWhen you are done, end your answer with exactly these three sections, in " + tongue + ", each a few "
            "short sentences a person who is not a security expert understands: '" + REVIEW_SECTIONS[0] + "' (what you "
            "recorded or changed, and where it shows), '" + REVIEW_SECTIONS[1] + "' (what could be wrong or break "
            "because of it, and how likely that is), '" + REVIEW_SECTIONS[2] + "' (how you checked it). Keep the "
            "headings in English exactly as written.")


@dataclass(frozen=True)
class AgentTask:
    id: str
    title: str
    what: str            # what the agent does, in one or two sentences
    agent: str           # the default agent
    target: str = ""     # what one run works on, e.g. "finding"; "" when it works on everything at once
    batch: bool = False  # it can also run on everything open at once (the panel offers that in its AI card)
    review: bool = False  # what the agent records waits for a person to check it: its run ends "needs you"


@dataclass(frozen=True)
class PersonOnly:
    id: str
    title: str
    why: str


class AiSettings:
    """A person's choice per task -- mode and agent -- in a table of the plugin's own SQLite file."""

    def __init__(self, path: Path, tasks: tuple[AgentTask, ...]):
        self.path, self.tasks = path, {task.id: task for task in tasks}
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, connection(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS ai_tasks (task TEXT PRIMARY KEY, mode TEXT NOT NULL, agent TEXT NOT NULL, "
                       "changed TEXT, changed_by TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS ai_runs (session_id TEXT PRIMARY KEY, task TEXT NOT NULL, "
                       "agent TEXT NOT NULL, title TEXT NOT NULL, who TEXT NOT NULL, started TEXT NOT NULL, ended TEXT, "
                       "state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', "
                       "process TEXT NOT NULL, turns INTEGER NOT NULL DEFAULT 1)")

    # ── the runs ──────────────────────────────────────────────────────────────

    def save_run(self, run: dict[str, Any]) -> None:
        """Keep a run as it stands (started, gone on, ended); the oldest beyond MAX_RUNS go."""
        values = tuple(run.get(key) if run.get(key) is not None else ("" if key in ("error", "summary") else None)
                       for key in RUN_FIELDS)
        with self._lock, connection(self.path) as db:
            db.execute(f"INSERT OR REPLACE INTO ai_runs ({', '.join(RUN_FIELDS)}) VALUES "
                       f"({', '.join('?' for _ in RUN_FIELDS)})", values)
            db.execute("DELETE FROM ai_runs WHERE session_id NOT IN "
                       "(SELECT session_id FROM ai_runs ORDER BY started DESC LIMIT ?)", (MAX_RUNS,))

    @staticmethod
    def _shown(row: tuple) -> dict[str, Any]:
        run = dict(zip(RUN_FIELDS, row))
        # still marked running, but not by a run of this process: its process ended under it
        if run["state"] == "running" and (run["process"] != PROCESS or run["session_id"] not in _LIVE):
            run["state"] = "interrupted"
        return run

    def run(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, connection(self.path) as db:
            row = db.execute(f"SELECT {', '.join(RUN_FIELDS)} FROM ai_runs WHERE session_id = ?", (session_id,)).fetchone()
        return self._shown(row) if row else None

    def runs(self, who: str, limit: int = 20) -> list[dict[str, Any]]:
        """The runs ``who`` started, newest first."""
        with self._lock, connection(self.path) as db:
            rows = db.execute(f"SELECT {', '.join(RUN_FIELDS)} FROM ai_runs WHERE who = ? ORDER BY started DESC, rowid DESC "
                              "LIMIT ?", (who, limit)).fetchall()
        return [self._shown(row) for row in rows]

    def get(self, task_id: str) -> dict[str, Any]:
        task = self.tasks[task_id]
        with self._lock, connection(self.path) as db:
            row = db.execute("SELECT mode, agent, changed, changed_by FROM ai_tasks WHERE task = ?", (task_id,)).fetchone()
        mode, agent, changed, changed_by = row or ("person", task.agent, None, None)
        return {**asdict(task), "default_agent": task.agent, "agent": agent, "mode": mode,
                "changed": changed, "changed_by": changed_by}

    def all(self) -> list[dict[str, Any]]:
        return [self.get(task_id) for task_id in self.tasks]

    def set(self, task_id: str, mode: str | None, agent: str | None, now: str, who: str) -> dict[str, Any]:
        if task_id not in self.tasks:
            raise HTTPException(status_code=404, detail=f"No AI task {task_id}")
        if mode is not None and mode not in MODES:
            raise HTTPException(status_code=422, detail=f"mode must be one of {', '.join(MODES)}")
        known = self.get(task_id)
        agent = (agent or "").strip() or known["agent"]
        with self._lock, connection(self.path) as db:
            db.execute("INSERT INTO ai_tasks (task, mode, agent, changed, changed_by) VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT(task) DO UPDATE SET mode = excluded.mode, agent = excluded.agent, "
                       "changed = excluded.changed, changed_by = excluded.changed_by",
                       (task_id, mode or known["mode"], agent, now, who))
        return self.get(task_id)

    def overview(self, person_only: tuple[PersonOnly, ...] = ()) -> dict[str, Any]:
        return {"tasks": self.all(), "person_only": [asdict(item) for item in person_only]}

    def ready(self, task_id: str) -> dict[str, Any]:
        """The task, refused unless a person chose to let an agent do it."""
        if task_id not in self.tasks:
            raise HTTPException(status_code=404, detail=f"No AI task {task_id}")
        setting = self.get(task_id)
        if setting["mode"] != "ai":
            raise HTTPException(status_code=409, detail=f"{setting['title']} is set to be done by a person; "
                                                        "switch it to AI in the panel first")
        return setting


def _detail(answer: httpx.Response) -> str:
    try:
        return str(answer.json().get("detail", answer.text))
    except (ValueError, AttributeError):
        return answer.text


def _outcome(run: dict[str, Any], done: asyncio.Task) -> None:
    """Record how a run ended: its answer from /run, or the exception that ended it."""
    if done.cancelled():
        run.update(state="cancelled")
    elif done.exception() is not None:
        run.update(state="failed", error=str(done.exception())[:MAX_TEXT])
    elif done.result().status_code >= 400:
        run.update(state="refused", error=_detail(done.result())[:MAX_TEXT])
    else:
        try:
            result = done.result().json()
        except ValueError:
            result = {}
        result = result if isinstance(result, dict) else {}
        errors = [str(e) for e in result.get("errors") or [] if e]
        if result.get("refused"):
            run.update(state="refused", error=(errors[0] if errors else str(result["refused"]))[:MAX_TEXT])
        elif result.get("cancelled"):
            run.update(state="cancelled")
        elif errors and not result.get("summary"):
            run.update(state="failed", error=errors[0][:MAX_TEXT])
        else:
            run.update(state="done", summary=str(result.get("summary") or "")[:MAX_TEXT])
    run["ended"] = _now()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def report_end(reporter: RunReporter, run: dict[str, Any], review: bool = False, check_in: str = "") -> None:
    """The run event that ends a run, from how ``_outcome`` recorded its end; a task whose work a person checks
    (``review``) ends waiting for them."""
    state = run.get("state")
    if state == "done" and review:
        await reporter.review_ready(str(run.get("summary") or ""), check_in)
    elif state == "done":
        await reporter.done(str(run.get("summary") or ""))
    elif state == "cancelled":
        await reporter.cancelled(str(run.get("cancelled_by") or ""))
    elif state == "refused":
        await reporter.failed(f"refused: {run.get('error') or 'no reason given'}")
    else:
        await reporter.failed(str(run.get("error") or "the run ended without an answer"))


async def launch(request: Request, agent: str, task: str, title: str, wait: float | None = None, *,
                 store: AiSettings | None = None, task_id: str = "", who: str = "",
                 session_id: str = "", record: dict[str, Any] | None = None, source: str = "",
                 review: bool = False, check_in: str = "") -> dict[str, Any]:
    """Start ``agent`` on ``task`` as the person who asked -- in a new session, or going on in ``session_id`` -- and
    answer the session id.

    Waits up to ``wait`` seconds: a run refused at once (no such agent, no permission, no model) is answered as that
    refusal; one still running after that goes on in the background. With a ``store`` the run is kept there as it
    starts and as it ends, for the panel to follow (``record``: the run it goes on with). Either way it says what it
    does as run events (``source``: the plugin that started it); a ``review`` task ends waiting for its person to
    check the work (``check_in``: where). The run has a request id of its own, so ``cancel_run`` can stop it.
    A session a run of this process is still working in is refused (409): one run per session at a time.
    """
    wait = START_WAIT if wait is None else wait
    if session_id and session_id in _LIVE:
        raise HTTPException(status_code=409, detail="the agent is still at work in this session")
    session_id = session_id or short_id()
    record = {**(record or {"session_id": session_id, "task": task_id, "agent": agent, "title": title[:120],
                            "who": who, "started": _now(), "turns": 0}),
              "ended": None, "state": "running", "error": "", "summary": "", "process": PROCESS}
    record["turns"] = int(record.get("turns") or 0) + 1
    _LIVE.add(session_id)
    if store is not None:
        await asyncio.to_thread(store.save_run, record)
    reporter = RunReporter(session_id, agent, who=str(record.get("who") or who or "local"), title=record["title"],
                           source=source, task=str(record.get("task") or task_id))
    await reporter.started()
    reporter.keep_alive()
    headers = {name: value for name, value in request.headers.items() if name.lower() in _IDENTITY}
    if review:
        task += review_note(request.headers.get("accept-language", ""))
    request_id = f"panel-{session_id}-{record['turns']}"
    _REQUESTS[session_id] = request_id
    body = {"task": task, "session_id": session_id, "agent_name": agent, "session_title": title[:120],
            "request_id": request_id}

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=request.app)
        # In-process (ASGI): no TLS on the way, verify only keeps the client off a context of its own.
        async with httpx.AsyncClient(transport=transport, base_url="http://scarabhive.internal", timeout=None,
                                     verify=httpx_verify(True)) as client:
            return await client.post("/run", json=body, headers=headers)

    job = asyncio.create_task(run(), name=f"agent-task-{agent}-{session_id}")
    _RUNNING.add(job)
    _JOBS[session_id] = job

    def finished(done: asyncio.Task) -> None:
        _RUNNING.discard(done)
        _LIVE.discard(session_id)
        if _JOBS.get(session_id) is done:
            del _JOBS[session_id]
        if _REQUESTS.get(session_id) == request_id:
            del _REQUESTS[session_id]
        _outcome(record, done)
        ending = asyncio.ensure_future(report_end(reporter, {**record, "cancelled_by": _CANCELLED_BY.pop(session_id, "")},
                                                  review=review, check_in=check_in))
        _ENDING.add(ending)
        ending.add_done_callback(_ENDING.discard)
        if store is not None:
            try:
                store.save_run(record)
            except Exception:  # the run's end is logged below either way; the list just misses it
                logger.exception("the end of agent task %s in session %s was not kept", agent, session_id)
        if done.cancelled():
            return
        if done.exception() is not None:
            logger.error("agent task %s in session %s failed: %s", agent, session_id, done.exception())
        elif done.result().status_code >= 400:
            logger.warning("agent task %s in session %s refused: %s", agent, session_id, done.result().text[:300])

    job.add_done_callback(finished)
    try:
        answer = await asyncio.wait_for(asyncio.shield(job), timeout=wait)
    except asyncio.TimeoutError:
        return {"session_id": session_id, "agent": agent, "state": "running"}
    if answer.status_code >= 400:
        raise HTTPException(status_code=answer.status_code if answer.status_code < 500 else 502,
                            detail=f"{agent} could not start: {_detail(answer)}")
    return {"session_id": session_id, "agent": agent, "state": "done"}


async def cancel_run(session_id: str, who: str) -> bool:
    """Stop the run of this process working in ``session_id``, as the app stops any run (the background job of its
    request id), else its job itself; False when no run of this process works there."""
    request_id = _REQUESTS.get(session_id)
    if request_id is None:
        return False
    _CANCELLED_BY[session_id] = who
    from agent_system.services.background_job_manager import get_background_job_manager

    if await get_background_job_manager().cancel_job(request_id, force_timeout=5.0):
        return True
    job = _JOBS.get(session_id)
    if job is None or job.done():
        _CANCELLED_BY.pop(session_id, None)
        return False
    job.cancel()
    return True


def steps(messages: list[Any]) -> list[dict[str, str]]:
    """What a run's messages show a person: the task, the agent's words, each tool it called and what came back.
    The system prompt stays out; long texts are cut."""
    out: list[dict[str, str]] = []
    for message in messages:
        role, text = message_role(message), message_text(message).strip()
        if role in ("user", "developer") and text:
            # the first is the task the panel gave; every later one a person's answer from the panel or the chat
            asked = any(step["kind"] == "task" for step in out)
            out.append({"kind": "reply" if asked else "task", "text": text[:MAX_TEXT]})
        elif role == "assistant":
            if text:
                out.append({"kind": "text", "text": text[:MAX_TEXT]})
            for call in tool_calls_of(message):
                name, arguments = tool_call_summary(call)
                out.append({"kind": "tool", "name": name, "text": one_line(arguments, 160) if arguments else ""})
        elif role == "tool":
            name = message.get("name") if isinstance(message, dict) else getattr(message, "name", "")
            failed = '"status": "error"' in text or text.lower().startswith("error")
            out.append({"kind": "error" if failed else "result", "name": str(name or ""), "text": one_line(text, 300)})
    return out[-MAX_STEPS:]


async def read_session(request: Request, session_id: str) -> list[Any]:
    """The messages of a session as the asking person may read them -- the live ones while a run holds it; [] while
    it is not saved yet."""
    headers = {name: value for name, value in request.headers.items() if name.lower() in _IDENTITY}
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://scarabhive.internal", timeout=30,
                                 verify=httpx_verify(True)) as client:
        answer = await client.get(f"/api/sessions/{session_id}", headers=headers)
    if answer.status_code == 404:
        return []
    if answer.status_code >= 400:
        raise HTTPException(status_code=answer.status_code, detail=_detail(answer))
    return list((answer.json() or {}).get("messages") or [])


async def user_of(request: Request) -> str:
    """The signed-in user's name for the records; 'local' when the app runs without auth."""
    auth = getattr(getattr(request.app.state, "config", None), "auth", None)
    if auth is None or not auth.enabled:
        return "local"
    from agent_system.auth.database import get_db
    from agent_system.auth.dependencies import bearer_scheme, get_optional_user

    user = await get_optional_user(request, await bearer_scheme(request), request.headers.get("X-API-Key"), get_db())
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user.username


async def admin_of(request: Request) -> str:
    """The signed-in user's name, refused (403) unless they are an admin; 'local' when the app runs without auth
    (one user, its owner)."""
    auth = getattr(getattr(request.app.state, "config", None), "auth", None)
    if auth is None or not auth.enabled:
        return "local"
    from agent_system.auth.database import get_db
    from agent_system.auth.dependencies import bearer_scheme, get_optional_user
    from agent_system.auth.models import UserRole

    user = await get_optional_user(request, await bearer_scheme(request), request.headers.get("X-API-Key"), get_db())
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user.username


class AiChange(BaseModel):
    mode: str | None = None
    agent: str | None = Field(default=None, max_length=100)


class AiRun(BaseModel):
    target: str = Field(default="", max_length=100)


class AiReply(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class AiTaskRoutes:
    """The panel routes of the AI tasks, for a web handler class whose ``server`` has ``ai()`` (its AiSettings),
    ``ai_prompt(task_id, target, agent)`` -> (session title, task text) (raises LookupError for an unknown target),
    ``PERSON_ONLY`` and ``now()``. The plugin's schema.yaml names them: GET /ai -> get_ai, POST /ai/{task} -> post_ai,
    POST /ai/{task}/run -> post_ai_run, GET /ai/runs -> get_ai_runs, GET /ai/runs/{session_id} -> get_ai_run,
    POST /ai/runs/{session_id}/reply -> post_ai_reply."""

    server: Any

    async def get_ai(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.server.ai().overview, self.server.PERSON_ONLY)

    async def post_ai(self, task: str, body: AiChange, request: Request) -> dict[str, Any]:
        """A task's mode and agent, for everyone who opens the panel: admins only -- whoever starts the task later
        runs the agent chosen here under their own rights."""
        who = await admin_of(request)
        return await asyncio.to_thread(self.server.ai().set, task, body.mode, body.agent, self.server.now(), who)

    async def post_ai_run(self, task: str, body: AiRun, request: Request) -> dict[str, Any]:
        setting = await asyncio.to_thread(self.server.ai().ready, task)
        if setting["target"] and not body.target and not setting["batch"]:
            raise HTTPException(status_code=422, detail=f"name the {setting['target']} to work on")
        try:
            title, text = await asyncio.to_thread(self.server.ai_prompt, task, body.target.strip(), setting["agent"])
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await launch(request, setting["agent"], text, title, store=self.server.ai(), task_id=task,
                            who=await user_of(request), source=str(getattr(self.server, "name", "") or ""),
                            review=bool(setting.get("review")), check_in=setting["title"])

    async def _own_run(self, session_id: str, request: Request) -> dict[str, Any]:
        run = await asyncio.to_thread(self.server.ai().run, session_id)
        if run is None or run["who"] != await user_of(request):
            raise HTTPException(status_code=404, detail=f"No run {session_id}")
        return run

    async def get_ai_runs(self, request: Request) -> dict[str, Any]:
        """The runs this person started from this plugin's panel, newest first."""
        return {"runs": await asyncio.to_thread(self.server.ai().runs, await user_of(request))}

    async def get_ai_run(self, session_id: str, request: Request) -> dict[str, Any]:
        """One run with what its agent said and did so far, and its answer once it is done."""
        run = await self._own_run(session_id, request)
        messages = await read_session(request, session_id)
        answer = last_answer(messages) if run["state"] != "running" else ""
        return {**run, "steps": steps(messages), "answer": (answer or run["summary"])[:MAX_TEXT * 4]}

    async def post_ai_reply(self, session_id: str, body: AiReply, request: Request) -> dict[str, Any]:
        """A person's answer to the agent: the run's session goes on with it, the same agent, as the same person --
        while the task is still one a person lets an agent do."""
        run = await self._own_run(session_id, request)
        if run["state"] == "running":
            raise HTTPException(status_code=409, detail="the agent is still at work; answer once it has stopped")
        setting = await asyncio.to_thread(self.server.ai().ready, run["task"])
        return await launch(request, run["agent"], body.text.strip(), run["title"], store=self.server.ai(),
                            session_id=session_id, record=run, source=str(getattr(self.server, "name", "") or ""),
                            review=bool(setting.get("review")), check_in=setting["title"])
