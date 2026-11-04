"""I/O utilities for safe file operations."""
import json
import logging
import os
import time
from pathlib import Path
from typing import IO, Any, Callable, Dict, Union
from uuid import uuid4


logger = logging.getLogger(__name__)


#: How often a JSON write or read rides out Windows' "being replaced" window
#: (PermissionError while another process swaps the file), and the first wait.
#: Every write below is a temp file plus ``os.replace``; while another PROCESS
#: replaces a file, Windows refuses to open or replace it for a few
#: milliseconds.
_WRITE_RETRIES = 5
_READ_RETRIES = 10
_FIRST_RETRY_DELAY = 0.005


def atomic_write_text(
    filepath: Union[str, Path],
    content: str,
    encoding: str = "utf-8",
    *,
    mkdir: bool = False,
    retries: int = _WRITE_RETRIES,
) -> None:
    """Write text to a file atomically: a unique temp file beside it, then ``os.replace``.

    The same mechanism as ``atomic_write_json`` (see there: unique temp file,
    retried OSError, no temp file left behind). The file is created readable
    by its owner only (0600), as the ``mkstemp`` this used to write through
    made it -- config/local.env is written here.

    Args:
        filepath: Target file path.
        content: Text content to write.
        encoding: Text encoding (default: utf-8).
        mkdir: Create the target's missing parent directories first.
        retries: Attempts before giving up.

    Raises:
        OSError: If the write or the rename fails.
    """
    _atomic_replace(Path(filepath), lambda handle: handle.write(content),
                    encoding=encoding, mode=0o600, mkdir=mkdir, retries=retries)


def atomic_write_json(
    path: Union[str, Path],
    data: Any,
    *,
    indent: int = 2,
    retries: int = _WRITE_RETRIES,
) -> None:
    """Write *data* as JSON to *path* atomically: a temp file, then ``os.replace``.

    The temp file is unique per write (``.<name>.<hex>.tmp`` next to the
    target), so two writers of the same file never write into each other's
    temp file. A PermissionError or other OSError -- on Windows, another
    process holding the file for a moment -- is retried with an exponential
    backoff; anything else (a value JSON cannot encode) fails at once. The
    temp file never outlives a failed write.

    Raises:
        OSError: when the write still fails after *retries* attempts, or
            failed for any other reason (the cause is chained).
    """
    _atomic_replace(Path(path), lambda handle: json.dump(data, handle, indent=indent, ensure_ascii=False),
                    encoding="utf-8", mode=0o666, mkdir=False, retries=retries)


def _atomic_replace(path: Path, write: Callable[[IO[str]], Any], *, encoding: str, mode: int,
                    mkdir: bool, retries: int) -> None:
    """``write`` into a temp file of this write's own beside *path*, then ``os.replace`` it onto *path*.

    *mode* is the new file's permission bits before the umask. An OSError is
    retried with an exponential backoff, anything else fails at once; either
    way the temp file is removed and the failure is an OSError naming the file.
    """
    if mkdir:
        path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid4().hex[:8]}.tmp"
    delay = _FIRST_RETRY_DELAY * 2
    for attempt in range(retries):
        try:
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), mode)
            with os.fdopen(fd, "w", encoding=encoding) as handle:
                write(handle)
            os.replace(temp_path, path)
            return
        except OSError as e:
            if attempt < retries - 1:
                logger.debug("File lock conflict writing %s (attempt %d/%d), retrying in %.2fms: %s",
                             path, attempt + 1, retries, delay * 1000, e)
                time.sleep(delay)
                delay *= 2
                continue
            temp_path.unlink(missing_ok=True)
            logger.error("Failed to write %s after %d retries: %s", path, retries, e)
            raise OSError(f"Failed to write {path.name} after {retries} retries: {e}") from e
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            logger.error("Failed to write %s: %s", path, e)
            raise OSError(f"Failed to write {path.name}: {e}") from e


def read_json_retrying(path: Union[str, Path]) -> Any:
    """``json.load`` of *path* that rides out Windows' "being replaced" window.

    The writers (``atomic_write_json``) retried that window; readers did not,
    so with several agent-cli processes on one user, ``create_session`` died
    on reading ``index.json`` (measured 21.09.2026: 8 processes x 25 sessions,
    a crashed process in both runs), and ``list_sessions`` read the moment as
    "no index" and started a full rebuild. FileNotFoundError is not retried:
    callers rely on it meaning "not there".
    """
    delay = _FIRST_RETRY_DELAY
    for attempt in range(_READ_RETRIES):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except PermissionError:
            if attempt == _READ_RETRIES - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def read_json_object(path: Union[str, Path], *, what: str = "") -> Dict[str, Any]:
    """A JSON object from disk, or ``{}`` for a file that is missing, unreadable
    or holds something other than an object.

    A missing file is the normal state of a store that has not been written
    yet and passes silently; anything else is logged as a warning naming
    *what* the file is.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("%scould not read %s (%s)", f"{what}: " if what else "", path, exc)
        return {}
    return data if isinstance(data, dict) else {}
