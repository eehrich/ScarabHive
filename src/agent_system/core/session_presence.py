"""Session presence: which sessions run right now, and waking an idle one.

A process holds a session while the conversation is in its hands: the agent
loop for each request (servers/agent/server.py), agent-cli run and agent-run
through their save after the run, agent-cli chat while the session is open.
Holding is an OS lock on <sessions>/<user>/<session>.lock, next to the
session file. The OS lets go of it however the process ends, so a crashed run
never looks running, and whoever meets a lock file nobody holds deletes it:
the directory keeps only the sessions that run.

Whoever wants a session another process holds is told so (SessionBusy) rather
than running it too: both would write the conversation, and the last save
would win.

notify() is for input that waits for a session -- a direct message, say. A
held session reads it on its next step; input that arrives after its last
LLM call leaves <session>.pending, and letting go of the session wakes it. A
session nobody holds is woken right away: agent-cli continues it from its
file, on its stored agent and profile. The marker stays until that run takes
it, so a run that finds none knows somebody else got there first.

A woken run carries its depth in HIVE_WAKE_DEPTH; at max_wake_depth nobody is
woken, so sessions that answer each other cannot start each other forever.
Sub-agents' sessions are never woken and never listed: they belong to the run
that spawned them.

A session whose last run its user stopped starts again only when somebody
starts it: not for input left waiting, not for work of the stopped run that
ends later and rings for it (wake_session knows that work by the run it came
from). The input waits in its store for the next run. A stop is noted where it
is made (note_stop: the web chat's Stop, Ctrl-C in a CLI), not asked of the
run, which may let go before it hears of it or after it can be told. The run
that took the session LAST decides: the holding process answers from memory,
and its last hold leaves <session>.stopped. The next run lifts the mark as it
takes the session; a hold that starts no run (/undo, an append, a woken run
that steps aside) leaves it.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

import psutil

from ..config.models import SessionPresenceConfig
from ..paths import data_path

if os.name == "nt":
    import ctypes
    import msvcrt

    # A handle is a pointer: read back as the default c_int it would be cut in
    # half on 64-bit, and the file the cut value names is not the one opened.
    _CreateFileW = ctypes.windll.kernel32.CreateFileW
    _CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                             ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    _CreateFileW.restype = ctypes.c_void_p
else:
    import fcntl

logger = logging.getLogger(__name__)

WAKE_DEPTH_ENV = "HIVE_WAKE_DEPTH"
WAKE_TASK = "You were woken because input is waiting for this session. Read it and act on it."
REPO_ROOT = Path(__file__).resolve().parents[3]
# SessionManager's rule for session ids; anything else names no session file.
_SESSION_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
# Windows locks are mandatory: the lock sits on a byte far past the content,
# so other processes can still read what the file says.
_LOCK_BYTE = 1 << 30
# A wake holds the lock file for the length of a process start; whoever wants
# the session meanwhile waits that out rather than being turned away. The
# patience is only ever spent while notify() has the file -- a session nobody
# holds is taken right away.
_WAKE_PATIENCE = 0.5
_RETRY_WAIT = 0.02  # waiting costs a thread here, not a core
# A delete is through in microseconds, and this wait is spent under the store's
# own guard: short, and often enough that twenty of them are still a fifth of a
# retry.
_DELETE_WAIT = 0.002
_BINARY = getattr(os, "O_BINARY", 0)


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
    return Path(os.getenv("AGENT_SESSION_STORAGE_PATH") or REPO_ROOT / data_path("sessions"))


def alive(pid: Optional[int], started: Optional[float] = None) -> bool:
    """Whether the process runs; with ``started``, whether it is still the same one."""
    if not pid:
        return False
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return False
        return started is None or abs(process.create_time() - started) < 1.0
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _own_start() -> float:
    """This process's create_time, so ``alive`` recognises its own marker."""
    try:
        return psutil.Process().create_time()
    except psutil.Error:
        return 0.0


def wake_depth() -> int:
    try:
        return int(os.environ.get(WAKE_DEPTH_ENV) or 0)
    except ValueError:
        return 0


