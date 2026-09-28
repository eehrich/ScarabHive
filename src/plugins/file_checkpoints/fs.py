"""The state of a path on disk, and the few operations a rewind performs on one.

A state is what a comparison needs: the kind of entry (absent, file, dir,
symlink) and, for a file or a link, a digest of its bytes. Only a state
recorded BEFORE a change also carries the bytes themselves (``snapshot``); the
states read afterwards (``fingerprint``) are compared, never restored.

Nothing here follows the last component of a path: a link is recorded,
compared and restored as a link, a file is replaced by renaming over the
directory entry, a delete unlinks. That is what lets the plugin check a path
against the allowed directories by its parent alone (``hooks.inside_roots``).
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ABSENT = "absent"
FILE = "file"
DIR = "dir"
SYMLINK = "symlink"
#: A path that could not be read at all (no permission, vanished mid-read).
UNREADABLE = "unreadable"

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class State:
    """One path, as it was at one moment."""

    kind: str
    #: sha256 of the file's bytes / of the link's target; None for absent and dirs.
    digest: Optional[str] = None
    #: Permission bits (``st_mode & 0o7777``) of a file or dir.
    mode: Optional[int] = None
    size: Optional[int] = None
    #: Whether the bytes are in the store (a file or link recorded before a change).
    kept: bool = False
    #: Why they are not, when they should have been.
    note: str = ""

    def same(self, other: Optional["State"]) -> bool:
        """Whether two states show the same content (mode is not compared)."""
        if other is None or self.kind == UNREADABLE or other.kind == UNREADABLE:
            return False
        return self.kind == other.kind and self.digest == other.digest


def _digest_file(path: Path, limit: int) -> Tuple[Optional[str], Optional[bytes]]:
    """(sha256, the bytes when there are at most ``limit`` of them)."""
    digest = hashlib.sha256()
    kept: Optional[bytearray] = bytearray()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            if kept is not None:
                if len(kept) + len(chunk) > limit:
                    kept = None
                else:
                    kept.extend(chunk)
    return digest.hexdigest(), (bytes(kept) if kept is not None else None)


def _read(path: Path, keep_up_to: int) -> Tuple[State, Optional[bytes]]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return State(ABSENT), None
    except OSError as exc:
        return State(UNREADABLE, note=f"cannot be read ({exc.strerror or exc})"), None
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.fsencode(os.readlink(path))
        except OSError as exc:
            return State(UNREADABLE, note=f"cannot be read ({exc.strerror or exc})"), None
        return State(SYMLINK, digest=hashlib.sha256(target).hexdigest(), size=len(target)), target
    if stat.S_ISDIR(info.st_mode):
        return State(DIR, mode=mode), None
    if not stat.S_ISREG(info.st_mode):
        return State(UNREADABLE, note="not a regular file, directory or link"), None
    try:
        digest, content = _digest_file(path, keep_up_to)
    except OSError as exc:
        return State(UNREADABLE, note=f"cannot be read ({exc.strerror or exc})"), None
    return State(FILE, digest=digest, mode=mode, size=info.st_size), content


def snapshot(path: Path, keep_up_to: int) -> Tuple[State, Optional[bytes]]:
    """The state before a change, with the bytes to restore it from.

    A file larger than ``keep_up_to`` is digested but not kept: the state says
    so (``kept=False`` and a note), and a rewind names the file instead of
    restoring it.
    """
    state, content = _read(path, keep_up_to)
    if state.kind in (FILE, SYMLINK):
        if content is None:
            return State(state.kind, digest=state.digest, mode=state.mode, size=state.size,
                         kept=False, note=f"larger than {keep_up_to} bytes, its content was not kept"), None
        return State(state.kind, digest=state.digest, mode=state.mode, size=state.size, kept=True), content
    return state, None


def fingerprint(path: Path) -> State:
    """The state now, for comparison only."""
    state, _ = _read(path, -1)
    return state


def walk(directory: Path, limit: int) -> Tuple[List[Path], bool]:
    """Every entry under ``directory`` (not the directory itself), parents
    before children, links not followed; (entries, complete). Stops at
    ``limit`` entries and says it did."""
    found: List[Path] = []
    for root, dirs, files in os.walk(directory, followlinks=False):
        dirs.sort()
        base = Path(root)
        for name in sorted(dirs) + sorted(files):
            found.append(base / name)
            if len(found) > limit:
                return found[:limit], False
    return found, True


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def spelled_on_disk(path: Path, listings: Optional[Dict[str, List[str]]] = None) -> Path:
    """``path`` with every existing component spelled as the directory stores it.

    On a case- and normalisation-insensitive file system (macOS' APFS) "Foo.txt"
    and "foo.txt", "Müller" composed and decomposed, are one file; the tool
    servers keep the caller's spelling, a directory walk yields the stored one.
    Recorded under two spellings, one file had two histories, and a rewind
    restored one over the other. A component that does not exist yet keeps the
    spelling given -- the one it will be stored under. ``listings`` caches the
    directories read, for one batch of paths.
    """
    listings = listings if listings is not None else {}
    parts = path.parts
    current = Path(parts[0])
    for index, name in enumerate(parts[1:], start=1):
        candidate = current / name
        try:
            wanted = os.lstat(candidate)
        except OSError:
            return current.joinpath(*parts[index:])
        key = str(current)
        entries = listings.get(key)
        if entries is None:
            try:
                entries = listings[key] = os.listdir(current)
            except OSError:
                entries = listings[key] = []
        if name not in entries:
            folded = _fold(name)
            for entry in entries:
                if _fold(entry) != folded:
                    continue
                try:
                    if os.path.samestat(os.lstat(current / entry), wanted):
                        name = entry
                        break
                except OSError:
                    continue
        current = current / name
    return current


def write_file(path: Path, content: bytes, mode: Optional[int]) -> None:
    """Put ``content`` at ``path`` in one rename: a reader never sees half of it,
    and a link at ``path`` is replaced, not written through."""
    # The name shortened: near the file system's limit a longer temp name fails.
    kept = os.fsencode(path.name)[:200].decode("utf-8", "ignore")
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{kept}.", suffix=".rewind")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(content)
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def remove(path: Path) -> None:
    """Remove a file, a link or an EMPTY directory -- never a tree."""
    info = os.lstat(path)
    if stat.S_ISDIR(info.st_mode):
        os.rmdir(path)
    else:
        os.unlink(path)
