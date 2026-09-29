"""Writing a JSON file so that a reader, or the next start, sees the old content or the new one -- never a torn one."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

REPLACE_ATTEMPTS = 40
REPLACE_PAUSE_SECONDS = 0.025
LEFTOVER_AGE_SECONDS = 600


def write_json_atomically(path: Path, data: Any, *, attempts: int = REPLACE_ATTEMPTS) -> None:
    """Dump beside the file and swap it in. Opened with "w", a process killed mid-write left an empty or half file.

    Blocking, and with ``attempts`` > 1 it sleeps: call it from a worker thread, not the event loop. Windows refuses to
    replace a file someone is reading (the panel), so the swap is tried again ``attempts`` times, 25 ms apart.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _remove_leftovers(path)
    # A plain open, not tempfile: the file keeps the mode an open("w") gives (tempfile makes it 0600 on POSIX).
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8") as file:  # the close too: a full disk shows at the flush
            json.dump(data, file, indent=2)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    for attempt in range(attempts):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(REPLACE_PAUSE_SECONDS)


def _remove_leftovers(path: Path) -> None:
    """Temporaries of a process killed between dump and swap; old enough that no living writer still owns them."""
    cutoff = time.time() - LEFTOVER_AGE_SECONDS
    for leftover in path.parent.glob(f".{path.name}.*.tmp"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
        except OSError:
            pass  # removed by another writer already, or not ours to remove: the save still has to happen