def wake_command(session_id: str, user_id: str) -> list[str]:
    """agent-cli continuing the session from its file, on its stored agent and
    profile. --woken is what lets it step aside quietly when the session turns
    out to be taken: nobody typed this command, so nobody is waiting for it."""
    return [sys.executable, "-m", "agent_system.agent_cli", "--raw", "run", WAKE_TASK,
            "--session", session_id, "--session-user", user_id, "--woken"]


def _wake_log() -> Any:
    """Where a woken run's stderr goes, or DEVNULL when that cannot be opened.

    It used to go to DEVNULL together with stdout, and that is how a failed
    wake became unfalsifiable: the process starts, ``notify`` answers
    ``woke_session``, and whatever the run says on its way down -- a traceback
    at import, a tool server that will not build, "5 consecutive empty
    responses" -- is written to a handle that discards it. Somebody looking for
    a wake that did not work had nowhere to look.

    Appended to, never truncated: two wakes can overlap. stdout stays
    discarded -- that is the run's ANSWER, it belongs in the session file, and
    a ``--raw`` run writes a great deal of it.
    """
    try:
        path = REPO_ROOT / "logs" / "agent-wake.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return _append_handle(path)
    except OSError as exc:
        # A wake nobody can log is still a wake worth starting.
        logger.warning("Session presence: no wake log (%s); stderr is discarded", exc)
        return subprocess.DEVNULL


#: CreateFileW, for a handle that appends (winnt.h, fileapi.h).
_FILE_APPEND_DATA = 0x0004
_FILE_SHARE_READ_WRITE = 0x0003
_OPEN_ALWAYS = 4


def _append_handle(path: Path) -> Any:
    """A handle whose every write lands at the end, whatever other handles do.

    Two overlapping wakes each open the log and hand their handle to a child.
    On Windows ``open(path, "ab")`` asks for the general write right and keeps
    its own offset, so the second wake writes over the first -- measured, the
    first wake kept 0 of its 200 lines, and the log exists for exactly the wake
    that went wrong. FILE_APPEND_DATA is the right that appends in the file
    system instead, which is what the inherited handle carries into the child;
    what the C runtime does to make "ab" look like appending stays behind in
    this process. On POSIX O_APPEND is that guarantee already.
    """
    if os.name != "nt":
        return open(path, "ab")
    handle = _CreateFileW(str(path), _FILE_APPEND_DATA, _FILE_SHARE_READ_WRITE,
                          None, _OPEN_ALWAYS, 0, None)
    # INVALID_HANDLE_VALUE is -1, which comes back as the unsigned pointer it is.
    if not handle or handle == (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1:
        raise ctypes.WinError()
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_APPEND), "wb")


def spawn_wake(session_id: str, user_id: str, depth: int) -> tuple[int, float]:
    """Start the woken run on its own, out of sight; returns its (pid, create_time).

    CREATE_NO_WINDOW, not DETACHED_PROCESS: a detached process has no console,
    and Windows gives every console program it starts a window of its own --
    a woken run would open one per stdio tool server, in the user's face.

    Out of sight is not the same as unobservable: its stderr goes to a file
    (see :func:`_wake_log`), because a wake that fails silently cannot be told
    from one that never happened.
    """
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
              if os.name == "nt" else {"start_new_session": True})
    errors = _wake_log()
    try:
        process = subprocess.Popen(
            wake_command(session_id, user_id), cwd=REPO_ROOT,
            env={**os.environ, WAKE_DEPTH_ENV: str(depth)},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
            **detach)
    finally:
        # The child has its own handle by now; this one would otherwise be held
        # for the life of the API process, one per wake.
        if errors is not subprocess.DEVNULL:
            errors.close()
    try:
        return process.pid, psutil.Process(process.pid).create_time()
    except psutil.Error:  # gone already
        return process.pid, 0.0


