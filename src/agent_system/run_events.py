"""Run events: what an agent run tells a person while it works -- its state, the step it is on in plain words, and a
sign of life every few seconds.

A *run* is one job given to an agent (in the panels: one AI task, see ``plugins/agent_tasks.py``); its id is the
session id the run works in. Everything a run says is an *event*: a flat record with the run's id, a gap-free
sequence number per run, the state the run is in afterwards and -- while it works -- the step (index, total, a label
a layperson understands). The last event of a run is enough to draw it; the whole list is its timeline.

Events are kept before they are sent (``RunEventStore``, one SQLite file in the data directory): a panel opened late,
a reload or a dropped stream reads on from the store. Live they go out through ``RunEventBus`` to every open stream
(``api/run_event_routes.py``: ``GET /api/agent-runs/stream``, Server-Sent Events).

``RunReporter`` is the one way to write them. It numbers the steps, keeps a run alive with ``run.heartbeat`` while
nothing else is said (``HEARTBEAT_S``) and ends a run exactly once. Whoever starts a run holds its reporter;
``reporter_for(session_id)`` finds it again (a hook plugin can turn tool calls into steps with it).

States (``STATES``): queued, working, needs_input, review, done, handed_over, cancelled, failed -- the last four end
a run. A run that is not ended and has said nothing for ``SILENT_S`` seconds is shown ``silent``: it hangs, or its
process stopped. Each run names the process that reports it (``owner``); a store opened by a later process ends the
runs whose process is gone as ``failed`` (``sweep``), so a restart leaves none at work for good. A heartbeat only
marks the run alive: it goes out live and is not kept as an event. ``needs_input``: the agent asked its person something (``ask_user`` in a panel run) and
waits; the run's view carries the ``question`` until ``run.answered`` puts it back to work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from agent_system.paths import data_path
from agent_system.plugins.sqlite_guard import connection

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STATES = ("queued", "working", "needs_input", "review", "done", "handed_over", "cancelled", "failed")
FINAL = frozenset({"done", "handed_over", "cancelled", "failed"})
#: The event types and the state each sets; None keeps the state as it is.
TYPES: dict[str, str | None] = {
    "run.queued": "queued",
    "run.started": "working",
    "run.step": "working",
    "run.heartbeat": None,
    "run.needs_input": "needs_input",
    # the question was answered (or its wait ended): the run is at work again
    "run.answered": "working",
    "run.review_ready": "review",
    "run.done": "done",
    "run.handed_over": "handed_over",
    "run.cancelled": "cancelled",
    "run.failed": "failed",
    # a person has seen a run that needed them (a failure, a handover): it stays as it ended, no longer asks
    "run.acknowledged": None,
}
#: The states in which a run is at work: only these can go quiet.
ACTIVE = frozenset({"queued", "working"})
#: A run that says nothing else this long gets a heartbeat, in seconds.
HEARTBEAT_S = 5.0
#: A run not ended that has said nothing this long is shown silent, in seconds.
SILENT_S = 30.0
#: A step's label at most this long (a layperson reads it at a glance); longer ones are cut.
MAX_LABEL = 80
#: Texts in a payload (a summary, a reason) at most this long.
MAX_TEXT = 4000
#: The runs kept, the newest; older ones go with their events.
MAX_RUNS = 500
#: Files or objects named in one event at most.
MAX_TOUCHING = 5

RUN_FIELDS = ("run_id", "agent", "who", "title", "source", "task", "state", "started", "updated", "ended",
              "last_seq", "step_index", "step_total", "step_label", "step_key", "step_labels", "progress", "eta_s", "acked",
              "owner")
#: The states a run is in while its process holds it: a run in one of them whose process is gone was cut off.
HELD = frozenset({"queued", "working", "needs_input"})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event_id() -> str:
    """Sortable by time, unique: nanoseconds then random bits, both hex."""
    return f"evt_{time.time_ns():016x}{os.urandom(4).hex()}"


def _process_key(pid: int) -> str:
    """A process as a run names its owner: its id and start time (an id alone is reused)."""
    try:
        return f"{pid}:{psutil.Process(pid).create_time():.3f}"
    except (psutil.Error, OSError):
        return ""


#: This process, as the runs it reports name it: id and start time, and this interpreter's own mark -- a restart
#: that re-executes in place (os.execv) keeps the id and the start time, not the runs.
OWNER = f"{_process_key(os.getpid())}:{os.urandom(4).hex()}"


def owner_alive(owner: str) -> bool:
    """Whether the process a run names as its owner still runs; a run without one (an older store) has none. A
    process this one may not inspect counts as alive (nothing is ended on a guess); the start time is compared with
    a second's slack (a clock step moves it a little)."""
    if owner == OWNER:
        return True
    pid, _, rest = (owner or "").partition(":")
    if not pid.isdigit() or int(pid) == os.getpid():
        return False   # this process, but an earlier interpreter in it
    try:
        started = float(rest.partition(":")[0])
    except ValueError:
        return False
    try:
        return abs(psutil.Process(int(pid)).create_time() - started) < 1.0
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError):
        return True


