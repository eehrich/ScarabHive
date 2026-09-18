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

from pathlib import Path
from typing import Iterable, Tuple

from ..paths import user_path

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
        # An attached file is named where the person stands, which after
        # enter_project() is no longer the working directory. user_path also
        # holds the ~ expansion this used to do here, and the reason it
        # cannot be the pathlib one.
        target = user_path(raw)
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


def greedy_attach_hint(paths: Iterable[str | Path],
                       command: str = "agent-cli run") -> str | None:
    """The line to print when ``--attach`` has probably eaten the request.

    ``--attach`` takes ``nargs="+"``, so argparse reads every word after it as
    one more path: ``--attach note.png "summarise this"`` hands over two files
    and leaves no request. What the person got for that was either "Either
    'request' or --list-sessions must be provided" or "Not a file: summarise
    this" -- both true, neither says what happened, and the second is the one
    the report called a trap.

    Only when the LAST entry is missing, which is the signature of the swallow;
    a mistyped path in the middle is a mistyped path. Returns None when there
    is nothing to say.
    """
    entries = list(paths or [])
    if not entries:
        return None
    last = str(entries[-1])
    if user_path(last).exists():
        return None
    return (f"--attach read {last!r} as a file name and there is no such file. "
            f"--attach takes EVERY following word as a path, so the request "
            f'has to come FIRST: {command} "<request>" --attach <path>')