def _lock(fd: int) -> bool:
    """Take the lock without waiting; False while another handle has it."""
    try:
        if os.name == "nt":
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_and_close(fd: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _is_file_at(fd: int, path: Path) -> bool:
    """Whether the path still names the file this handle has open."""
    try:
        return os.stat(path).st_ino == os.fstat(fd).st_ino
    except OSError:
        return False


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # gone already, or open in another process (Windows)
        pass


def _open_locked(path: Path, attempts: int = 20) -> Optional[int]:
    """The lock file, created if missing, opened and locked by a new handle;
    None while another handle holds it.

    Bounded on purpose: every attempt means the file was deleted under this
    one -- between the open and the lock, or while it was being opened -- and a
    filesystem that keeps answering that way would spin here forever instead of
    letting the run report it.
    """
    refused = None
    for _ in range(attempts):
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | _BINARY)
        except PermissionError as exc:
            # Windows, while the file is on its way out: a probe found it held
            # by nobody and dropped it (_drop), and until that delete is through
            # the name can be opened by no one. Measured on one session with six
            # holders and two askers: 52 holds in six seconds were refused this
            # way -- and a refused hold does not stop the run, it lets it write
            # the session with nothing to keep a second run out.
            #
            # Windows only: POSIX deletes a name at once, so a refusal there is
            # the directory's, and waiting would only put it off.
            if os.name != "nt":
                raise
            refused = exc
            time.sleep(_DELETE_WAIT)
            continue
        refused = None
        if not _lock(fd):
            os.close(fd)
            return None
        if _is_file_at(fd, path):
            return fd
        # Deleted between the open and the lock (only POSIX deletes an open
        # file): that file no longer belongs to the session.
        _unlock_and_close(fd)
    if refused is not None:
        raise refused   # not a delete in progress after all: the file cannot be opened at all
    raise OSError(f"{path} is replaced faster than it can be locked")


def _drop(fd: int, path: Path) -> None:
    """Delete a lock file this handle holds, and let go of it."""
    ours = _is_file_at(fd, path)
    if ours and os.name != "nt":
        _unlink(path)  # still locked: a handle opened meanwhile sees the file is gone
    _unlock_and_close(fd)
    if ours and os.name == "nt":
        _unlink(path)  # refused while another process has it open -- then it is theirs


def _read(fd: int) -> dict[str, Any]:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        content = json.loads(os.read(fd, 4096) or b"{}")
    except ValueError:  # being written this very moment
        return {}
    return content if isinstance(content, dict) else {}


def _write(fd: int, content: dict[str, Any]) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, json.dumps(content).encode())


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
    return store


#: The one reason that is a setting rather than a fault, so it is the one
#: reason a blocked wake is not worth a warning. Named, because the log level
#: is decided by the REASON -- a reason nobody has thought of yet should be
#: visible, not quietly demoted.
PRESENCE_OFF = "session presence is off (config: session_presence.enabled)"

#: notify()'s note for a session whose user stopped its last run, and
#: wake_session's for work such a run started. The user's choice, not a fault:
#: neither is reported as a warning.
STOPPED = "its user stopped its last run; nothing starts it again by itself"


def wake_blocked(system_config: Any, session_id: str, user_id: str) -> str:
    """Why waking this session is ruled out already, or "" when it is not.

    Note the direction: here "" is the GOOD answer, while ``wake_session``
    returns "" when nothing happened.

    Asked BEFORE long work starts, so whoever wants to be woken can be told to
    poll instead. The wording reaches the model that asked, so it says what to
    change rather than only what failed.

    "" is not a promise. It means everything that CAN be checked up front came
    out fine. Two of ``notify``'s exits cannot be:

    * Whether the process holding the work is still there when the work ends.
      A one-shot ``agent-cli run`` ends its turn and takes its background work
      with it, and nothing in this tree marks which kind of process this is.
    * Whether this is a sub-agent's session, which is never woken -- the run
      that spawned it hands its result over. Reading that means parsing the
      whole session file (``_stored_session``), which is the conversation, on
      the caller's event loop for every armed wake. Deliberately not done
      here; ``wake_session`` reports it as a wake that did NOT happen.

    So a caller told the wake is armed must still treat being woken as the
    good case and polling as the fallback.
    """
    presence = presence_for(system_config)
    if presence is None:
        return PRESENCE_OFF
    if not session_id or not user_id:
        # Both are injected per tool call (servers/agent/components/
        # tool_execution.py). Missing means there is no session behind this.
        return "this call belongs to no session, so there is nobody to wake"
    # Free to ask -- an env var and an int -- and it covers the setting that
    # turns waking off entirely (max_wake_depth: 0, "0 = never wake"), which
    # would otherwise answer "armed" to every caller and wake none of them.
    depth = wake_depth()
    if depth >= presence.max_wake_depth:
        if presence.max_wake_depth == 0:
            return "waking is switched off (config: session_presence.max_wake_depth is 0)"
        return (f"this run was itself woken, {depth} deep, and the wake chain stops at "
                f"max_wake_depth={presence.max_wake_depth}")
    return ""


