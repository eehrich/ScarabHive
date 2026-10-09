"""Old conversations move out of the live session store into zip archives.

``data/sessions`` grew without a bound: one book run leaves a root session and
several hundred sub-agent sessions behind, and nothing ever took them away.
Measured 20.09.2026 on this machine: 60.196 files, 5,1 GB, 59.960 of them
older than a month. Everything that walks the store pays for that -- a
directory listing, an index rebuild, the ``iterdir`` in a delete.

What moves is a whole TREE: a root session together with every sub-agent
session below it, because half a conversation is worth nothing. A tree moves
only when EVERY session in it is older than the retention and none of them is
running.

    data/session_archive/<user>/index.json          one entry per archived tree
    data/session_archive/<user>/<YYYY-MM>/<root>.zip

One zip per tree rather than one per month: the tree is the unit that is
archived, restored and deleted, so it is also the unit that is a file. That
keeps restore to a single open. An archive that is already there is never
replaced, only added to -- see ``ArchiveStore.write_zip``; a restore takes its
zip away, so a restored tree starts a fresh one.

Each zip carries its own ``_manifest.json``, so an archive stays readable and
the index can be rebuilt from the archives if it is ever lost.

The order is always: write the archive, verify it, and only then delete the
live files. A crash in between costs a duplicate, never a conversation.

This module is that policy -- which trees go, when, what must hold before a
live file is deleted -- and the walk over the live store that finds the trees.
The format on disk (paths, the manifest under its lock, writing and reading
the zips) is ``services/session_archive_store.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from filelock import Timeout as LockTimeout

from .session_archive_store import SWEEP_LOCK_NAME  # noqa: F401 - importable from here, as before the split
from .session_archive_store import ArchiveBusy, ArchiveError, ArchiveNotFound, ArchiveStore, _read_json
from .session_index import MAIN_INDEX, SUBS_PREFIX, SUBS_SUFFIX
from .session_paths import user_dir_of

logger = logging.getLogger(__name__)


@dataclass
class ArchiveReport:
    """What one sweep did for one user."""

    user_id: str
    dry_run: bool = False
    trees: int = 0
    sessions: int = 0
    bytes_live: int = 0
    bytes_archived: int = 0
    skipped_young: int = 0
    skipped_busy: int = 0
    capped: bool = False         # max_trees_per_sweep stopped this pass early
    remaining: int = 0           # old enough, still there: what the next pass finds
    roots: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "dry_run": self.dry_run,
            "trees": self.trees,
            "sessions": self.sessions,
            "bytes_live": self.bytes_live,
            "bytes_archived": self.bytes_archived,
            "skipped_young": self.skipped_young,
            "skipped_busy": self.skipped_busy,
            "capped": self.capped,
            "remaining": self.remaining,
            "roots": self.roots,
            "errors": self.errors,
        }


@dataclass
class _Tree:
    """A root session and its descendants, as they lie on disk right now."""

    root: str
    sessions: List[str]          # shallow first, root included
    files: Dict[str, Path]
    newest: float                # epoch seconds of the most recently touched session
    size: int                    # bytes of the live files
    root_meta: Dict[str, Any]    # index metadata of the root, for the manifest


def _parse_iso(value: Any) -> Optional[float]:
    """Epoch seconds of an ISO timestamp, or None if it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class SessionArchive:
    """Archives whole session trees and puts them back.

    The heavy work -- walking a user directory, zipping, unzipping -- runs in a
    worker thread. Every change to the live store goes through
    ``SessionManager`` (``delete_session`` / ``reinstate_session``) so its
    index partitions, its cache and its tombstones stay right; this class owns
    no invariant of the live store. What is written where in the archive, and
    under which lock, is its ``ArchiveStore``.
    """

    def __init__(
        self,
        session_manager: Any,
        *,
        archive_path: Optional[str | Path] = None,
        retention_days: int = 30,
        sweep_interval_hours: float = 24.0,
        first_sweep_delay_seconds: float = 300.0,
        max_trees_per_sweep: int = 0,
        presence: Any = None,
        busy_sessions: Optional[Callable[[], Awaitable[Set[str]]]] = None,
    ) -> None:
        self._session_manager = session_manager
        self.storage_path = Path(session_manager.storage_path)
        self.archive_path = (
            Path(archive_path) if archive_path
            else self.storage_path.parent / "session_archive"
        )
        self.retention_days = retention_days
        self.sweep_interval_hours = sweep_interval_hours
        self.first_sweep_delay_seconds = first_sweep_delay_seconds
        self.max_trees_per_sweep = max_trees_per_sweep
        self._presence = presence
        self._busy_sessions = busy_sessions
        # Where the archives are and how they are written: the format, under its own locks.
        self._store = ArchiveStore(self.archive_path)
        # One restore at a time: two of the same tree would both pass the
        # conflict check and then race on writing the same files.
        self._restore_lock = asyncio.Lock()
        logger.info(
            "SessionArchive initialized: archive_path=%s retention_days=%d",
            self.archive_path, self.retention_days,
        )

    # -- paths ---------------------------------------------------------------

    def _user_dir(self, user_id: str) -> Path:
        return user_dir_of(self.storage_path, user_id)

    @property
    def has_busy_guard(self) -> bool:
        """Whether anything here can tell a running session from an idle one.

        False means both are missing: no job manager (a command-line process
        has none) and no presence (``session_presence.enabled: false``). The
        age limit is then the only thing between the sweep and a session
        somebody is using, and a caller that can say so should.
        """
        return self._busy_sessions is not None or self._presence is not None

    def known_users(self) -> List[str]:
        """Every user directory in the live store."""
        try:
            return sorted(
                entry.name for entry in os.scandir(self.storage_path)
                if entry.is_dir() and not entry.name.startswith(".")
            )
        except OSError as exc:
            logger.warning("session archive: cannot list %s (%s)", self.storage_path, exc)
            return []

    # -- reading the live store ---------------------------------------------

    def _collect_trees(self, user_dir: Path) -> List[_Tree]:
        """Every session tree of one user, built from the index partitions.

        Structure comes from the ``.subs.<parent>.index.json`` partitions, the
        file list from one ``scandir`` -- no session file is opened, which is
        what keeps a sweep affordable on a store with 60k files. A session that
        no index knows becomes its own root, so nothing can hide from the
        sweep; the same for a child whose parent file is gone.
        """
        files: Dict[str, Path] = {}
        sizes: Dict[str, int] = {}
        mtimes: Dict[str, float] = {}
        sub_index_paths: List[Path] = []
        main_index_path: Optional[Path] = None

        try:
            entries = list(os.scandir(user_dir))
        except OSError as exc:
            logger.warning("session archive: cannot scan %s (%s)", user_dir, exc)
            return []

        for entry in entries:
            name = entry.name
            if not name.endswith(".json") or not entry.is_file():
                continue
            if name == MAIN_INDEX:
                main_index_path = Path(entry.path)
                continue
            if name.startswith(SUBS_PREFIX) and name.endswith(SUBS_SUFFIX):
                sub_index_paths.append(Path(entry.path))
                continue
            if name.startswith("."):
                continue  # .backup_* and whatever else hides there
            session_id = name[:-len(".json")]
            files[session_id] = Path(entry.path)
            try:
                stat = entry.stat()
            except OSError:
                continue
            sizes[session_id] = stat.st_size
            mtimes[session_id] = stat.st_mtime

        meta: Dict[str, Dict[str, Any]] = {}
        children: Dict[str, List[str]] = {}
        parent_of: Dict[str, str] = {}

        for path in sub_index_paths:
            parent = path.name[len(SUBS_PREFIX):-len(SUBS_SUFFIX)]
            for session_id, entry_meta in _read_json(path).items():
                if isinstance(entry_meta, dict):
                    meta[session_id] = entry_meta
                if session_id == parent:
                    continue  # its own parent: it would be in no tree at all
                if session_id in files and session_id not in parent_of:
                    parent_of[session_id] = parent
                    children.setdefault(parent, []).append(session_id)

        if main_index_path is not None:
            for session_id, entry_meta in _read_json(main_index_path).items():
                if isinstance(entry_meta, dict):
                    meta.setdefault(session_id, entry_meta)

        now = time.time()

        def age_of(session_id: str) -> float:
            recorded = _parse_iso(meta.get(session_id, {}).get("updated_at"))
            if recorded is not None:
                return recorded
            # No index row and no stat: treat it as touched right now. The
            # other default, epoch 0, would make a session nobody could read
            # the age of look older than everything and archive it first --
            # the wrong direction for the one operation here that deletes.
            return mtimes.get(session_id, now)

        roots = [
            session_id for session_id in files
            if parent_of.get(session_id) not in files
        ]

        trees: List[_Tree] = []
        claimed: Set[str] = set()
        for root in sorted(roots):
            ordered: List[str] = []
            queue = [root]
            while queue:
                current = queue.pop(0)
                if current in claimed:
                    continue  # a cycle, or a child listed under two parents
                claimed.add(current)
                ordered.append(current)
                queue.extend(child for child in children.get(current, []) if child not in claimed)
            trees.append(_Tree(
                root=root,
                sessions=ordered,
                files={session_id: files[session_id] for session_id in ordered},
                newest=max(age_of(session_id) for session_id in ordered),
                size=sum(sizes.get(session_id, 0) for session_id in ordered),
                root_meta=dict(meta.get(root, {})),
            ))
        return trees

    # -- sweeping ------------------------------------------------------------

    async def sweep(self, *, dry_run: bool = False) -> Dict[str, ArchiveReport]:
        """Archive what is due, for every user. One report per user."""
        reports: Dict[str, ArchiveReport] = {}
        for user_id in self.known_users():
            try:
                reports[user_id] = await self.archive_user(user_id, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - one user must not hide the rest
                logger.warning("session archive: user %s failed (%s)", user_id, exc)
                report = ArchiveReport(user_id=user_id, dry_run=dry_run)
                report.errors.append(str(exc))
                reports[user_id] = report
        return reports

    async def archive_user(
        self,
        user_id: str,
        *,
        dry_run: bool = False,
        retention_days: Optional[int] = None,
    ) -> ArchiveReport:
        """Archive every tree of one user whose sessions are all old enough."""
        days = self.retention_days if retention_days is None else retention_days
        report = ArchiveReport(user_id=user_id, dry_run=dry_run)
        if days < 1:
            report.errors.append(f"retention_days={days} would archive live sessions")
            return report

        user_dir = self._user_dir(user_id)
        if not user_dir.is_dir():
            return report
        if dry_run:
            # A dry run writes nothing, so it needs no lock -- and must not
            # take one: holding it for the minutes a big user takes to read
            # would refuse the timer's real pass as "already running".
            return await self._archive_user(report, user_dir, days, dry_run=True)

        # Refused, not queued: the caller asked for a pass NOW, and waiting out
        # the one already running would answer minutes later with a report
        # about somebody else's work.
        #
        # A file lock rather than an asyncio one, because the second pass is
        # usually not in this process: the API sweeps on a timer, and the CLI
        # sweeps when somebody runs it. Both write the same "<root>.zip.tmp"
        # and both register in the same index. An OS lock also answers the
        # case an in-process flag cannot -- a sweep whose process was killed
        # holds nothing, so the next one runs instead of waiting for a flag
        # nobody will ever clear. It covers this process too: two handles on
        # the same file conflict even when they belong to one process.
        lock = self._store.sweep_lock(user_id)
        try:
            lock.acquire(timeout=0)
        except LockTimeout as exc:
            raise ArchiveBusy(f"a sweep for {user_id} is already running") from exc
        try:
            return await self._archive_user(report, user_dir, days, dry_run=dry_run)
        finally:
            lock.release()

    async def _archive_user(
        self,
        report: ArchiveReport,
        user_dir: Path,
        days: int,
        *,
        dry_run: bool,
    ) -> ArchiveReport:
        user_id = report.user_id
        # Leftovers of a pass that was killed mid-write. `write_zip` clears
        # the one it is about to use, but a tree nobody archives again -- one
        # whose conversation was resumed -- would keep its .tmp for good.
        # Only the cold ones: the sweep lock above keeps out every process
        # that knows it, but a process still on the code from before it (an
        # API pulled without a restart) does not ask, and taking the file it
        # is writing right now would turn a leftover into a broken pass.
        # Not in a dry run: it reports, it does not clean up.
        if not dry_run:
            self._store.drop_leftovers(user_id)

        cutoff = time.time() - days * 86400
        trees = await asyncio.to_thread(self._collect_trees, user_dir)
        # BOTH guards are asked per tree, right before it is taken. A pass
        # runs for minutes, and a conversation somebody resumes in minute 12
        # must not be archived out from under its own run because it was
        # idle in minute 0 -- that holds for the running jobs and just as
        # much for the lock files, which in a CLI process are the only
        # guard there is, since no job manager answers there.

        # Counted over the WHOLE forest and before the loop, because the cap
        # below ends the pass early: raised inside the loop, this said how many
        # young trees THIS PASS happened to walk past before it stopped -- a
        # number that climbs every pass as the archivable trees ahead of them
        # go, while the log reads it out as "this many are too young".
        # Measured 20.09.2026 on cli_user, over an unchanged set: 328, 667, 970.
        # Age is free to decide (the walk already carries it); being in use is
        # not, so skipped_busy stays a count over the trees actually reached.
        eligible = [tree for tree in trees if tree.newest <= cutoff]
        report.skipped_young = len(trees) - len(eligible)

        for tree in eligible:
            # A cap bounds what one pass WRITES, and it is off by default:
            # the brief was "archive what is older than X", and a pass that
            # stops at 200 leaves the rest lying for a day -- a cleanup
            # turned into a drip. Nothing here needs bounding for the app's
            # sake either, because the zip work is already off the event
            # loop; what is left is contention on the session manager's lock
            # while the sessions are deleted, and that is the price of the
            # job. A positive value is for someone who wants a pass bounded
            # anyway. A dry run is never capped -- a report that stopped
            # counting would read as "that is all".
            #
            # Asked FIRST, ahead of the busy check: behind it, a pass whose
            # remaining trees are all in use skips every one of them and runs
            # off the end without ever setting `capped` -- a pass that wrote
            # its full quota with work left over, calling itself finished.
            # It also saves the job manager a question per tree whose answer
            # cannot change anything.
            if (not dry_run and self.max_trees_per_sweep
                    and report.trees >= self.max_trees_per_sweep):
                report.capped = True
                break
            held = await asyncio.to_thread(self._held_in, user_id, tree.sessions)
            if (held | await self._running()).intersection(tree.sessions):
                report.skipped_busy += 1
                continue
            try:
                archived_bytes, failures = await self._archive_tree(
                    user_id, tree, dry_run=dry_run)
            except ArchiveBusy as exc:
                # The index is held by another process. Every tree after this
                # one would wait the same timeout for the same lock, so the
                # pass stops -- and still returns what it did so far, which a
                # raise would throw away. Nothing of this tree was deleted:
                # the registration that failed comes before the first delete.
                logger.warning("session archive: pass for %s stopped (%s)", user_id, exc)
                report.errors.append(f"{tree.root}: {exc}")
                break
            except Exception as exc:  # noqa: BLE001 - one bad tree must not stop the sweep
                logger.exception("session archive: tree %s failed", tree.root)
                report.errors.append(f"{tree.root}: {exc}")
                continue
            report.errors.extend(failures)
            report.trees += 1
            report.sessions += len(tree.sessions)
            report.bytes_live += tree.size
            report.bytes_archived += archived_bytes
            report.roots.append(tree.root)

        # Old enough and still on disk: the cap's leftovers, the trees that were
        # in use, and any that failed. "More is left for the next pass" was true
        # and said nothing about how much.
        report.remaining = len(eligible) - report.trees

        if report.trees or report.errors:
            logger.info(
                "session archive%s: user=%s trees=%d sessions=%d %.1f MB -> %.1f MB "
                "(skipped: %d too young, %d busy, %d errors)",
                " (dry run)" if dry_run else "", user_id, report.trees, report.sessions,
                report.bytes_live / 1e6, report.bytes_archived / 1e6,
                report.skipped_young, report.skipped_busy, len(report.errors),
            )
        if report.capped:
            logger.info(
                "session archive: user=%s stopped at max_trees_per_sweep=%d, "
                "%d conversation(s) wait for the next pass",
                user_id, self.max_trees_per_sweep, report.remaining,
            )
        return report

    async def _running(self) -> Set[str]:
        """Sessions this process is running right now."""
        if self._busy_sessions is None:
            return set()
        try:
            return set(await self._busy_sessions())
        except Exception as exc:  # noqa: BLE001
            # Not knowing means not archiving -- a broken provider must not
            # silently turn the guard off, so say so and leave the user alone.
            logger.warning("session archive: busy check failed (%s)", exc)
            raise ArchiveError(f"cannot determine running sessions: {exc}") from exc

    def _held_in(self, user_id: str, session_ids: List[str]) -> Set[str]:
        """Which of THESE sessions a lock file speaks for, asked right now --
        the guard that crosses processes.

        The snapshot this replaces was taken once, before the whole pass.
        With no cap a pass runs for minutes, and in a CLI process -- which
        has no job manager, so ``_running`` is always empty -- it was the
        ONLY guard: a conversation opened in minute three could be
        archived in minute twelve.

        Per tree costs one ``exists`` per session instead of one scandir
        per pass; over a whole sweep that is the same number of files
        touched, and it is current. Presence off means no guard, and a
        presence that cannot answer stops nothing.

        The lock files are read directly rather than through
        ``SessionPresence.list_for_user``: that one leaves SUB-AGENT sessions
        out by design ("they belong to the run that spawned them"), and those
        are 59.970 of the 60.196 sessions here -- the guard would have missed
        almost everything it is for. ``get()`` does not filter.

        Unlike ``_running`` a failure here does NOT stop the sweep, and that
        asymmetry is deliberate: presence is optional (``session_presence.
        enabled: false`` is a supported setup, and then there are no lock files
        at all), so a sweep that refused to run without it would refuse on
        every machine that turned it off. The running-jobs check has no such
        "off" -- if it cannot answer, the answer is unknown, not empty.
        """
        presence = self._presence
        if presence is None:
            return set()
        user_dir = self._user_dir(user_id)
        held: Set[str] = set()
        for session_id in session_ids:
            if not (user_dir / f"{session_id}.lock").exists():
                continue
            try:
                state = presence.get(session_id, user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("session archive: presence check failed (%s)", exc)
                return set()
            # "idle" is a lock file nobody holds any more; anything else --
            # running, waking -- is a process that would lose its session.
            if state and state.get("status") != "idle":
                held.add(session_id)
        return held

    async def _archive_tree(
        self, user_id: str, tree: _Tree, *, dry_run: bool,
    ) -> tuple[int, List[str]]:
        """Zip one tree, verify it, register it, then take its files out.

        Returns the archive's size and the sessions that could not be deleted.
        """
        if dry_run:
            return 0, []

        # The month comes from the ROOT, not from `tree.newest`: a retry
        # after a partial delete sees a tree whose newest session is
        # already gone, works out an older month and writes a SECOND zip,
        # leaving the first one holding sessions nothing points at any
        # more. The root is deleted last and is what the tree is named
        # after, so its stamp is the one that does not move. And a tree
        # the manifest already knows keeps the path it was filed under.
        known = await self._store.entry_of(user_id, tree.root) or {}
        relative = known.get("archive") or f"{self._root_month(tree)}/{tree.root}.zip"
        target = self._store.user_dir(user_id) / relative
        entry = self._manifest_entry(user_id, tree, relative)

        archived_bytes, session_count = await asyncio.to_thread(
            self._store.write_zip, target, tree.sessions, tree.files, entry)
        entry["bytes"] = archived_bytes
        entry["session_count"] = session_count

        # Registered BEFORE the first delete: from here on the archive is the
        # only copy of what the loop below removes, and a verified archive that
        # no index knows would be lost by the next pass overwriting it.
        await self._store.remember(user_id, tree.root, entry)

        user_dir = self._user_dir(user_id)
        # The tree's index partitions go before its sessions do. Each of them
        # describes only sessions that are leaving, and letting delete_session
        # shrink a 300-entry partition once per child is O(n^2) atomic writes
        # to a file that ends up deleted anyway -- measured 20.09.2026 on a
        # 296-session tree of 26 MB: 9.2 s with the churn, 5.4 s without it.
        # SessionIndex.remove_entry then finds no partition and returns.
        for session_id in tree.sessions:
            try:
                (user_dir / f"{SUBS_PREFIX}{session_id}{SUBS_SUFFIX}").unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("session archive: partition of %s (%s)", session_id, exc)

        failures: List[str] = []
        # Deepest first; the root leaves last, so a failed delete leaves ONE
        # tree behind rather than a handful of orphan roots.
        for session_id in reversed(tree.sessions):
            try:
                await self._session_manager.delete_session(
                    user_id, session_id, create_backup=False)
            except Exception as exc:  # noqa: BLE001 - the archive already has it
                failures.append(f"{session_id}: {exc}")
                continue
            # Its presence files (core/session_presence.py) went nowhere else. Not .stopped: the user's stop
            # outlives the archive, and a restored session still waits for them to start it.
            for suffix in (".lock", ".pending", ".woken"):
                try:
                    (user_dir / f"{session_id}{suffix}").unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug("session archive: stale %s of %s (%s)", suffix, session_id, exc)
        if failures:
            # The partitions went first, for the churn. With a session left
            # behind, its row went with them and nothing lists it as a child
            # any more -- it would show up as a root of its own. Rebuilding
            # the partition is the repair path kept for exactly this; it is
            # not cheap, and a failed delete is not common.
            try:
                await self._session_manager._rebuild_index(user_id, tree.root)
            except Exception as exc:  # noqa: BLE001 - the archive is safe either way
                logger.warning(
                    "session archive: could not rebuild the index of %s (%s)",
                    tree.root, exc)
            logger.warning(
                "session archive: %s is archived, but %d of its files could not be "
                "deleted -- the next pass will add whatever is left to the same "
                "archive: %s", tree.root, len(failures), failures[:3],
            )
        return archived_bytes, failures

    @staticmethod
    def _root_month(tree: _Tree) -> str:
        """The month a tree is filed under: the root's own timestamp."""
        stamp = _parse_iso(tree.root_meta.get("updated_at"))
        if stamp is None:
            root_file = tree.files.get(tree.root)
            stamp = (root_file.stat().st_mtime
                     if root_file and root_file.exists() else tree.newest)
        return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m")

    def _manifest_entry(self, user_id: str, tree: _Tree, relative: str) -> Dict[str, Any]:
        meta = tree.root_meta
        return {
            "session_id": tree.root,
            "user_id": user_id,
            "title": meta.get("title") or tree.root,
            "agent_name": meta.get("agent_name", ""),
            "llm_profile": meta.get("llm_profile", ""),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get(
                "updated_at",
                datetime.fromtimestamp(tree.newest, timezone.utc).isoformat(),
            ),
            "message_count": meta.get("message_count", 0),
            "session_count": len(tree.sessions),
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "archive": relative,
            "bytes_live": tree.size,
            "bytes": 0,
        }

    # -- listing, restoring, forgetting --------------------------------------

    async def list_archived(self, user_id: str) -> List[Dict[str, Any]]:
        """Every archived tree of a user, newest archive first."""
        manifest = await self._store.manifest(user_id)
        entries = [entry for entry in manifest.values() if isinstance(entry, dict)]
        entries.sort(key=lambda item: item.get("archived_at", ""), reverse=True)
        return entries

    async def restore(self, user_id: str, root_session_id: str) -> Dict[str, Any]:
        """Put an archived tree back into the live store, timestamps and all.

        Refuses rather than overwrites when a session of the tree is live again
        as something ELSE -- that is corruption, not a duplicate, and the
        caller has to look at it. A session that is live as the very copy this
        archive holds is a restore that stopped halfway and is simply skipped,
        so a second attempt finishes the job instead of refusing forever.
        """
        async with self._restore_lock:
            return await self._restore(user_id, root_session_id)

    async def _restore(self, user_id: str, root_session_id: str) -> Dict[str, Any]:
        manifest = await self._store.manifest(user_id)
        entry = manifest.get(root_session_id)
        if not isinstance(entry, dict):
            raise ArchiveNotFound(
                f"No archived session {root_session_id} for user {user_id}")

        zip_path = self._store.archive_file(user_id, entry.get("archive", ""))
        sessions = await asyncio.to_thread(self._store.read_zip_sessions, zip_path)
        if not sessions:
            raise ArchiveError(f"Archive {zip_path.name} holds no sessions")

        strangers = [
            data["session_id"] for data in sessions
            if data.get("user_id") != user_id
        ]
        if strangers:
            # reinstate_session writes to the user_id INSIDE the document.
            raise ArchiveError(
                f"{len(strangers)} session(s) in this archive belong to someone "
                f"else ({', '.join(strangers[:3])}) -- not restoring it"
            )

        conflicts, done, taken = [], [], []
        for data in sessions:
            if not self._session_manager.belongs_to(user_id, data["session_id"]):
                # Another user's now (a custom id, their own archive restored first):
                # reinstate_session refuses it -- asked here, before the tree is half back.
                if self._session_manager._session_id_exists_globally(data["session_id"]):
                    taken.append(data["session_id"])
                continue
            if await self._is_the_same_session(user_id, data):
                done.append(data["session_id"])
            else:
                conflicts.append(data["session_id"])
        if conflicts:
            raise ArchiveError(
                f"{len(conflicts)} session(s) of this tree are live again "
                f"({', '.join(conflicts[:3])}) -- refusing to overwrite them"
            )
        if taken:
            raise ArchiveError(
                f"{len(taken)} session id(s) of this tree are another user's now "
                f"({', '.join(taken[:3])}) -- not restoring it"
            )

        for data in sessions:
            if data["session_id"] in done:
                continue
            try:
                await self._session_manager.reinstate_session(data)
            except ValueError as exc:
                # A readable answer for the panel instead of a 500.
                raise ArchiveError(
                    f"Could not restore {data['session_id']}: {exc}") from exc

        await self._store.edit_manifest(
            user_id, lambda manifest: manifest.pop(root_session_id, None))
        zip_path.unlink(missing_ok=True)

        logger.info(
            "session archive: restored %s for user %s (%d sessions, %d were already back)",
            root_session_id, user_id, len(sessions), len(done),
        )
        return {
            "session_id": root_session_id,
            "restored": len(sessions),
            "title": entry.get("title", ""),
        }

    async def _is_the_same_session(self, user_id: str, data: Dict[str, Any]) -> bool:
        """Whether the live session under this id IS the archived one.

        Compared on ``updated_at``, which ``reinstate_session`` keeps verbatim:
        a live copy carrying the archive's own timestamp can only have come
        from an earlier run of this restore.
        """
        try:
            live = await self._session_manager.load_session(
                user_id, data["session_id"], bypass_cache=True)
        except Exception as exc:  # noqa: BLE001 - unreadable is not "the same"
            logger.debug("session archive: cannot read live %s (%s)",
                         data["session_id"], exc)
            return False
        return bool(live.get("updated_at")) and live["updated_at"] == data.get("updated_at")

    async def forget(self, user_id: str, root_session_id: str) -> Dict[str, Any]:
        """Delete an archived tree for good. There is no copy after this."""
        def take_it_out(manifest: Dict[str, Any]) -> Dict[str, Any]:
            entry = manifest.pop(root_session_id, None)
            if not isinstance(entry, dict):
                # Raised inside the edit, so nothing is written back.
                raise ArchiveNotFound(
                    f"No archived session {root_session_id} for user {user_id}")
            return entry

        entry = await self._store.edit_manifest(user_id, take_it_out)
        try:
            self._store.archive_file(user_id, entry.get("archive", "")).unlink(missing_ok=True)
        except (ArchiveNotFound, OSError) as exc:
            # The entry is out of the manifest either way -- say what is left.
            logger.warning(
                "session archive: %s removed from the index, but its file was not "
                "deleted (%s)", root_session_id, exc,
            )
        logger.info("session archive: forgot %s for user %s", root_session_id, user_id)
        return {"session_id": root_session_id, "sessions": entry.get("session_count", 0)}

    # -- the periodic sweep --------------------------------------------------

    async def sweep_loop(self) -> None:
        """Sweep every ``sweep_interval_hours``, starting after a settling delay."""
        try:
            await asyncio.sleep(self.first_sweep_delay_seconds)
            while True:
                try:
                    await self.sweep()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("session archive sweep failed: %s", exc, exc_info=True)
                await asyncio.sleep(self.sweep_interval_hours * 3600)
        except asyncio.CancelledError:
            logger.debug("session archive sweep loop cancelled")
