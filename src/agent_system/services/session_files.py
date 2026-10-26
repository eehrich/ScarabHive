"""Files an agent hands to the person in a session: an image it made, a sound, a video.

The chat shows an answer's Markdown, and loads no image from an address the text chose
(a remote image is how an injected page exfiltrates). A file handed over here gets an
address on this server instead -- ``/api/sessions/<id>/files/<name>`` -- served only to
whoever may see the session; the chat shows images from that address and nothing else,
the CLI turns it into the local file.

Copies, not references: the agent's original may be overwritten or cleaned up (the
ComfyUI outputs are), and what the person was shown must stay what the link opens.
Kept per user and session in ``session_files/`` beside the session store (``data/``
for the app's); deleting the session deletes them. Archiving does not: a restored
session shows its pictures again -- forgetting the archive deletes them.
"""
from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

from agent_system.services.session_paths import sanitize_user_id, validate_session_id

#: A handed-over file's name: one path segment, no leading dot.
FILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")

#: The address the chat shows images from; the CLI maps it back to the file.
URL_PATTERN = re.compile(r"^/api/sessions/([A-Za-z0-9_-]+)/files/([^/?#]+)$")


def root_beside(sessions_root: Path) -> Path:
    """Where a session store's handed-over files live: ``session_files/`` next to it."""
    return Path(sessions_root).parent / "session_files"


def default_root() -> Path:
    """The running app's: beside its session store, else beside the one every process uses
    (``sessions_dir``: AGENT_SESSION_STORAGE_PATH, else the data directory's) -- the CLI, a woken run."""
    from agent_system import app_state
    from agent_system.core.session_presence.presence import sessions_dir
    sessions = getattr(getattr(app_state, "session_service", None), "session_manager", None)
    return root_beside(sessions.storage_path if sessions is not None else sessions_dir())


def files_dir(root: Path, user_id: str, session_id: str) -> Path:
    return root / sanitize_user_id(user_id) / validate_session_id(session_id)


def url_of(session_id: str, name: str) -> str:
    return f"/api/sessions/{session_id}/files/{quote(name)}"


def _safe_name(name: str) -> str:
    """``name`` reduced to one harmless path segment (the extension kept)."""
    ascii_name = unicodedata.normalize("NFKD", Path(name).name).encode("ascii", "ignore").decode()  # Ärger -> Arger
    stem, dot, suffix = ascii_name.rpartition(".")
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem if dot else suffix).strip(" ._-") or "file"
    return f"{cleaned[:100]}.{re.sub(r'[^A-Za-z0-9]+', '', suffix)[:10]}" if dot else cleaned[:110]


def hand_over(root: Path, user_id: str, session_id: str, source: Path,
              name: Optional[str] = None) -> tuple[str, Path]:
    """Copy ``source`` into the session's files; its URL and the copy's path. A name already
    taken gets a number (``bg-2.png``): an earlier link keeps opening what it showed."""
    target_dir = files_dir(root, user_id, session_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    wanted = _safe_name(name or source.name)
    stem, dot, suffix = wanted.rpartition(".")
    stem, suffix = (stem, f".{suffix}") if dot else (wanted, "")
    target, n = target_dir / wanted, 1
    while target.exists():
        n += 1
        target = target_dir / f"{stem}-{n}{suffix}"
    shutil.copyfile(source, target)
    return url_of(session_id, target.name), target


def find(root: Path, user_id: str, session_id: str, name: str) -> Optional[Path]:
    """The handed-over file ``name`` of the session, or None (also for a name that is no file name)."""
    if not FILE_NAME_PATTERN.match(name):
        return None
    try:
        path = files_dir(root, user_id, session_id) / name
    except ValueError:
        return None
    return path if path.is_file() else None


def find_by_url(root: Path, url: str) -> Optional[Path]:
    """The local file a handed-over file's URL names, under any user -- the CLI's question: it runs
    beside the files and only maps an answer's link back to what it points at."""
    match = URL_PATTERN.match(url)
    if not match or not root.is_dir():
        return None
    session_id, name = match[1], unquote(match[2])
    for user_dir in root.iterdir():
        found = find(root, user_dir.name, session_id, name) if user_dir.is_dir() else None
        if found:
            return found
    return None


def remove(root: Path, user_id: str, session_id: str) -> None:
    """Delete the session's handed-over files (the session is gone)."""
    try:
        target = files_dir(root, user_id, session_id)
    except ValueError:
        return
    shutil.rmtree(target, ignore_errors=True)
