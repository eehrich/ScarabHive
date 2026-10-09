"""SessionPresence: holding a session, letting it go, and when it is woken.

The lock files under one sessions directory as this process sees them: its own
holds, counted and nested; what a lock file says about a session -- running,
being woken, or nobody's -- and taking one; the markers beside it
(<session>.pending, .woken, .stopped); and notify(), which decides whether input
that waits wakes the session. presence_for() hands out the one store per
directory. The stop marks are here too: the process that holds a session
answers from memory (``_stops``), its last hold leaves <session>.stopped, and
the two halves of that rule are read side by side.

Built on the lock primitives (lockfile.py) and the process helpers
(process.py); wake.py builds on this. A test that replaces one of the names
looked up here -- spawn_wake, sessions_dir, _open_locked, _probe,
_stored_session, _stops -- replaces it in this module: a name the package
re-exports is a second reference to the same object, and replacing that one
changes nothing here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ...config.models import SessionPresenceConfig
from ...paths import PROJECT_ROOT, data_path
from .lockfile import _BINARY, _drop, _lock, _open_locked, _read, _unlink, _unlock_and_close, _write
from .process import _own_start, alive, spawn_wake, wake_depth

logger = logging.getLogger(__name__)

# SessionManager's rule for session ids; anything else names no session file.
_SESSION_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
# A wake holds the lock file for the length of a process start; whoever wants
# the session meanwhile waits that out rather than being turned away. The
# patience is only ever spent while notify() has the file -- a session nobody
# holds is taken right away.
_WAKE_PATIENCE = 0.5
_RETRY_WAIT = 0.02  # waiting costs a thread here, not a core


class SessionBusy(RuntimeError):
    """Another process runs the session, so this one must not write it too."""

    def __init__(self, session_id: str, agent: str = ""):
        self.session_id = session_id
        self.agent = agent
        super().__init__(f"Session {session_id} is running in another process"
                         + (f" (agent {agent})" if agent else ""))


def sessions_dir() -> Path:
    """Where the session files are -- the one rule every process uses.

    AGENT_SESSION_STORAGE_PATH if set (a woken run inherits it from the
    process that woke it), else ``sessions`` in the data directory.
    """
    return Path(os.getenv("AGENT_SESSION_STORAGE_PATH") or PROJECT_ROOT / data_path("sessions"))


def _stored_session(path: Path) -> Optional[dict[str, Any]]:
    """What the session file says about itself: the agent it was saved on, and
    whether it belongs to a sub-agent -- those are run by the orchestrator that
    spawned them, so nobody else may start them. None where no session is."""
    try:
        with open(path, "rb") as handle:
            content = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(content, dict):
        return None
    return {"agent": content.get("agent_name") or "",
            "sub_agent": bool(content.get("parent_session"))}


def _probe(path: Path) -> Optional[dict[str, Any]]:
    """What a lock file says -- running or waking -- or None: no file, or one
    nobody holds any more, which goes."""
    try:
        fd = os.open(path, os.O_RDWR | _BINARY)
    except OSError:
        return None
    locked = _lock(fd)
    try:
        content = _read(fd)
    except OSError:
        content = {}
    if alive(content.get("wake_pid"), content.get("wake_started")):
        # Before the lock on purpose: notify() keeps the file locked while it
        # starts the run, and a session being woken is not one that runs.
        _unlock_and_close(fd) if locked else os.close(fd)
        return {"status": "waking", "agent": ""}
    if not locked:
        os.close(fd)
        return {"status": "running", "agent": content.get("agent", "")}
    _drop(fd, path)
    return None


def _take(path: Path, session_id: str) -> int:
    """The locked handle for a session no other process holds. Who holds it is
    read off the file itself, and one let go in between is taken after all.

    A wake being started holds the file for the length of a process start; that
    is waited out, not reported as a session that runs.
    """
    deadline = time.monotonic() + _WAKE_PATIENCE
    while True:
        fd = _open_locked(path)
        if fd is not None:
            return fd
        state = _probe(path)
        if state and state["status"] == "running":
            # Asked once more before it is reported: the holder may have let go
            # between the attempt above and this answer, and then the session is
            # free -- the caller would be told it is worked on elsewhere and a
            # run would stay out of a session nobody has. Measured on one session
            # with six holders and two askers: 415 of some 3000 answers of
            # "running" were this stale. The lock attempt is the only thing that
            # can tell; asking cannot close the window, only narrow it.
            try:
                fd = _open_locked(path)
            except OSError:
                fd = None   # the file will not open: what is known is that somebody holds it
            if fd is not None:
                return fd
            raise SessionBusy(session_id, state["agent"])
        if time.monotonic() >= deadline:
            raise SessionBusy(session_id)
        time.sleep(_RETRY_WAIT)


#: Request ids of the runs their user stopped, noted where the stop is made.
#: ponytail: one id per user stop for the life of the process.
_stops: set[str] = set()
_stops_lock = threading.Lock()


def _belongs(request_id: str, run: str) -> bool:
    """A run's tool calls, sub-agents and agent-as-tool runs carry its id as a prefix."""
    return bool(run) and (request_id == run or request_id.startswith(run + "_"))


