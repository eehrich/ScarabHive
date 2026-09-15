"""Writing a JSON file so that a reader, or the next start, sees the old content or the new one -- never a torn one."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

REPLACE_ATTEMPTS = 40
REPLACE_PAUSE_SECONDS = 0.025


def write_json_atomically(path: Path, data: Any, *, attempts: int = REPLACE_ATTEMPTS) -> None:
    """Dump beside the file and swap it in. Opened with "w", a process killed mid-write left an empty or half file.

    Blocking, and with ``attempts`` > 1 it sleeps: call it from a worker thread, not the event loop. Windows refuses to
    replace a file someone is reading (the panel), so the swap is tried again ``attempts`` times, 25 ms apart.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    file = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
                                       delete=False)
    temporary = Path(file.name)
    try:
        with file:  # the close too: a full disk shows when the buffer is flushed
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
