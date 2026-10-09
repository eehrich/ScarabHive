"""The processes presence deals with: whether one still runs, and the woken run.

``alive`` tells a process that runs from one that is gone, or was replaced by
another under the same pid (coding_cli asks it about its own runs as well).
The rest is what starting a woken run takes: its depth in the wake chain, the
agent-cli command, the log its stderr goes to, and the start itself. Its own
module because it is the one part that starts anything: SessionPresence.notify
(presence.py) decides WHETHER a session is woken, and this is HOW.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import psutil

from ... import own_console
from ...config import settings as config_settings
from ...paths import PROJECT_ROOT

if os.name == "nt":
    import ctypes
    import msvcrt

    # A handle is a pointer: read back as the default c_int it would be cut in
    # half on 64-bit, and the file the cut value names is not the one opened.
    _CreateFileW = ctypes.windll.kernel32.CreateFileW
    _CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                             ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    _CreateFileW.restype = ctypes.c_void_p

logger = logging.getLogger(__name__)

WAKE_DEPTH_ENV = "HIVE_WAKE_DEPTH"
WAKE_TASK = "You were woken because input is waiting for this session. Read it and act on it."


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
        path = PROJECT_ROOT / "logs" / "agent-wake.log"
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
    env = {**os.environ, WAKE_DEPTH_ENV: str(depth)}
    from ...auth import security  # here only: the auth stack is no concern of a process that never wakes
    if security.AUTH_ENFORCED or config_settings.AUTH_REQUIRED_BY_WAKER:
        # The run reads the config from disk, where auth may be off by now while this API still enforces it
        # (until a restart): it acts for a user of this API and judges them as the API does (config.settings).
        # A run such an API woke passes it on to the runs it wakes.
        env[config_settings.AUTH_REQUIRED_ENV] = "1"
    errors = _wake_log()
    try:
        # out of the job an API on a console of its own dies with (own_console)
        process = own_console.popen_outliving(
            wake_command(session_id, user_id), cwd=PROJECT_ROOT,
            env=env,
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
