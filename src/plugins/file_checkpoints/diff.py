"""What one checkpoint changed in one file, as a unified diff.

Only the state BEFORE a change is copied into the record; the state after it
is known by its digest. The bytes after are found by that digest: in the
record (a later change of the same file kept them as its "before") or on
disk, as long as the file there still has them. Neither -- the file was
changed again outside the record -- and the diff says so instead of showing
the wrong text.

``contents`` reads (under the record's lock: blobs go when old turns are
forgotten); ``render`` diffs, outside it.
"""
from __future__ import annotations

import difflib
import os
import re
import stat
from pathlib import Path
from typing import Optional, Tuple

from . import fs
from .fs import ABSENT, DIR, FILE, SYMLINK, State
from .store import Journal

#: Lines of diff text served at most; the rest is cut and said to be.
MAX_DIFF_LINES = 4000
#: A line is what ends in "\n" -- not every boundary str.splitlines() knows (form feed, U+2028, a lone CR).
_LINES = re.compile(r"[^\n]*\n|[^\n]+\Z")


def _on_disk(path: str, state: State, max_bytes: int) -> Tuple[Optional[bytes], str]:
    """(the file's bytes when it is still the regular file ``state`` describes, why not)."""
    try:
        info = os.lstat(path)
    except OSError:
        return None, "its content was not kept, and the file is gone since"
    # A link, a FIFO, a file too big to have been kept or of another size than recorded (an
    # "after" carries none) cannot hold those bytes: not opened at all.
    if not stat.S_ISREG(info.st_mode):
        return None, "its content was not kept, and the path is no regular file now"
    if info.st_size > max_bytes:
        return None, f"larger than {max_bytes} bytes, so its content was not kept"
    if state.size is not None and info.st_size != state.size:
        return None, "its content was not kept, and the file has changed since"
    try:
        digest, content = fs._digest_file(Path(path), max_bytes)
    except OSError:
        return None, "its content was not kept, and the file cannot be read now"
    if digest != state.digest:
        return None, "its content was not kept, and the file has changed since"
    return content, ""


def _bytes_of(journal: Journal, state: State, path: str, max_bytes: int) -> Tuple[Optional[bytes], str]:
    """(the content of ``state``, why it is not available)."""
    if state.kind == ABSENT:
        return b"", ""
    if state.kind == DIR:
        return None, "a directory"
    if state.kind not in (FILE, SYMLINK) or not state.digest:
        return None, state.note or f"{state.kind}, no content recorded"
    blob = journal.blob_dir / state.digest
    if blob.is_file():
        return blob.read_bytes(), ""
    if state.note:           # a "before" too big to copy says so itself
        return None, state.note
    if state.kind == SYMLINK:
        return None, "a link whose target was not kept"
    return _on_disk(path, state, max_bytes)


def contents(journal: Journal, path: str, before: State, after: Optional[State],
             max_bytes: int) -> Tuple[Optional[bytes], Optional[bytes], str]:
    """(bytes before, bytes after, why one of them is missing)."""
    if after is None:
        return None, None, "The call's result was never recorded."
    old, why_old = _bytes_of(journal, before, path, max_bytes)
    new, why_new = _bytes_of(journal, after, path, max_bytes)
    if old is None or new is None:
        side = "before" if old is None else "after"
        return old, new, f"No content {side} this change: {why_old or why_new}."
    return old, new, ""


def _text(content: bytes) -> Optional[str]:
    if b"\0" in content[:8192]:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def render(old: Optional[bytes], new: Optional[bytes], note: str) -> dict:
    """``{"diff": str|None, "note": str, "truncated": bool}`` -- the note says why there is no diff."""
    if old is None or new is None:
        return {"diff": None, "note": note, "truncated": False}
    old_text, new_text = _text(old), _text(new)
    if old_text is None or new_text is None:
        return {"diff": None, "note": " ".join(filter(None, [f"Binary file ({len(old)} -> {len(new)} bytes).", note])),
                "truncated": False}
    lines = list(difflib.unified_diff(_LINES.findall(old_text), _LINES.findall(new_text),
                                      fromfile="before", tofile="after"))
    truncated = len(lines) > MAX_DIFF_LINES
    text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in lines[:MAX_DIFF_LINES])
    return {"diff": text, "note": note, "truncated": truncated}