def _note(request_id: str) -> None:
    if request_id:
        with _stops_lock:
            _stops.add(request_id)


def note_stop(request_id: str) -> None:
    """Its user stopped this run -- the web chat's Stop, Ctrl-C in a CLI. Not for
    a run that ends by its own failure, and not for a caller ending its sub-agent:
    neither is its user stopping anything.

    A session this process holds and whose last run it was is marked on disk at
    once, not only when the process lets go: a process that ends before that
    (a chat left open for hours, then closed) must not take the stop with it."""
    if not request_id:
        return
    _note(request_id)
    with _stores_lock:
        stores = list(_stores.values())
    for store in stores:
        store._mark_stopped(request_id)


def forget_stop(request_id: str) -> None:
    """A new run is adopted under an id a caller chose (app.py): whatever an
    earlier run of the same id was told is over (writer_jobs sends a run again
    under its id)."""
    with _stops_lock:
        _stops.discard(request_id)


def stopped_by_user(request_id: str) -> bool:
    """Whether this run, or the run it belongs to, was stopped by its user."""
    if not request_id:
        return False
    with _stops_lock:
        return any(_belongs(request_id, run) for run in _stops)


@dataclass
class _Hold:
    """This process's hold on a session."""
    fd: int
    holds: int = 1
    last_run: str = ""  # the run that took it last: how that one ended is the session's
    runs: set[str] = field(default_factory=set)  # the runs that took it while held, not nested ones