def needs_you(state: str, acked: Any) -> bool:
    """Whether a run waits for its person: a result to check, a question -- or a failure or handover not yet seen."""
    return state in ("review", "needs_input") or (state in ("failed", "handed_over") and not acked)


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class RunEventStore:
    """The runs and their events in one SQLite file; thread safe, safe between processes (sqlite_guard)."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, connection(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS agent_runs (run_id TEXT PRIMARY KEY, agent TEXT NOT NULL, "
                       "who TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', "
                       "task TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, started TEXT NOT NULL, updated TEXT NOT NULL, "
                       "ended TEXT, last_seq INTEGER NOT NULL DEFAULT 0, step_index INTEGER, step_total INTEGER, "
                       "step_label TEXT NOT NULL DEFAULT '', step_key TEXT NOT NULL DEFAULT '', step_labels TEXT NOT NULL DEFAULT '', "
                       "progress REAL, eta_s INTEGER, updated_mono REAL, acked INTEGER NOT NULL DEFAULT 0, "
                       "owner TEXT NOT NULL DEFAULT '')")
            columns = {row[1] for row in db.execute("PRAGMA table_info(agent_runs)").fetchall()}
            if "acked" not in columns:   # a store of stage 1
                db.execute("ALTER TABLE agent_runs ADD COLUMN acked INTEGER NOT NULL DEFAULT 0")
            if "owner" not in columns:   # a store of stage 3
                db.execute("ALTER TABLE agent_runs ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE TABLE IF NOT EXISTS agent_run_events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "event_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, seq INTEGER NOT NULL, who TEXT NOT NULL, "
                       "body TEXT NOT NULL, UNIQUE (run_id, seq))")
            db.execute("CREATE INDEX IF NOT EXISTS agent_run_events_who ON agent_run_events (who, cursor)")
        self.swept = self.sweep()

    # ── writing ──────────────────────────────────────────────────────────────

    def sweep(self) -> list[dict[str, Any]]:
        """End as ``failed`` every run still held (queued, working, asking) whose process is gone -- a restart, a
        crash: nothing will say anything for it again, and its person sees that it stopped. Answers the events."""
        with self._lock, connection(self.path) as db:
            held = db.execute(f"SELECT run_id, owner FROM agent_runs WHERE state IN ({', '.join('?' for _ in HELD)})",
                              tuple(HELD)).fetchall()
        ended = []
        for run_id, owner in held:
            if not owner_alive(owner):
                event = self.append(run_id, "run.failed",
                                    payload={"reason": "interrupted: the process that ran it has ended"})
                if event is not None:
                    ended.append(event)
        return ended

    def append(self, run_id: str, type_: str, *, agent: str = "", who: str = "", title: str = "", source: str = "",
               task: str = "", step: dict[str, Any] | None = None, progress: float | None = None,
               eta_s: int | None = None, touching: Iterable[str] = (), payload: dict[str, Any] | None = None,
               ) -> dict[str, Any] | None:
        """Write one event of ``run_id`` and the run's new state; answer the event (with its ``cursor``), or None when
        the run is already ended (nothing more is said for it) or unknown and the event cannot start it."""
        if type_ not in TYPES:
            raise ValueError(f"unknown run event type {type_!r}")
        stamp = now_iso()
        with self._lock, connection(self.path) as db:
            row = db.execute(f"SELECT {', '.join(RUN_FIELDS)} FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            run = dict(zip(RUN_FIELDS, row)) if row else None
            starts = type_ in ("run.queued", "run.started")
            if run is None:
                if not starts:
                    return None
                run = {"run_id": run_id, "agent": agent, "who": who, "title": _cut(title, 120), "source": source,
                       "task": task, "state": "queued", "started": stamp, "updated": stamp, "ended": None,
                       "last_seq": 0, "step_index": None, "step_total": None, "step_label": "", "step_key": "",
                       "step_labels": "", "progress": None, "eta_s": None, "acked": 0, "owner": OWNER}
            elif type_ == "run.acknowledged":
                # only an end is seen: a question is answered, a result checked (acknowledge: run.done)
                if not needs_you(run["state"], run["acked"]) or run["state"] in ("review", "needs_input"):
                    return None
                run["acked"] = 1
            elif run["state"] in FINAL:
                if not starts:
                    return None
                # the same run goes on (a person answered it): a new turn, the step count starts over
                run.update(ended=None, step_index=None, step_total=None, step_label="", step_key="", step_labels="",
                           progress=None, eta_s=None, acked=0)
            if starts:
                run["owner"] = OWNER   # the process that (re)starts a run holds it
            state = TYPES[type_] or run["state"]
            if step is not None:
                run.update(step_index=step.get("index"), step_total=step.get("total"), step_label=step.get("label", ""),
                           step_key=step.get("key", ""),
                           step_labels=json.dumps(step["labels"], ensure_ascii=False) if step.get("labels") else "")
            if type_ == "run.started":
                run.update(step_index=None, step_label="", step_key="", step_labels="")
            if progress is not None:
                run["progress"] = progress
            if eta_s is not None:
                run["eta_s"] = eta_s
            heartbeat = type_ == "run.heartbeat"
            run.update(state=state, updated=stamp, last_seq=int(run["last_seq"]) + (0 if heartbeat else 1))
            if state in FINAL:
                run["ended"] = stamp
            event = {
                "v": SCHEMA_VERSION, "event_id": _event_id(), "run_id": run_id, "seq": run["last_seq"], "ts": stamp,
                "type": type_, "agent": run["agent"], "source": run["source"], "task": run["task"],
                "subject": {"title": run["title"]}, "state": state, "needs_you": needs_you(state, run["acked"]),
                "step": (self._step(run) if state == "working" and run["step_label"] else None),
                "progress": run["progress"], "eta_s": run["eta_s"],
                "touching": [_cut(item, 200) for item in list(touching)[:MAX_TOUCHING]],
                "payload": payload,
            }
            if heartbeat:
                # alive, nothing new: the run's clock moves, no event is kept (one every few seconds per run)
                db.execute("UPDATE agent_runs SET updated = ?, updated_mono = ? WHERE run_id = ?",
                           (stamp, time.time(), run_id))
                return {**event, "cursor": None, "who": run["who"]}
            db.execute(f"INSERT OR REPLACE INTO agent_runs ({', '.join(RUN_FIELDS)}, updated_mono) VALUES "
                       f"({', '.join('?' for _ in RUN_FIELDS)}, ?)",
                       (*(run[key] for key in RUN_FIELDS), time.time()))
            cursor = db.execute("INSERT INTO agent_run_events (event_id, run_id, seq, who, body) VALUES (?, ?, ?, ?, ?)",
                                (event["event_id"], run_id, event["seq"], run["who"], json.dumps(event))).lastrowid
            if starts and run["last_seq"] == 1:
                self._prune(db)
        return {**event, "cursor": cursor, "who": run["who"]}

    @staticmethod
    def _step(run: dict[str, Any]) -> dict[str, Any]:
        step = {"index": run["step_index"], "total": run["step_total"], "key": run["step_key"],
                "label": run["step_label"]}
        if run["step_labels"]:
            step["labels"] = json.loads(run["step_labels"])
        return step

    @staticmethod
    def _prune(db: Any) -> None:
        old = "SELECT run_id FROM agent_runs ORDER BY started DESC LIMIT -1 OFFSET ?"
        db.execute(f"DELETE FROM agent_run_events WHERE run_id IN ({old})", (MAX_RUNS,))
        db.execute(f"DELETE FROM agent_runs WHERE run_id IN ({old})", (MAX_RUNS,))

    # ── reading ──────────────────────────────────────────────────────────────

    @staticmethod
    def _shown(row: Any, mono: float | None, now: float) -> dict[str, Any]:
        run = dict(zip(RUN_FIELDS, row))
        run["step_labels"] = json.loads(run["step_labels"]) if run["step_labels"] else {}
        run["silent"] = run["state"] in ACTIVE and mono is not None and now - mono > SILENT_S
        run["needs_you"] = needs_you(run["state"], run["acked"])
        del run["owner"]   # the server's own business: which process holds the run
        return run

    def acknowledge(self, run_id: str, who: str) -> dict[str, Any] | None:
        """A person has dealt with a run that needed them: a result checked ends the run as done, a failure or
        handover seen stops asking. None when the run does not wait for anyone, or waits for an answer (that comes
        through the question's own route)."""
        run = self.run(run_id)
        if run is None or not run["needs_you"]:
            return None
        if run["state"] == "review":
            return self.append(run_id, "run.done", payload={"checked_by": who})
        return self.append(run_id, "run.acknowledged", payload={"by": who})

    @staticmethod
    def _with_question(db: Any, run: dict[str, Any]) -> dict[str, Any]:
        """A run that waits for an answer, with the question it waits for (its last ``run.needs_input``)."""
        if run["state"] == "needs_input":
            row = db.execute("SELECT body FROM agent_run_events WHERE run_id = ? AND body LIKE '%\"run.needs_input\"%' "
                             "ORDER BY seq DESC LIMIT 1", (run["run_id"],)).fetchone()
            run["question"] = (json.loads(row[0]).get("payload") or None) if row else None
        return run

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, connection(self.path) as db:
            row = db.execute(f"SELECT {', '.join(RUN_FIELDS)}, updated_mono FROM agent_runs WHERE run_id = ?",
                             (run_id,)).fetchone()
            return self._with_question(db, self._shown(tuple(row)[:-1], row[-1], time.time())) if row else None

    def runs(self, who: str | None, limit: int = 20) -> list[dict[str, Any]]:
        """The runs of ``who`` (all with None) -- those not ended first, then the newest."""
        where, args = ("WHERE who = ?", (who,)) if who is not None else ("", ())
        with self._lock, connection(self.path) as db:
            rows = db.execute(f"SELECT {', '.join(RUN_FIELDS)}, updated_mono FROM agent_runs {where} "
                              "ORDER BY ended IS NOT NULL, started DESC LIMIT ?", (*args, limit)).fetchall()
            now = time.time()
            return [self._with_question(db, self._shown(tuple(row)[:-1], row[-1], now)) for row in rows]

    def events(self, run_id: str, after_seq: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        """The events of a run after ``after_seq``, in order."""
        with self._lock, connection(self.path) as db:
            rows = db.execute("SELECT body FROM agent_run_events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                              (run_id, after_seq, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def cursor_of(self, event_id: str) -> int | None:
        with self._lock, connection(self.path) as db:
            row = db.execute("SELECT cursor FROM agent_run_events WHERE event_id = ?", (event_id,)).fetchone()
        return int(row[0]) if row else None

    def last_cursor(self) -> int:
        with self._lock, connection(self.path) as db:
            row = db.execute("SELECT MAX(cursor) FROM agent_run_events").fetchone()
        return int(row[0] or 0)

    def since(self, cursor: int, who: str | None, limit: int = 1000) -> list[dict[str, Any]]:
        """The events of ``who`` (all with None) after ``cursor``, in the order they were written, each with its
        ``cursor`` -- what a stream that dropped missed."""
        where, args = ("AND who = ?", (who,)) if who is not None else ("", ())
        with self._lock, connection(self.path) as db:
            rows = db.execute(f"SELECT cursor, body, who FROM agent_run_events WHERE cursor > ? {where} "
                              "ORDER BY cursor LIMIT ?", (cursor, *args, limit)).fetchall()
        return [{**json.loads(row[1]), "cursor": int(row[0]), "who": row[2]} for row in rows]


class RunEventBus:
    """Hands each event to every open stream, each through its own queue on its own event loop. A stream too slow
    to keep up loses events; it reads them on from the store (gap in ``seq``)."""

    QUEUE_SIZE = 1000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        with self._lock:
            self._subscribers.append((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers = [(loop, q) for loop, q in self._subscribers if q is not queue]

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._put, queue, event)
            except RuntimeError:   # its loop is closed: the stream is gone
                self.unsubscribe(queue)

    @staticmethod
    def _put(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


_STORE: RunEventStore | None = None
_BUS = RunEventBus()
_STORE_LOCK = threading.Lock()


def run_event_store() -> RunEventStore:
    """The process's store, in the data directory (``run_events/run_events.db``), opened on first use."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = RunEventStore(data_path("run_events", "run_events.db"))
        return _STORE


def run_event_bus() -> RunEventBus:
    return _BUS


def use_run_events(store: RunEventStore | None, bus: RunEventBus | None = None) -> None:
    """Point the process at another store (and bus) -- for tests; None opens the default again on next use."""
    global _STORE, _BUS
    with _STORE_LOCK:
        _STORE = store
        if bus is not None:
            _BUS = bus


#: The reporters of the runs this process is running now, by run id (= session id).
_REPORTERS: dict[str, RunReporter] = {}


def reporter_for(session_id: str) -> RunReporter | None:
    """The reporter of the run working in ``session_id`` now, or None."""
    return _REPORTERS.get(session_id or "")


class RunReporter:
    """Writes the events of one run: ``started`` once, then ``step`` as it goes on, one end (``done``, ``failed``,
    ``cancelled``, ``handed_over``). Between them ``keep_alive`` sends a heartbeat whenever nothing else was said for
    ``HEARTBEAT_S``. A reporter never raises into the run it reports: a write that fails is logged and the run goes on.
    """

    def __init__(self, run_id: str, agent: str, *, who: str = "", title: str = "", source: str = "", task: str = "",
                 store: RunEventStore | None = None, bus: RunEventBus | None = None,
                 heartbeat_s: float = HEARTBEAT_S):
        self.run_id, self.agent, self.who, self.title, self.source, self.task = run_id, agent, who, title, source, task
        self._store, self._bus = store, bus
        self.heartbeat_s = heartbeat_s
        self.plan: list[str] = []
        self.index = 0
        self.label = ""
        self.ended = False
        self._last = 0.0
        self._alive: asyncio.Task[None] | None = None
        self._write = asyncio.Lock()

    @property
    def store(self) -> RunEventStore:
        return self._store or run_event_store()

    @property
    def bus(self) -> RunEventBus:
        return self._bus or run_event_bus()

    async def _emit(self, type_: str, **fields: Any) -> dict[str, Any] | None:
        async with self._write:
            try:
                event = await asyncio.to_thread(self.store.append, self.run_id, type_, agent=self.agent, who=self.who,
                                                title=self.title, source=self.source, task=self.task, **fields)
            except Exception:
                logger.exception("run %s: event %s not kept", self.run_id, type_)
                return None
            self._last = time.monotonic()
        if event is not None:
            self.bus.publish(event)
        return event

    # ── the life of a run ────────────────────────────────────────────────────

    async def started(self, plan: Iterable[str] = ()) -> dict[str, Any] | None:
        """The run begins (again, when a person answered it); ``plan`` names its steps where they are known."""
        self.plan = [_cut(label, MAX_LABEL) for label in plan]
        self.index, self.label, self.ended = 0, "", False
        _REPORTERS[self.run_id] = self
        return await self._emit("run.started", payload={"plan": self.plan} if self.plan else None)

    async def step(self, label: str, *, index: int | None = None, total: int | None = None, key: str = "",
                   labels: dict[str, str] | None = None, touching: Iterable[str] = (),
                   progress: float | None = None, eta_s: int | None = None) -> dict[str, Any] | None:
        """The run goes on to a step: ``label`` in plain words (``labels``: the same in other languages, by language
        code). Without ``index`` the step after the last one -- the same label again stays the same step (said again
        as a sign of life, with what it touches now)."""
        if self.ended:
            return None
        label = _cut(label, MAX_LABEL)
        labels = {str(code): _cut(text, MAX_LABEL) for code, text in (labels or {}).items() if text}
        if index is None:
            index = self.index if (label == self.label and self.index) else self.index + 1
        self.index, self.label = index, label
        if total is None and self.plan:
            total = len(self.plan)
        if total is not None and index > total:
            total = index
        if progress is None and total:
            progress = round(min(1.0, max(0.0, (index - 1) / total)), 3)
        return await self._emit("run.step", step={"index": index, "total": total, "key": key, "label": label,
                                                  "labels": labels},
                                touching=touching, progress=progress, eta_s=eta_s)

    async def heartbeat(self) -> dict[str, Any] | None:
        if self.ended:
            return None
        return await self._emit("run.heartbeat")

    async def needs_input(self, question: str, *, question_id: str, answer_url: str, options: Iterable[str] = (),
                          multi_select: bool = False, wait_s: float | None = None) -> dict[str, Any] | None:
        """The agent asks its person and waits: the question in its words, the options to pick from, and where the
        answer goes (``answer_url``, the asking plugin's route, with ``question_id``). ``wait_s``: how long it waits."""
        if self.ended:
            return None
        payload: dict[str, Any] = {"question": _cut(question, MAX_TEXT), "options": [_cut(o, 200) for o in options],
                                   "multi_select": bool(multi_select), "question_id": question_id,
                                   "answer_url": answer_url}
        if wait_s is not None:
            payload["wait_s"] = round(wait_s)
        return await self._emit("run.needs_input", payload=payload)

    async def answered(self, by: str = "", *, outcome: str = "answered") -> dict[str, Any] | None:
        """The question no longer waits -- ``outcome``: answered (by ``by``), timeout, cancelled -- the run goes on."""
        if self.ended:
            return None
        payload: dict[str, Any] = {"outcome": outcome}
        if by:
            payload["by"] = by
        return await self._emit("run.answered", payload=payload)

    async def _end(self, type_: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if self.ended:
            return None
        self.ended = True
        self.stop_keep_alive()
        if _REPORTERS.get(self.run_id) is self:
            del _REPORTERS[self.run_id]
        return await self._emit(type_, payload=payload)

    async def review_ready(self, summary: str, check_in: str = "") -> dict[str, Any] | None:
        """The agent's work is done and waits for its person to check it (``check_in``: where, in plain words);
        a person's ``acknowledge`` ends the run as done."""
        payload = {"summary": _cut(summary, MAX_TEXT)}
        if check_in:
            payload["check_in"] = _cut(check_in, 200)
        return await self._end("run.review_ready", payload)

    async def done(self, summary: str = "") -> dict[str, Any] | None:
        return await self._end("run.done", {"summary": _cut(summary, MAX_TEXT)} if summary else None)

    async def failed(self, reason: str) -> dict[str, Any] | None:
        """A technical failure; the panel shows it as a handover to a person (spec: never fail silently)."""
        return await self._end("run.failed", {"reason": _cut(reason, MAX_TEXT)})

    async def cancelled(self, by: str = "") -> dict[str, Any] | None:
        return await self._end("run.cancelled", {"by": by} if by else None)

    async def handed_over(self, reason: str, next_step: str = "") -> dict[str, Any] | None:
        payload = {"reason": _cut(reason, MAX_TEXT)}
        if next_step:
            payload["next_step"] = _cut(next_step, MAX_TEXT)
        return await self._end("run.handed_over", payload)

    # ── the sign of life ─────────────────────────────────────────────────────

    def keep_alive(self) -> None:
        """Send a heartbeat whenever the run said nothing for ``heartbeat_s``, until it ends."""
        if self._alive is None or self._alive.done():
            self._alive = asyncio.get_running_loop().create_task(self._beat(), name=f"run-heartbeat-{self.run_id}")

    def stop_keep_alive(self) -> None:
        if self._alive is not None and not self._alive.done() and self._alive is not asyncio.current_task():
            self._alive.cancel()
        self._alive = None

    async def _beat(self) -> None:
        while not self.ended:
            wait = self.heartbeat_s - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            await self.heartbeat()
