"""The OS locks on the lock files, and the few bytes written into them.

A lock file is held by an OS lock on it, not by its existence: flock on POSIX,
msvcrt.locking on Windows, and the OS lets go however the process ends. Nothing
here asks which session a file belongs to: it opens and locks a file another
process may delete at any moment, deletes one it holds, and reads and writes the
JSON in it. Its own module because every platform difference of the locking is
in it: the rules over the files (presence.py) read the same on both.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# Windows locks are mandatory: the lock sits on a byte far past the content,
# so other processes can still read what the file says.
_LOCK_BYTE = 1 << 30
# A delete is through in microseconds, and this wait is spent under the store's
# own guard: short, and often enough that twenty of them are still a fifth of a
# retry.
_DELETE_WAIT = 0.002
_BINARY = getattr(os, "O_BINARY", 0)


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
