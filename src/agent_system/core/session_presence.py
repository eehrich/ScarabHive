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
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import psutil

from ..config.models import SessionPresenceConfig

if os.name == "nt":
    import msvcrt
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
_BINARY = getattr(os, "O_BINARY", 0)


class SessionBusy(RuntimeError):
    """Another process runs the session, so this one must not write it too."""

    def __init__(self, session_id: str, agent: str = ""):
        self.session_id = session_id
        self.agent = agent
        super().__init__(f"Session {session_id} is running in another process"
                         + (f" (agent {agent})" if agent else ""))


def sessions_dir() -> Path:
    """Where the session files are, by the API's rule (app.py)."""
    return Path(os.getenv("AGENT_SESSION_STORAGE_PATH") or REPO_ROOT / "data" / "sessions")


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


def spawn_wake(session_id: str, user_id: str, depth: int) -> tuple[int, float]:
    """Start the woken run on its own, out of sight; returns its (pid, create_time).

    CREATE_NO_WINDOW, not DETACHED_PROCESS: a detached process has no console,
    and Windows gives every console program it starts a window of its own --
    a woken run would open one per stdio MCP server, in the user's face.
    """
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
              if os.name == "nt" else {"start_new_session": True})
    process = subprocess.Popen(
        wake_command(session_id, user_id), cwd=REPO_ROOT,
        env={**os.environ, WAKE_DEPTH_ENV: str(depth)},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **detach)
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

    Bounded on purpose: every attempt means the file was deleted between the
    open and the lock, and a filesystem that keeps answering that way would
    spin here forever instead of letting the run report it.
    """
    for _ in range(attempts):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | _BINARY)
        if not _lock(fd):
            os.close(fd)
            return None
        if _is_file_at(fd, path):
            return fd
        # Deleted between the open and the lock (only POSIX deletes an open
        # file): that file no longer belongs to the session.
        _unlock_and_close(fd)
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
            raise SessionBusy(session_id, state["agent"])
        if time.monotonic() >= deadline:
            raise SessionBusy(session_id)
        time.sleep(_RETRY_WAIT)


class SessionPresence:
    """The lock files under one sessions directory, and the wake rules over them."""

    def __init__(self, root: str | Path, max_wake_depth: int = 3):
        self.root = Path(root)
        self.max_wake_depth = max_wake_depth
        self._held: dict[Path, list[int]] = {}  # lock file -> [handle, holds] of this process
        self._guard = threading.Lock()

    def _user_dir(self, user_id: str) -> Path:
        # SessionManager._sanitize_user_id
        return self.root / user_id.replace("..", "_").replace("/", "_").replace("\\", "_")

    def _lock_path(self, session_id: str, user_id: str) -> Optional[Path]:
        if not session_id or not user_id or not _SESSION_ID.match(session_id):
            return None
        return self._user_dir(user_id) / f"{session_id}.lock"

    def hold(self, session_id: str, user_id: str, agent_name: str) -> bool:
        """This process has the session in hand. Holds nest.

        Raises SessionBusy while another process runs the session: two runs
        would both write the conversation and the last save would win. False
        means the disk refused -- the run goes on unheld rather than not at all.
        """
        path = self._lock_path(session_id, user_id)
        if path is None:
            return False
        with self._guard:
            if path in self._held:
                self._held[path][1] += 1
                return True
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = _take(path, session_id)
            except OSError as exc:
                logger.warning("Session presence: could not hold %s: %s", session_id, exc)
                return False
            try:
                _write(fd, {"agent": agent_name, "since": time.time()})
            except OSError:
                pass  # the lock is what counts; the content only names the agent
            self._held[path] = [fd, 1]
            return True

    def release(self, session_id: str, user_id: str) -> None:
        """Undo one hold. The last one lets the session go, and input waiting
        for it wakes it."""
        path = self._lock_path(session_id, user_id)
        with self._guard:
            entry = self._held.get(path)
            if entry is None:
                return
            entry[1] -= 1
            if entry[1]:
                return
            del self._held[path]
            try:
                _drop(entry[0], path)
            except OSError as exc:
                logger.warning("Session presence: releasing %s: %s", session_id, exc)
        if path.with_suffix(".pending").exists():
            logger.info("Session %s let go with input waiting; waking it", session_id)
            try:
                self.notify(session_id, user_id)
            except OSError as exc:
                # Callers let go in a finally: a disk that refuses here would
                # replace the answer they are on their way out with, or mask
                # the exception already in flight. hold() guards the same way.
                logger.warning("Session presence: waking %s: %s", session_id, exc)

    def take_pending(self, session_id: str, user_id: str) -> None:
        """A step of the session is about to hand the waiting input over."""
        path = self._lock_path(session_id, user_id)
        if path is not None:
            _unlink(path.with_suffix(".pending"))

    def pending(self, session_id: str, user_id: str) -> bool:
        """Whether input is waiting for the session. A woken run asks before it
        starts: whoever held the session meanwhile may have taken it over."""
        path = self._lock_path(session_id, user_id)
        return path is not None and path.with_suffix(".pending").exists()

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
        of delivered_next_step, woke_session, queued (the note says why), unknown."""
        path = self._lock_path(session_id, user_id)
        stored_path = path.with_suffix(".json") if path is not None else None
        if path is None or not (path.exists() or stored_path.exists()):
            if path is not None:
                # A session that ran but never reached disk left its marker here.
                _unlink(path.with_suffix(".pending"))
            return "unknown", "no such session"
        pending = path.with_suffix(".pending")
        pending.touch()  # before the lock: a holder letting go right now still finds it
        keep = False     # the lock file stays: a run of this session is on its way
        waiting = True   # the marker stays: that run has not taken the input yet
        try:
            fd = _open_locked(path)
            if fd is None:
                return "delivered_next_step", ""
            try:
                content = _read(fd)
                if alive(content.get("wake_pid"), content.get("wake_started")):
                    keep = True
                    return "delivered_next_step", ""  # woken a moment ago; that run reads it
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