#: A session that is HELD when the work ends gets rung again, because the ring
#: it got is thrown away (see the loop below). Five minutes of ringing at ten
#: seconds covers an ordinary turn; a longer one ends with the marker in place,
#: and release() wakes it on that.
WAKE_RETRY_SECONDS = 10.0
WAKE_RETRIES = 30

#: The answers from notify() that mean "the session is busy, ring again". Every
#: other answer ends the ringing, which is deliberate: a session somebody HOLDS
#: lets go at some point, and one a wake run is ALREADY on its way to is being
#: read right now -- ringing on through the second starts a second wake run for
#: news somebody is reading. notify() gave both the same answer until 20.09.2026;
#: "being_woken" ends the ringing here by not being in this set. A session its
#: user stopped is rung on as well (the loop below): the next run the user starts
#: lifts its mark, and work of a run nobody stopped is still news then.
RING_AGAIN = frozenset({"delivered_next_step"})


async def _asks_for_it(still_needed: Callable[[], Union[bool, Awaitable[bool]]],
                       session_id: str, what: str) -> bool:
    """``still_needed``'s answer, awaited where it is an awaitable. A guard that
    raises -- a session file read half-written, a PermissionError of a Windows
    replace -- counts as still needed: it used to end the whole wake."""
    try:
        needed = still_needed()
        if inspect.isawaitable(needed):
            needed = await needed
        return bool(needed)
    except Exception as error:
        logger.warning("Could not ask whether %s still waits for %s, ringing on: %s",
                       session_id, what or "finished work", error)
        return True