class SessionPresence:
    """The lock files under one sessions directory, and the wake rules over them."""

    def __init__(self, root: str | Path, max_wake_depth: int = 3):
        self.root = Path(root)
        self.max_wake_depth = max_wake_depth
        #: The process's config (presence_for sets it): what a wake is judged by
        #: before it is started (_wake_refusal). None: nothing to judge by.
        self.system_config: Any = None
        self._held: dict[Path, _Hold] = {}
        self._guard = threading.Lock()

    def _user_dir(self, user_id: str) -> Path:
        # SessionManager._sanitize_user_id
        return self.root / user_id.replace("..", "_").replace("/", "_").replace("\\", "_")

    def _lock_path(self, session_id: str, user_id: str) -> Optional[Path]:
        if not session_id or not user_id or not _SESSION_ID.match(session_id):
            return None
        return self._user_dir(user_id) / f"{session_id}.lock"

    def hold(self, session_id: str, user_id: str, agent_name: str, run: str = "") -> bool:
        """This process has the session in hand. Holds nest. ``run`` names a
        run (the agent loop) by its request id: its hold lifts a stop mark, no
        other hold does, and it is the session's last run unless it runs inside
        a run that took the session in this hold (an agent-as-tool on its
        caller's session).

        Raises SessionBusy while another process runs the session: two runs
        would both write the conversation and the last save would win. False
        means the disk refused -- the run goes on unheld rather than not at all.
        """
        path = self._lock_path(session_id, user_id)
        if path is None:
            return False
        with self._guard:
            entry = self._held.get(path)
            if entry is not None:
                entry.holds += 1
                if run and not any(_belongs(run, taken) for taken in entry.runs):
                    entry.last_run = run
                    entry.runs.add(run)
                    _unlink(path.with_suffix(".stopped"))   # a run takes it: whatever stopped is over
                return True
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = _take(path, session_id)
            except OSError as exc:
                logger.warning("Session presence: could not hold %s: %s", session_id, exc)
                return False
            if run:
                _unlink(path.with_suffix(".stopped"))   # a run takes it: whatever stopped is over
            try:
                _write(fd, {"agent": agent_name, "since": time.time()})
            except OSError:
                pass  # the lock is what counts; the content only names the agent
            self._held[path] = _Hold(fd, last_run=run, runs={run} if run else set())
            return True

    def held_here(self, session_id: str, user_id: str) -> bool:
        """Whether THIS process holds the session.

        The lock file cannot say. Probing it opens a fresh handle, and the OS
        lock conflicts across two handles of one process exactly as it does
        across processes -- so a session this process holds reads as "running"
        to its own probe, indistinguishable from one another process runs.
        """
        path = self._lock_path(session_id, user_id)
        with self._guard:
            return path in self._held

    def release(self, session_id: str, user_id: str, stopped: bool = False) -> None:
        """Undo one hold. The last one lets the session go, and input waiting
        for it wakes it -- unless its last run was stopped by its user.

        ``stopped`` is for a holder around a run that saw its user stop it where
        the run cannot hear of it (agent-run: Ctrl-C cancels the task, not the
        run): it notes the stop for the session's last run -- and marks the
        session when no run took it yet (a Ctrl-C while a CLI loads it)."""
        path = self._lock_path(session_id, user_id)
        with self._guard:
            entry = self._held.get(path)
            if entry is None:
                return
            if stopped:
                _note(entry.last_run)
                self._touch_stopped(path, session_id)
            entry.holds -= 1
            if entry.holds:
                return
            del self._held[path]
            if stopped_by_user(entry.last_run):
                # Before the lock goes: a ring in between would find it idle and
                # wake it.
                self._touch_stopped(path, session_id)
            if path.with_suffix(".stopped").exists():
                _unlink(path.with_suffix(".pending"))   # the input waits in its store; its doorbell goes
            try:
                _drop(entry.fd, path)
            except OSError as exc:
                logger.warning("Session presence: releasing %s: %s", session_id, exc)
        if self.pending(session_id, user_id):
            logger.info("Session %s let go with input waiting; waking it", session_id)
            try:
                self.notify(session_id, user_id)
            except OSError as exc:
                # Callers let go in a finally: a disk that refuses here would
                # replace the answer they are on their way out with, or mask
                # the exception already in flight. hold() guards the same way.
                logger.warning("Session presence: waking %s: %s", session_id, exc)

    def _wake_refusal(self, user_id: str, stored: Optional[dict[str, Any]]) -> str:
        """Why the woken run would be refused by its agent's role gate, or "".

        Judged as the woken process will judge it (Agent._run_denial there):
        agent-cli runs the agent it picks for the session -- the stored one,
        or the config's default where the config no longer defines that one
        (cli_utils.session_defaults, the same two calls) -- as the session's
        user, and it trusts the local operator. The agent's gate is read from
        this process's config; one changed on disk since is the woken process's
        to apply, and its refusal then stops the chain there. A config that
        does not resolve leaves the decision to that process as well.
        """
        config = self.system_config
        if config is None:
            return ""
        from ...auth.agent_access import agent_run_denial
        from ...cli_utils.session_defaults import choose_agent_name, usable_session_defaults
        from ...config.settings import get_tool_server_config
        # A session file that is missing or unreadable names no agent: agent-cli
        # then runs the config's default one, and that one is judged.
        usable, _ = usable_session_defaults((stored or {}).get("agent") or None, None, config)
        agent_name = choose_agent_name(None, usable, getattr(config, "default_agent", None))
        if not agent_name:
            return ""
        try:
            merged = get_tool_server_config(agent_name, config)
        except Exception as exc:  # noqa: BLE001 - the woken run decides, as before
            logger.debug("Session presence: no gate for %s (%s)", agent_name, exc)
            return ""
        min_role = merged.metadata.min_role if merged is not None and merged.metadata is not None else None
        reason = agent_run_denial(min_role, user_id, getattr(config, "auth", None), local_operator=True)
        return f"its agent '{agent_name}' may not be run by '{user_id}': {reason}" if reason else ""

    @staticmethod
    def _touch_stopped(path: Path, session_id: str) -> None:
        try:
            path.with_suffix(".stopped").touch()
        except OSError as exc:
            logger.warning("Session presence: marking %s stopped: %s", session_id, exc)

    def _mark_stopped(self, request_id: str) -> None:
        """Mark the sessions held here whose last run a stop just covered."""
        with self._guard:
            for path, entry in self._held.items():
                if _belongs(entry.last_run, request_id):
                    self._touch_stopped(path, path.stem)

    def take_pending(self, session_id: str, user_id: str) -> None:
        """A step of the session is about to hand the waiting input over."""
        path = self._lock_path(session_id, user_id)
        if path is not None:
            _unlink(path.with_suffix(".pending"))

    def take_for_wake(self, session_id: str, user_id: str) -> None:
        """A turn starts now BECAUSE input is waiting: a woken run (agent_cli), a chat's woken prompt.

        The chat's is cli_utils/chat/context.py (_take_wake_mark). Takes the mark and changes <session>.woken. Such a turn is told that input waits, as a woken
        process is, so whatever waited when it began counts as delivered: wake_session stops ringing
        when it sees the stamp change. A held chat turns every ring into a turn of its own -- without
        the stamp one finished command cost a turn per ring, up to WAKE_RETRIES of them.
        """
        path = self._lock_path(session_id, user_id)
        if path is None:
            return
        try:
            # A value of its own each time: a clock can repeat itself within its tick.
            path.with_suffix(".woken").write_text(os.urandom(8).hex(), encoding="ascii")
        except OSError as exc:
            logger.warning("Session presence: stamping the wake of %s: %s -- its ringers ring on", session_id, exc)
        _unlink(path.with_suffix(".pending"))

    def wake_stamp(self, session_id: str, user_id: str) -> str:
        """<session>.woken as it stands, "" without one: a change means a turn started for the waiting input."""
        path = self._lock_path(session_id, user_id)
        if path is None:
            return ""
        try:
            return path.with_suffix(".woken").read_text(encoding="ascii")
        except (OSError, ValueError):
            return ""

    def pending(self, session_id: str, user_id: str) -> bool:
        """Whether input is waiting for the session. A woken run asks before it
        starts: whoever held the session meanwhile may have taken it over. Not
        for a session whose last run its user stopped: its input waits for a
        run somebody starts."""
        path = self._lock_path(session_id, user_id)
        return (path is not None and path.with_suffix(".pending").exists()
                and not self._stopped(path))

    def _stopped(self, path: Path) -> bool:
        """Whether the session's last run was stopped by its user: said by this
        process while a run of it held the session, by <session>.stopped
        otherwise."""
        with self._guard:
            entry = self._held.get(path)
            last_run = entry.last_run if entry is not None else ""
        if last_run:
            return stopped_by_user(last_run)
        return path.with_suffix(".stopped").exists()

    def get(self, session_id: str, user_id: str) -> Optional[dict[str, Any]]:
        """{"status": running | waking | idle, "agent": ..., "sub_agent": ...}
        for a session of the user; idle is a session file nobody holds. None
        for no session."""
        path = self._lock_path(session_id, user_id)
        if path is None:
            return None
        state = _probe(path)
        stored = _stored_session(path.with_suffix(".json"))
        if state is None and stored is None:
            return None
        state = state or {"status": "idle", "agent": ""}
        return {"status": state["status"],
                "agent": state["agent"] or (stored or {}).get("agent", ""),
                "sub_agent": bool(stored and stored["sub_agent"])}

    def status(self, session_id: str, user_id: str) -> Optional[str]:
        """running or waking while some process has the session in hand or is
        waking it, else None -- what get() says about the lock, without reading
        the session file beside it. For a caller that asks often: the sub-agent
        list asks per sub-agent before every LLM call, and a sub-agent's
        session file is its whole transcript."""
        path = self._lock_path(session_id, user_id)
        state = _probe(path) if path is not None else None
        return state["status"] if state else None

    def list_for_user(self, user_id: str, exclude: str = "") -> list[dict[str, Any]]:
        """The user's sessions that run or are being woken. Sub-agents' sessions
        stay out: they belong to the run that spawned them."""
        user_dir = self._user_dir(user_id)
        try:
            names = [entry.name for entry in os.scandir(user_dir) if entry.name.endswith(".lock")]
        except OSError:
            return []
        sessions = []
        for name in names:
            session_id = name[:-len(".lock")]
            state = None if session_id == exclude else _probe(user_dir / name)
            if not state:
                continue
            stored = _stored_session(user_dir / f"{session_id}.json")
            if stored and stored["sub_agent"]:
                continue
            sessions.append({"session_id": session_id, "status": state["status"],
                             "agent": state["agent"] or (stored or {}).get("agent", "")})
        return sessions

    def notify(self, session_id: str, user_id: str) -> tuple[str, str]:
        """Input is waiting for the session. Returns (status, note), status one
        of delivered_next_step, being_woken, woke_session, queued (the note says
        why), unknown.

        delivered_next_step and being_woken both mean "a run will read this", and
        they are NOT the same answer: the first is a session somebody holds, which
        lets go at some point and can be rung again; the second is a run already on
        its way, and ringing it again would start a second run for news that is
        being read. Whoever repeats a ring has to stop on the second."""
        path = self._lock_path(session_id, user_id)
        stored_path = path.with_suffix(".json") if path is not None else None
        if path is None or not (path.exists() or stored_path.exists()):
            if path is not None:
                # A session that ran but never reached disk left its marker here.
                _unlink(path.with_suffix(".pending"))
            return "unknown", "no such session"
        pending = path.with_suffix(".pending")
        if self._stopped(path):
            # Its user stopped its last run: nothing starts it again by itself. The
            # input waits in its store for the next run.
            _unlink(pending)
            return "queued", STOPPED
        pending.touch()  # before the lock: a holder letting go right now still finds it
        keep = False     # the lock file stays: a run of this session is on its way
        waiting = True   # the marker stays: that run has not taken the input yet
        try:
            fd = _open_locked(path)
            if fd is None:
                return "delivered_next_step", ""
            try:
                if path.with_suffix(".stopped").exists():
                    # Again under the lock: a stopped run that let go after the
                    # check above marked the session before its lock went.
                    waiting = False
                    return "queued", STOPPED
                content = _read(fd)
                if alive(content.get("wake_pid"), content.get("wake_started")):
                    keep = True
                    # Woken a moment ago; that run reads it. Its own answer, not
                    # the held one: a caller that rings a held session again must
                    # not ring this one, and only the answer can tell it apart.
                    return "being_woken", "a wake run is already on its way"
                stored = _stored_session(stored_path)
                if stored and stored["sub_agent"]:
                    waiting = False
                    return "queued", "a sub-agent's session; the run that spawned it hands the input over"
                depth = wake_depth()
                if depth >= self.max_wake_depth:
                    waiting = False
                    return "queued", "wake chain limit reached; it reads the input on its next run"
                refusal = self._wake_refusal(user_id, stored)
                if refusal:
                    # Not started: the run would be refused, and its letting go
                    # would ring again -- one refused agent-cli per ring, up to
                    # max_wake_depth, for input that never gets read. The marker
                    # stays (waiting): once the user may run the agent, the next
                    # ring wakes it.
                    logger.warning("Session presence: not waking %s: %s", session_id, refusal)
                    return "queued", refusal
                # This process is the one holding the file now, and starting
                # a run takes milliseconds: without the marker a run beginning
                # in that window reads the locked file as a session that runs
                # and is turned away over a message.
                _write(fd, {"wake_pid": os.getpid(), "wake_started": _own_start()})
                try:
                    pid, started = spawn_wake(session_id, user_id, depth + 1)
                except Exception as exc:
                    # Whatever starting a process runs into, it belongs to the
                    # input, not to the run that is letting the session go: it
                    # notifies from its own finally, and an exception here would
                    # take that run down over a message.
                    waiting = False
                    return "queued", f"could not wake it ({exc}); it reads the input on its next run"
                _write(fd, {"wake_pid": pid, "wake_started": started})
                keep = True
                return "woke_session", ""
            finally:
                if keep:
                    _unlock_and_close(fd)
                else:
                    _drop(fd, path)
        finally:
            if not waiting:
                # Nobody is on the way for it, so the marker would outlive its
                # reason; the input itself waits in the store that holds it.
                _unlink(pending)


_stores: dict[Path, SessionPresence] = {}
_stores_lock = threading.Lock()


def presence_for(system_config: Any) -> Optional[SessionPresence]:
    """This process's presence over the session files, or None while
    session_presence is off. One store per directory: the holds of a process
    are counted in one place."""
    config = getattr(system_config, "session_presence", None)
    if not isinstance(config, SessionPresenceConfig) or not config.enabled:
        return None
    root = sessions_dir()
    with _stores_lock:
        if root not in _stores:
            _stores[root] = SessionPresence(root)
        store = _stores[root]
    store.max_wake_depth = config.max_wake_depth
    store.system_config = system_config
    return store


#: notify()'s note for a session whose user stopped its last run, and
#: wake_session's for work such a run started. The user's choice, not a fault:
#: neither is reported as a warning.
STOPPED = "its user stopped its last run; nothing starts it again by itself"
