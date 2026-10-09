"""The index of a session store: which sessions a user has, without opening them.

Per user, ``index.json`` holds a row per top-level session, and every parent
with sub-agents has a partition of its own, ``.subs.<parent>.index.json``, so
parallel ``agent-cli`` processes spawning sub-agents write different files.
The listings -- the whole user, the roots, one parent's children -- read these
rows, never the session files.

Its own module, apart from ``SessionManager``, because the index is a store
with rules of its own: an OS file lock per partition that spans processes, a
rebuild that only fills in, a partition that goes with its last child. The
manager decides WHEN it changes (after a write or a delete, under its own
``_lock``) and calls ``update_entry`` / ``remove_entry``; this class decides
how. It reads a session file only to rebuild a lost index, through the
manager's validating reader, and writes through the manager's atomic write.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from filelock import FileLock
from filelock import Timeout as LockTimeout

from ..utils.io import read_json_retrying
from .session_paths import user_dir_of, validate_session_id

logger = logging.getLogger(__name__)

#: How long an index edit waits for another process holding the same index.
#: An edit is one read and one write of a few hundred kilobytes; anything near
#: this long is a process that hangs, and an error beats hanging with it.
INDEX_LOCK_TIMEOUT = 30.0

#: What an edit of an index file does after ``change`` has seen it.
_WRITE, _DELETE, _SKIP = "write", "delete", "skip"

#: The partitions by name: the main index, and ``<SUBS_PREFIX><parent><SUBS_SUFFIX>``
#: for the children of one parent. The session archive's walk over a user
#: directory reads the tree structure off these names.
MAIN_INDEX = "index.json"
SUBS_PREFIX = ".subs."
SUBS_SUFFIX = ".index.json"


def extract_parent_id(session_data_or_metadata: Dict[str, Any]) -> Optional[str]:
    """Pull the parent session_id from a session-data or index-metadata dict.

    Sessions track their parent via ``parent_session = {"session_id": ..., "created_at": ...}``.
    Returns None for top-level sessions.
    """
    parent = session_data_or_metadata.get("parent_session")
    if isinstance(parent, dict):
        pid = parent.get("session_id")
        if isinstance(pid, str) and pid:
            return pid
    return None


def _read_partition(path: Path, what: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """One index file as it lies on disk -- None when it cannot be read or holds no object.

    The read behind every listing. A broken index is skipped with a warning
    naming ``what`` it is, never raised: it must not 500. Each caller decides
    what None means -- an empty partition, or a main index to heal.
    """
    try:
        data = read_json_retrying(path)
    except Exception as e:  # noqa: BLE001 — skip a broken index; it must not 500
        logger.warning("Failed to read %s %s: %s", what, path, e)
        return None
    return data if isinstance(data, dict) else None


class SessionIndex:
    """The index partitions of every user under one session store.

    ``read_session`` reads and validates one session file (a rebuild);
    ``write`` writes one file atomically. Both are the manager's, so an index
    file is written as a session file is.
    """

    def __init__(
        self,
        storage_path: Path,
        *,
        read_session: Callable[[Path], Awaitable[Dict[str, Any]]],
        write: Callable[[Path, Dict[str, Any]], None],
    ) -> None:
        self.storage_path = storage_path
        self._read_session = read_session
        self._write = write

    def path(self, user_id: str, parent_session_id: Optional[str] = None) -> Path:
        """Index file path. Top-level: ``<user>/index.json``. Sub-agents:
        ``<user>/.subs.<parent>.index.json`` (per-parent partition so parallel
        ``agent-cli`` processes write to different files, no contention).
        """
        user_dir = user_dir_of(self.storage_path, user_id)
        if parent_session_id:
            safe_parent = validate_session_id(parent_session_id)
            return user_dir / f"{SUBS_PREFIX}{safe_parent}{SUBS_SUFFIX}"
        return user_dir / MAIN_INDEX

    @staticmethod
    def lock_path(index_path: Path) -> Path:
        """The lock file of one index partition.

        NOT ``<name>.lock``: in a user directory every ``*.lock`` is a session
        presence lock, and ``SessionPresence.list_for_user`` probes each one --
        an index lock there would show up as a running session "index.json",
        and one nobody holds would be deleted as a leftover. Dot-prefixed, so
        the scans that skip hidden files skip it too.

        ponytail: on POSIX filelock leaves the file behind on release, so one
        empty ``.mutex`` stays per parent session that ever had children,
        also after its partition is gone. Deleting it with the partition is
        NOT the fix: unlinking a lock file another process may be opening
        breaks the exclusion on POSIX. If the count ever matters, sweep the
        ones whose partition is gone and whose tree is archived.
        """
        return index_path.parent / f".{index_path.name}.mutex"

    # -- changing a partition ------------------------------------------------

    def _edit_locked(
        self,
        user_id: str,
        parent_session_id: Optional[str],
        change: Callable[[Dict[str, Any]], str],
        missing: Optional[Dict[str, Any]],
        corrupt_is_missing: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Read one index partition, let ``change`` work on it, write it back.

        The one place an index is modified. Read-modify-write on a file several
        PROCESSES share -- the API and any number of agent-cli runs of one user
        -- loses whatever another one wrote in between: measured 21.09.2026,
        8 processes creating 25 sessions each, and 35 and 116 of the sessions
        that were on disk were in no index, invisible in the sidebar. The
        in-process ``_lock`` says nothing about the other processes; this OS
        lock does, and a process that dies drops it.

        Runs in a worker thread. ``change`` returns ``_WRITE``, ``_DELETE``
        (the partition goes, for a sub-index whose last child left) or
        ``_SKIP``. A missing file is ``missing`` to start from, or -- with
        ``missing`` None -- no edit at all and a None back, so the caller can
        rebuild outside the lock (a rebuild reads every session file; holding
        every other process off for that is not an option).
        """
        path = self.path(user_id, parent_session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.lock_path(path)), timeout=INDEX_LOCK_TIMEOUT)
        try:
            lock.acquire()
        except LockTimeout as exc:
            raise IOError(
                f"{path.name} of {user_id} is held by another process "
                f"(waited {INDEX_LOCK_TIMEOUT:.0f}s)") from exc
        try:
            try:
                data = read_json_retrying(path)
                if not isinstance(data, dict):
                    raise ValueError(f"{path.name} is not an object")
            except FileNotFoundError:
                if missing is None:
                    return None
                data = dict(missing)
            except ValueError:
                # Only the rebuild may throw an unreadable index away -- it is
                # the repair. Anyone else gets the error, as before.
                if not corrupt_is_missing or missing is None:
                    raise
                data = dict(missing)
            action = change(data)
            if action == _WRITE:
                self._write(path, data)
            elif action == _DELETE:
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to remove emptied index %s: %s", path, e)
            return data
        finally:
            lock.release()

    async def _edit(
        self,
        user_id: str,
        parent_session_id: Optional[str],
        change: Callable[[Dict[str, Any]], str],
        missing: Optional[Dict[str, Any]] = None,
        corrupt_is_missing: bool = False,
    ) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(
            self._edit_locked, user_id, parent_session_id, change, missing,
            corrupt_is_missing)

    async def rebuild(
        self,
        user_id: str,
        parent_session_id: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Rebuild a single index file from session files on disk.

        Args:
            user_id: User identifier.
            parent_session_id: If set, rebuild only this parent's sub-index
                (entries whose ``parent_session.session_id`` matches). Otherwise
                rebuild the main index (entries without a parent).

        Returns:
            Dict mapping session_id -> metadata for the rebuilt index.
        """
        user_dir = user_dir_of(self.storage_path, user_id)

        if not user_dir.exists():
            return {}

        index_data: Dict[str, Dict[str, Any]] = {}

        for session_file in user_dir.glob("*.json"):
            # Skip index files, backups, temp files
            if session_file.name in (MAIN_INDEX, 'index.tmp') or session_file.name.startswith('.'):
                continue

            try:
                session_data = await self._read_session(session_file)
                pid = extract_parent_id(session_data)
                # Only include entries that belong in this index partition
                if pid != parent_session_id:
                    continue

                index_data[session_data["session_id"]] = self.row(session_data)
            except Exception as e:
                logger.warning("Failed to read session %s for index rebuild: %s", session_file, e)
                continue

        if index_data:
            rebuilt = index_data

            def merge(current: Dict[str, Any]) -> str:
                # The scan took as long as it took -- minutes on a big user --
                # and whatever another process wrote meanwhile is newer than
                # what the scan read. So the scan only fills in; it never
                # overwrites a row, and never drops one it did not see.
                for sid, row in rebuilt.items():
                    current.setdefault(sid, row)
                return _WRITE

            index_data = await self._edit(
                user_id, parent_session_id, merge, missing={},
                corrupt_is_missing=True) or rebuilt
            logger.info(
                "Rebuilt %s index for user %s with %d sessions",
                ("sub-index for parent " + parent_session_id) if parent_session_id else "main",
                user_id, len(index_data),
            )

        return index_data

    @staticmethod
    def row(session_data: Dict[str, Any]) -> Dict[str, Any]:
        """The row a session gets in its index partition.

        One shape for create, save and reinstate: an index whose rows differ by
        which method wrote them is an index nobody can read.
        """
        metadata = session_data.get("metadata", {})
        row = {
            "session_id": session_data["session_id"],
            "user_id": session_data["user_id"],
            "title": session_data["title"],
            "created_at": session_data["created_at"],
            "updated_at": session_data["updated_at"],
            "agent_name": session_data["agent_name"],
            "llm_profile": session_data["llm_profile"],
            "message_count": metadata.get("message_count", 0),
            "last_agent_response": metadata.get("last_agent_response", ""),
            "tags": metadata.get("tags", []),
            "parent_session": session_data.get("parent_session"),
            "depth": session_data.get("depth", 1),
        }
        if extract_parent_id(session_data):
            # The runs of a sub-session, by the id each opened with (ChatMessage.request_id):
            # a session read back asks its children for them and hangs each under the
            # call it started from. Only sub-sessions: nothing looks up a top-level
            # session's runs, and index.json would grow by one id per turn.
            row["runs"] = [m["request_id"] for m in session_data.get("messages") or []
                           if isinstance(m, dict) and m.get("request_id")]
        return row

    async def update_entry(
        self,
        user_id: str,
        session_id: str,
        metadata: Dict[str, Any],
        parent_session_id: Optional[str] = None,
    ) -> None:
        """Update a single entry in the appropriate index (main or per-parent sub).

        ``parent_session_id`` is auto-detected from ``metadata.parent_session``
        if not supplied — top-level sessions go to main index, sub-agent
        sessions go to ``.subs.<parent>.index.json``.
        """
        if parent_session_id is None:
            parent_session_id = extract_parent_id(metadata)

        def put(index_data: Dict[str, Any]) -> str:
            index_data[session_id] = metadata
            return _WRITE

        # A missing sub-index partition means "no children", not "index lost":
        # create_session writes the parent link BEFORE the first index write,
        # so every child lands in the partition, and remove_entry
        # deletes the file exactly when the last one goes. Rebuilding it would
        # read every session file of the user to find what cannot be there --
        # measured 20.09.2026: 7 min 31 s for 60k files, to produce one entry.
        # A missing MAIN index is a lost one: rebuilt (outside the index lock,
        # it is a full scan), then the entry goes in on top of what it found.
        written = await self._edit(
            user_id, parent_session_id, put,
            missing={} if parent_session_id is not None else None)
        if written is None:
            await self.rebuild(user_id, parent_session_id)
            await self._edit(user_id, parent_session_id, put, missing={})

        # Migration cleanup: when an entry is now in a sub-index, ensure it's
        # not still lingering in main from an earlier write (sub-agents are
        # created via create_session first → land in main → then save_session
        # adds parent_session and routes to sub here). One-off cost on the
        # first save with a parent; subsequent saves no-op the main read.
        if parent_session_id is not None:
            def drop_from_main(main_idx: Dict[str, Any]) -> str:
                return _WRITE if main_idx.pop(session_id, None) is not None else _SKIP

            await self._edit(user_id, None, drop_from_main)

    async def remove_entry(
        self,
        user_id: str,
        session_id: str,
        parent_session_id: Optional[str],
    ) -> None:
        """Remove an entry from the index partition where it lives.

        ``parent_session_id`` MUST be the same value that was used when the
        entry was last written (None for top-level sessions, parent's id for
        sub-agents). The caller knows this from the session data — there's
        no fallback search to keep this on the fast path.
        """
        def drop(data: Dict[str, Any]) -> str:
            if data.pop(session_id, None) is None:
                return _SKIP
            if not data and parent_session_id:
                # Last child removed: delete the sub-index file instead of
                # persisting an empty {} — the file's existence doubles as the
                # sidebar's has_children signal, so a leftover empty partition
                # would render a phantom expand toggle forever.
                return _DELETE
            return _WRITE

        await self._edit(user_id, parent_session_id, drop)

    # -- reading it for the listings ----------------------------------------

    async def list_all(self, user_id: str) -> List[Dict[str, Any]]:
        """Every session of a user, newest first: ``SessionManager.list_sessions``."""
        user_dir = user_dir_of(self.storage_path, user_id)

        if not user_dir.exists():
            logger.debug("No sessions directory for user %s", user_id)
            return []

        # Read main index + every per-parent sub-index and merge.
        # Per-parent sub-indices live in ".subs.<parent>.index.json" — they
        # exist so that parallel agent-cli processes don't write-collide on
        # the main index. ``list_sessions`` is the unified view.
        try:
            index_data = await self._read_all(user_id)
            if not index_data:
                # Nothing read — either no index files at all (fresh user)
                # or all empty. Try a full rebuild as a last resort.
                index_data = await self.rebuild(user_id)

            sessions = list(index_data.values())
            sessions.sort(key=lambda s: s["updated_at"], reverse=True)
            logger.debug("Listed %d sessions for user %s", len(sessions), user_id)
            return sessions

        except Exception as e:
            # Read pipeline blew up — rebuild main index from disk.
            logger.warning("Index read failed for user %s, rebuilding main: %s", user_id, e)
            index_data = await self.rebuild(user_id)
            sessions = list(index_data.values())

            # Sort by updated_at (most recent first)
            sessions.sort(key=lambda s: s["updated_at"], reverse=True)

            logger.debug("Listed %d sessions for user %s after rebuild", len(sessions), user_id)
            return sessions

    async def list_roots(self, user_id: str) -> List[Dict[str, Any]]:
        """Top-level sessions, newest first, flagged ``has_children``: ``SessionManager.list_root_sessions``."""
        user_dir = user_dir_of(self.storage_path, user_id)
        if not user_dir.exists():
            return []
        index_data = await self._read_main(user_id)
        if index_data is None:
            # Main index missing or unreadable — same self-heal as the old
            # pipeline: rebuild it from the session files on disk (expensive,
            # but only on actual index loss; rebuild persists the
            # result, so the next call is fast again). A parsed-but-empty
            # index is trusted and does NOT trigger this (deletes empty it
            # legitimately — rebuilding every call would reintroduce the
            # full-scan cost this method exists to avoid).
            index_data = await self.rebuild(user_id)
        sessions = list(index_data.values())
        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        self._annotate_children_flag(user_id, sessions)
        logger.debug("Listed %d root sessions for user %s", len(sessions), user_id)
        return sessions

    async def list_children(
        self, user_id: str, parent_session_id: str, annotate_children: bool = True
    ) -> List[Dict[str, Any]]:
        """Direct children of one parent, newest first: ``SessionManager.list_child_sessions``."""
        path = self.path(user_id, parent_session_id)  # validates the id
        if not path.exists():
            return []

        def read_and_shape() -> List[Dict[str, Any]]:
            index_data = _read_partition(path, "sub-index") or {}
            # A sub-index is a partition of ONE parent, but stay strict: only return
            # entries that actually name this parent.
            children = [
                meta for meta in index_data.values()
                if extract_parent_id(meta) == parent_session_id
            ]
            children.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
            if annotate_children:
                self._annotate_children_flag(user_id, children)
            return children

        # The flag is one stat per child, and it used to run here, on the event loop,
        # AFTER the read came back from its thread: 1127 children of one node measured
        # 11 ms of contiguous block, which every SSE stream in the process waits out.
        # It is stat work like the read, so it belongs in the same thread.
        return await asyncio.to_thread(read_and_shape)

    def has_children(self, user_id: str, session_id: str) -> bool:
        """Whether a session has sub-sessions — one stat on its per-parent
        sub-index, no file read. The size guard (> 4 bytes; an empty index
        serialises to ``{}``) keeps a stale emptied sub-index from producing a
        phantom expand toggle."""
        try:
            st = self.path(user_id, session_id).stat()
        except (OSError, ValueError):
            return False
        return st.st_size > 4

    def _annotate_children_flag(self, user_id: str, sessions: List[Dict[str, Any]]) -> None:
        """Add ``has_children`` so the UI can render an expand toggle without
        shipping the descendants."""
        for s in sessions:
            sid = s.get("session_id")
            s["has_children"] = bool(sid) and self.has_children(user_id, sid)

    async def _read_all(self, user_id: str) -> Dict[str, Dict[str, Any]]:
        """Read main index plus all per-parent sub indices and merge.

        Used by list_sessions for a unified view across partitions.
        Reads only the small index files, not every session blob.
        """
        user_dir = user_dir_of(self.storage_path, user_id)
        if not user_dir.exists():
            return {}

        merged: Dict[str, Dict[str, Any]] = {}

        candidates = [user_dir / MAIN_INDEX] + sorted(user_dir.glob(f"{SUBS_PREFIX}*{SUBS_SUFFIX}"))
        for path in candidates:
            if not path.exists():
                continue
            data = await asyncio.to_thread(_read_partition, path, "index file")
            merged.update(data or {})
        return merged

    async def _read_main(self, user_id: str) -> Optional[Dict[str, Dict[str, Any]]]:
        """Read ONLY the main index (top-level sessions).

        Deliberately skips the per-parent ``.subs.*.index.json`` partitions —
        those hold exclusively sub-sessions.

        Returns ``None`` when the index needs healing (file missing or
        unparseable) — callers decide whether to rebuild. A parsed-but-empty
        index returns ``{}`` and is trusted: deletes legitimately empty it.
        """
        path = self.path(user_id)
        if not path.exists():
            return None
        return await asyncio.to_thread(_read_partition, path, "main index")