async def wake_session(system_config: Any, session_id: str, user_id: str,
                       what: str = "",
                       still_needed: Optional[Callable[[], Union[bool, Awaitable[bool]]]] = None,
                       started_by: Optional[str] = None) -> str:
    """Tell a session that something it has been waiting for is over.

    For work that outlives the turn which started it -- a sub-agent, a
    background command -- so its caller can end the turn instead of polling.
    ``what`` names the work in the log. Returns notify()'s status, or "" when
    nothing was done -- the opposite direction from ``wake_blocked``, where ""
    is the good answer.

    THIS IS FOR NEWS THAT LIVES IN NO STORE. notify() only rings a bell: a
    session that is held takes the marker at its next step (``take_pending``)
    because a pre-LLM hook is expected to hand the waiting input over. Nothing
    hands over "your background command finished", so that ring is lost. It is
    therefore repeated while the session stays held.

    ``still_needed`` is what keeps the repetition from costing a turn. Asked
    before every ring after the first, it ends the ringing once the caller has
    read the result by itself -- otherwise a ring can land after a wake run has
    already delivered and read the news, and start a second run for it. Pass it
    whenever the caller can tell; without it the loop rings its full budget. It
    may return an awaitable, for a caller that has to look the answer up -- the
    reader can be a run of another process, whose reading only its stored state
    shows. One that raises is logged and counts as still needed: ringing on costs
    at most a woken run, stopping would lose the news.

    A wake only reaches a session whose work still runs somewhere, and
    background work lives in the process that started it: the API, or an
    ``agent-cli chat`` whose prompt waits on the same loop, where the wake
    arrives at the prompt. A one-shot ``agent-cli run`` ends its turn and
    takes the work with it, and then nothing is left to wake anybody.

    Work of a run its user stopped rings nobody: the user stopped it, and the
    next run the user starts is no reason to deliver it either. The run is the
    one the caller's task inherited (tools/status.current_request_id) -- the
    work's task is started from the run's tool call, and its tool calls and
    sub-agents carry the run's id as a prefix. A task started outside any run
    (a sweeper) carries whichever run started it, or none, and a task that
    iterated another run's stream carries that one's -- a caller that knows the
    run passes it as ``started_by``. Work of another run
    rings on while the session is marked stopped, within the same budget: the
    user's next run lifts the mark.

    A failed wake costs its caller a poll, never the operation: by the time
    this runs the operation is over and recorded, and letting the failure
    through would have the caller's error handling record a finished job as
    failed. Cancellation is deliberately NOT caught -- a caller being torn
    down should not be held up over a message.
    """
    try:
        blocked = wake_blocked(system_config, session_id, user_id)
        if blocked:
            # By the REASON, not by a stand-in for it: presence being off is a
            # setting somebody chose, everything else is somebody asking for a
            # wake that was never going to happen -- including a reason added
            # later, which should be seen rather than quietly demoted.
            report = logger.debug if blocked == PRESENCE_OFF else logger.warning
            report("Not waking %s for %s: %s", session_id, what or "finished work", blocked)
            return ""
        presence = presence_for(system_config)
        if started_by is None:
            from ..tools.status import current_request_id
            started_by = current_request_id.get() or ""
        # notify() reads and writes lock files and may start a process. The
        # loop this runs on serves every other request of the process, so it
        # does not wait for that here.
        state, note = "", ""
        # One bound, the range: two would make neither of them measurable.
        for ring in range(WAKE_RETRIES + 1):
            if ring:
                # The previous ring found the session held, and the marker it
                # left will be taken by that session's next step without
                # anything handing the news over. Wait for it to let go.
                await asyncio.sleep(WAKE_RETRY_SECONDS)
                # Asked BEFORE ringing again, never after: a ring that goes out
                # while a wake run is already reading the news starts a SECOND
                # one, and that is a whole turn on the user's money.
                if still_needed is not None and not await _asks_for_it(still_needed, session_id, what):
                    # "not needed" covers read-by-its-caller AND gone-from-the
                    # registry (pruned, stopped). Naming only the first would be
                    # a reason this cannot know.
                    logger.debug("Stopped ringing %s for %s: nobody waits for it any more",
                                 session_id, what or "finished work")
                    return state
            # Before every ring, the first included: the user may start a run
            # meanwhile, which lifts the session's mark but does not want this.
            if stopped_by_user(started_by):
                state, note = "queued", STOPPED
                break
            state, note = await asyncio.to_thread(presence.notify, session_id, user_id)
            if state not in RING_AGAIN and note != STOPPED:
                break

        # notify() has exits that wake nobody -- a sub-agent's session, a wake
        # chain at its limit, a session that is not on disk. Reporting those as
        # "woke" made the only operator-visible signal read like success.
        # delivered_next_step counts as woken only once it is the LAST word:
        # the session let go, and release() wakes it on the marker. being_woken
        # is a run already on its way, which is the same good outcome as having
        # started one -- reported as a failure it would make the one signal an
        # operator has read like an error for the case that worked best.
        # ONE decision, used twice. `report is logger.info` was never true --
        # every attribute access on a logger builds a fresh bound method -- so
        # the line said "Did NOT wake" for a wake that worked.
        woke = state in ("woke_session", "delivered_next_step", "being_woken")
        report = logger.info if woke or note == STOPPED else logger.warning
        report("%s %s for %s: %s%s", "Woke" if woke else "Did NOT wake",
               session_id, what or "finished work", state, f" ({note})" if note else "")
        return state
    except Exception as e:
        logger.warning("Could not wake %s for %s: %s", session_id,
                       what or "finished work", e)
        return ""
