"""Bounding what goes back to a model or onto a status line.

Shared by the plugins that front another program over a socket or a
subprocess (blender, godot) and kept their own, drifting copies: blender's
result clip never cut a long string inside a mapping, so a reply carrying a
screenshot's base64 came back whole while the flag said "truncated".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["STATUS_LINE_LIMIT", "clip_value", "file_fingerprint", "middle_cut"]

#: What the status bus shows of one line (agent_system/tools/base.py caps there).
STATUS_LINE_LIMIT = 140


def middle_cut(line: str, limit: int = STATUS_LINE_LIMIT) -> str:
    """``line`` at most ``limit`` characters, cut in the MIDDLE.

    The bus cuts an over-long line wherever it lands, and the tail -- where
    the reason lives -- is what gets lost. Cutting the middle keeps both ends.
    """
    if len(line) <= limit:
        return line
    keep = limit - 5
    return line[: keep // 2] + " … " + line[-(keep - keep // 2):]


def file_fingerprint(path: Path) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` if the file is there, else None -- to tell whether a
    program wrote a file during a call, not merely that one exists."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str))


def clip_value(value: Any, limit: int) -> tuple[Any, str]:
    """``value`` within ``limit`` characters of JSON, and a note of what was
    dropped (``""``: nothing).

    A dict stays a dict -- the same field must not be an object on a small
    reply and a cut string on a large one. Its longest list is halved until
    the whole fits (``<key>_omitted`` counts the dropped entries), then
    oversized string fields are cut, then the biggest nested values are
    serialised and cut. A list is halved; a string, or anything else, is cut.
    """
    marker = f"…[truncated at {limit} characters]"
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= limit:
        return value, ""
    if isinstance(value, str):
        return value[:limit] + "\n" + marker, f"string cut to {limit} characters"
    if isinstance(value, dict):
        trimmed = dict(value)
        notes = []
        lists = sorted((k for k, v in trimmed.items() if isinstance(v, list)),
                       key=lambda k: len(trimmed[k]), reverse=True)
        for key in lists:
            items = trimmed[key]
            # Halve rather than pop: `len // 2` without a floor is what makes
            # this terminate (4 -> 2 -> 1 -> 0); one entry at a time would
            # re-serialise the mapping thousands of times.
            while items and _size(trimmed) > limit:
                items = items[: len(items) // 2]
                trimmed[key] = items
                trimmed[f"{key}_omitted"] = len(value[key]) - len(items)
            if f"{key}_omitted" in trimmed:
                notes.append(f"{key}: {trimmed[f'{key}_omitted']} of {len(value[key])} entries omitted")
            if _size(trimmed) <= limit:
                return trimmed, "; ".join(notes)
        budget = limit // 4
        for key, item in trimmed.items():
            if isinstance(item, str) and len(item) > budget:
                trimmed[key] = item[:budget] + marker
                notes.append(f"{key}: string cut to {budget} characters")
        # Still over: a nested structure (a scene tree is a dict of dicts, no
        # list anywhere). Cut the biggest values to a truncated JSON string,
        # largest first, until the whole fits. That one field changes type;
        # the alternative is an unbounded payload behind a flag saying it was
        # bounded.
        for key in sorted(trimmed, key=lambda k: _size(trimmed[k]), reverse=True):
            if _size(trimmed) <= limit:
                break
            if isinstance(trimmed[key], (dict, list)):
                trimmed[key] = json.dumps(trimmed[key], default=str)[:budget] + marker
                notes.append(f"{key}: serialised and cut to {budget} characters")
        return trimmed, "; ".join(notes) or "oversized scalars kept as they are"
    if isinstance(value, list):
        items = list(value)
        while items and _size(items) > limit:
            items = items[: len(items) // 2]
        return items, f"{len(value) - len(items)} of {len(value)} entries omitted"
    return text[:limit] + "\n" + marker, f"value cut to {limit} characters"
