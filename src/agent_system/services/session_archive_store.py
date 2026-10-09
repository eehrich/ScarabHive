"""The session archive on disk: a directory per user, a manifest, one zip per tree.

    data/session_archive/<user>/index.json          the manifest, one entry per archived tree
    data/session_archive/<user>/index.json.lock     lock for every change to the manifest
    data/session_archive/<user>/.sweep.lock         lock for a whole pass
    data/session_archive/<user>/<YYYY-MM>/<root>.zip

Its own module because this is the format -- where each file is, how the
manifest is changed under its cross-process lock, how a zip is written,
verified and read back -- while ``SessionArchive`` (services/session_archive.py)
is the policy over it: which trees go and when, and what has to hold before
anything of the live store is deleted. Nothing here touches the live store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Set

from filelock import FileLock
from filelock import Timeout as LockTimeout

from ..utils.io import atomic_write_json, read_json_object
from .session_paths import user_dir_of

logger = logging.getLogger(__name__)

MANIFEST_NAME = "index.json"
ZIP_MANIFEST_MEMBER = "_manifest.json"
#: What an archive is called while it is being written. A pass that is
#: killed leaves one behind; the next pass over that tree overwrites it,
#: and a sweep clears the ones no tree will come back to.
TMP_SUFFIX = ".tmp"
#: How long a leftover has to lie still before a sweep drops it. The sweep
#: lock keeps every process that has it out, but not one still running the
#: code from before it -- an API that was pulled and not restarted.
TMP_MIN_AGE_SECONDS = 3600.0
#: The two cross-process locks, both per user and both next to what they
#: guard: one for a whole pass, one for the read-modify-write on the
#: manifest. Separate on purpose -- a pass runs for minutes and holds the
#: first the whole time, while it takes the second once per tree, and a
#: restore or a delete takes only the second.
SWEEP_LOCK_NAME = ".sweep.lock"
MANIFEST_LOCK_SUFFIX = ".lock"
#: How long a manifest write waits for another process. Long enough for a
#: write that is a few hundred kilobytes of JSON, short enough that a lock
#: nobody releases turns into an error instead of a hang.
MANIFEST_LOCK_TIMEOUT = 20.0


class ArchiveError(Exception):
    """An archive could not be written, read or restored."""


class ArchiveNotFound(ArchiveError):
    """No archive for that session -- the caller asked about the wrong one.

    Separate from its parent so the HTTP layer can tell "you asked about
    something that is not there" (404) from "what you asked for cannot be done
    right now" (409) without reading the message.
    """


class ArchiveBusy(ArchiveError):
    """Another process holds a lock this needs -- a sweep, or the index.

    Separate because it is about the USER's archive, not about one tree: a
    pass that meets it for one tree would meet it for every tree after, each
    after the same timeout, so the pass stops there instead of trying on.
    Still an ArchiveError, so every caller answers it as it answers a refusal
    (409 in the panel, one line in the CLI).
    """


def _read_json(path: Path) -> Dict[str, Any]:
    """A JSON object from disk, or {} for anything unreadable (no manifest yet is the normal state)."""
    return read_json_object(path, what="session archive")


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)


class ArchiveStore:
    """The archives of every user under one archive directory."""

    def __init__(self, archive_path: Path) -> None:
        self.archive_path = archive_path
        # In front of the manifest's file lock, so tasks of THIS process queue
        # without touching the filesystem. The file lock is what makes the
        # edit safe; this one keeps it uncontended in the common case.
        self._manifest_lock = asyncio.Lock()

    # -- paths ---------------------------------------------------------------

    def user_dir(self, user_id: str) -> Path:
        return user_dir_of(self.archive_path, user_id)

    def manifest_path(self, user_id: str) -> Path:
        return self.user_dir(user_id) / MANIFEST_NAME

    def archive_file(self, user_id: str, relative: str) -> Path:
        """The zip named by a manifest entry -- checked to stay in the user's dir.

        ``relative`` is read back from ``index.json``, and one of the two
        things done with it is ``unlink``. A file is not a trusted input just
        because this process wrote it once.
        """
        base = self.user_dir(user_id).resolve()
        path = (base / str(relative or "")).resolve()
        if path == base or base not in path.parents:
            raise ArchiveNotFound(f"Archive path {relative!r} is not in this archive")
        return path

    def sweep_lock(self, user_id: str) -> FileLock:
        """The lock a pass over this user's sessions holds while it runs (``SWEEP_LOCK_NAME``)."""
        lock_path = self.user_dir(user_id) / SWEEP_LOCK_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(lock_path), timeout=0)

    def drop_leftovers(self, user_id: str) -> None:
        """Delete the half-written archives (``TMP_SUFFIX``) of this user that have gone cold."""
        cold = time.time() - TMP_MIN_AGE_SECONDS
        for stale in self.user_dir(user_id).glob(f"*/*{TMP_SUFFIX}"):
            try:
                if stale.stat().st_mtime > cold:
                    continue
                stale.unlink()
                logger.info("session archive: dropped a leftover %s", stale.name)
            except OSError as exc:
                logger.debug("session archive: leftover %s (%s)", stale.name, exc)

    # -- the manifest --------------------------------------------------------

    async def manifest(self, user_id: str) -> Dict[str, Any]:
        """The user's manifest as it is on disk, ``{}`` when there is none.

        No lock: the manifest is only ever replaced whole, by ``os.replace``,
        so a reader sees the file before or after a write and never during.
        """
        return await asyncio.to_thread(_read_json, self.manifest_path(user_id))

    async def entry_of(self, user_id: str, root: str) -> Optional[Dict[str, Any]]:
        """What the manifest already holds for this tree, if anything."""
        manifest = await self.manifest(user_id)
        entry = manifest.get(root)
        return entry if isinstance(entry, dict) else None

    def _edit_manifest_locked(self, user_id: str, change: Callable[[Dict[str, Any]], Any]) -> Any:
        """Read the manifest, let ``change`` work on it, write it back.

        The one place the manifest is modified, and the reason it is one
        place: read-modify-write on a file two processes share loses whatever
        the other one wrote in between. The API sweeps on a timer while the
        CLI sweeps on demand, and both register every tree they take -- the
        loser's archive would sit on disk with nothing pointing at it, which
        no later pass repairs, because the sessions it held are gone.

        The lock file is what ``session_presence`` uses one directory over and
        what ``okf`` uses for its own read-modify-write: an OS lock, so a
        process that dies drops it. It is taken for the length of one edit,
        never across an await.

        Runs in a worker thread (``edit_manifest`` uses ``asyncio.to_thread``):
        the JSON is a few hundred kilobytes on a busy user, and waiting for
        another process must not stop the event loop.

        ``change`` may raise -- ``forget`` does, for a tree the manifest does
        not have -- and then nothing is written.
        """
        path = self.manifest_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(path) + MANIFEST_LOCK_SUFFIX, timeout=MANIFEST_LOCK_TIMEOUT)
        try:
            lock.acquire()
        except LockTimeout as exc:
            raise ArchiveBusy(
                f"the archive index of {user_id} is held by another process "
                f"(waited {MANIFEST_LOCK_TIMEOUT:.0f}s)") from exc
        try:
            manifest = _read_json(path)
            result = change(manifest)
            _write_json_atomic(path, manifest)
            return result
        finally:
            lock.release()

    async def edit_manifest(self, user_id: str, change: Callable[[Dict[str, Any]], Any]) -> Any:
        """``_edit_manifest_locked`` in a worker thread, behind the in-process lock; returns what ``change`` did."""
        async with self._manifest_lock:   # in-process; the file lock spans processes
            return await asyncio.to_thread(self._edit_manifest_locked, user_id, change)

    async def remember(self, user_id: str, root: str, entry: Dict[str, Any]) -> None:
        """Put one tree into the user's manifest."""
        def change(manifest: Dict[str, Any]) -> None:
            manifest[root] = entry

        await self.edit_manifest(user_id, change)

    # -- the zips ------------------------------------------------------------

    @staticmethod
    def write_zip(
        target: Path, sessions: List[str], files: Dict[str, Path], entry: Dict[str, Any],
    ) -> tuple[int, int]:
        """Write the tree -- ``sessions``, read from ``files`` -- to ``target``
        atomically; read it back before it counts.

        An archive that is already there is ADDED TO, never replaced: it can
        only be one an earlier pass wrote and did not get fully deleted, and
        the sessions in it may exist nowhere else. That decision is made from
        the FILE, not from the manifest -- a manifest that was lost or could
        not be parsed would otherwise turn into overwritten archives, which is
        the one failure here that cannot be undone. A restore takes the zip
        away, so a restored tree starts a fresh one.

        Returns ``(bytes, sessions)`` of the finished archive.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + TMP_SUFFIX)
        try:
            already: Set[str] = set()
            if target.is_file():
                shutil.copy2(target, tmp)
                with zipfile.ZipFile(tmp) as archive:
                    already = set(archive.namelist())
            else:
                tmp.unlink(missing_ok=True)

            expected = set(already)
            mode: Literal["a", "w"] = "a" if already else "w"
            with zipfile.ZipFile(tmp, mode, zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for session_id in sessions:
                    member = f"{session_id}.json"
                    path = files[session_id]
                    if member in already or not path.exists():
                        continue  # already archived, or deleted between the scan and now
                    archive.write(path, arcname=member)
                    expected.add(member)
                if ZIP_MANIFEST_MEMBER not in already:
                    # Without "bytes" and "session_count": a file cannot state
                    # its own size, and on a later add the count would be
                    # stale. Whoever rebuilds the index from the archives reads
                    # those two off the zip itself.
                    described = {key: value for key, value in entry.items()
                                 if key not in ("bytes", "session_count")}
                    archive.writestr(
                        ZIP_MANIFEST_MEMBER,
                        json.dumps({**described, "sessions": sessions},
                                   ensure_ascii=False, indent=2),
                    )
                    expected.add(ZIP_MANIFEST_MEMBER)
            # Read it back from disk: a zip that cannot be opened is not a backup,
            # and everything after this deletes the only other copy.
            with zipfile.ZipFile(tmp) as archive:
                written = set(archive.namelist())
            missing = expected - written
            if missing:
                raise ArchiveError(f"archive {target.name} is missing {len(missing)} sessions")
            size = tmp.stat().st_size
            os.replace(tmp, target)
            return size, len(written - {ZIP_MANIFEST_MEMBER})
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def read_zip_sessions(zip_path: Path) -> List[Dict[str, Any]]:
        """The session documents of an archive, root first."""
        if not zip_path.is_file():
            raise ArchiveNotFound(f"Archive {zip_path} is gone")
        sessions: List[Dict[str, Any]] = []
        order: List[str] = []
        try:
            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())
                if ZIP_MANIFEST_MEMBER in names:
                    inner = json.loads(archive.read(ZIP_MANIFEST_MEMBER).decode("utf-8"))
                    order = [str(sid) for sid in inner.get("sessions", [])]
                ordered = [f"{sid}.json" for sid in order if f"{sid}.json" in names]
                ordered += sorted(names - set(ordered) - {ZIP_MANIFEST_MEMBER})
                for member in ordered:
                    data = json.loads(archive.read(member).decode("utf-8"))
                    if isinstance(data, dict) and data.get("session_id"):
                        sessions.append(data)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise ArchiveError(f"Archive {zip_path.name} is unreadable: {exc}") from exc
        return sessions
