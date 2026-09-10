"""Sorting attached files by kind, shared by the chat and both CLI entry points.

`/attach <path>` in the chat never asked which kind of file it is -- it reads
the extension and puts the file where it belongs. The command line made the
person do that sorting by hand across --images, --audio and --text, and a file
in the wrong bucket does not fail politely: a .txt handed to --images is
base64-encoded as an image, and a .png handed to --text is read as text.

So the CLI asks the same question the chat asks: `--attach` takes a file of any
kind and works out which one. What it cannot work out, it says -- it does not
guess and it does not silently drop.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Tuple

KINDS = ("image", "audio", "text")


def sort_attachments(paths: Iterable[str | Path]) -> Tuple[dict[str, list[str]], list[str]]:
    """Group paths by detected kind. Returns ``(kinds, problems)``.

    ``problems`` holds one human-readable line per file that cannot be sent --
    missing or of a kind the message builder has no place for. The caller
    decides whether that is a warning or a reason to stop; nothing is silently
    dropped either way.
    """
    from ..utils.multimodal_processor import detect_file_type

    kinds: dict[str, list[str]] = {kind: [] for kind in KINDS}
    problems: list[str] = []
    for raw in paths or []:
        # os.path.expanduser, not Path.expanduser: the pathlib one RAISES
        # RuntimeError("Could not determine home directory") for a name it
        # cannot resolve, and `~$notes.md` -- the lock file Word leaves next
        # to a document -- is such a name whenever USERNAME differs from the
        # profile directory, as it does on this machine. A file name is not
        # allowed to crash the listing of what could not be attached.
        target = Path(os.path.expanduser(str(raw)))
        if not target.is_file():
            problems.append(f"Not a file: {target}")
            continue
        kind = detect_file_type(target)
        if kind not in kinds:
            problems.append(
                f"Unsupported file type: {target} "
                f"({target.suffix or 'no extension'}) -- images, audio and "
                f"text files work.")
            continue
        kinds[kind].append(str(target))
    return kinds, problems
