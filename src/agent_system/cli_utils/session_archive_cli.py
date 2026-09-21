"""The archive from the command line -- listing, archiving, restoring.

One place, so the CLI and the panel cannot drift apart: both call
``agent_system.services.session_archive.SessionArchive``, this module only
builds it the way a command-line process needs it and prints what it says.

What a CLI process cannot see is the API process's running jobs, so the busy
check here is the session PRESENCE lock -- the one guard that works across
processes. A sweep started here therefore leaves a session alone that another
process is running, which is the case that matters when a book is going.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20
_SID_WIDTH = 10
_TITLE_WIDTH = 34


def build_archive(session_manager: Any, config: Any) -> Any:
    """The archive service as a command-line process needs it."""
    from ..core.session_presence import presence_for
    from ..services.session_archive import SessionArchive

    settings = getattr(config, "session_archive", None)
    return SessionArchive(
        session_manager,
        archive_path=getattr(settings, "archive_path", None),
        retention_days=getattr(settings, "retention_days", 30),
        max_trees_per_sweep=getattr(settings, "max_trees_per_sweep", 200),
        presence=presence_for(config),
    )


def _short(text: str, width: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _day(stamp: str) -> str:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return "?"


async def print_archived(archive: Any, user_id: str, limit: int = DEFAULT_LIMIT) -> None:
    """One line per archived conversation, newest archive first."""
    entries = await archive.list_archived(user_id)
    if not entries:
        print(f"No archived sessions for {user_id}.")
        print(f"They move here after {archive.retention_days} days without a change.")
        return

    shown = entries if limit == 0 else entries[:limit]
    total_sessions = sum(entry.get("session_count", 0) for entry in entries)
    total_bytes = sum(entry.get("bytes", 0) for entry in entries)
    print(f"Archived conversations for {user_id} "
          f"({len(shown)} of {len(entries)}, {total_sessions} sessions, "
          f"{total_bytes / 1e6:.1f} MB)")
    print(f"{'ID':<{_SID_WIDTH}}  {'TITLE':<{_TITLE_WIDTH}}  {'SESS':>5}  "
          f"{'MB':>6}  {'LAST USED':<10}  {'ARCHIVED':<10}")
    for entry in shown:
        print(f"{_short(entry.get('session_id', ''), _SID_WIDTH):<{_SID_WIDTH}}  "
              f"{_short(entry.get('title', ''), _TITLE_WIDTH):<{_TITLE_WIDTH}}  "
              f"{entry.get('session_count', 0):>5}  "
              f"{entry.get('bytes', 0) / 1e6:>6.1f}  "
              f"{_day(entry.get('updated_at', '')):<10}  "
              f"{_day(entry.get('archived_at', '')):<10}")
    if limit and len(entries) > len(shown):
        print(f"... {len(entries) - len(shown)} more (--list-archived 0 for all)")
    print("Bring one back with: --restore-session <id>")


async def run_sweep(
    archive: Any,
    user_id: str,
    retention_days: Optional[int] = None,
    dry_run: bool = False,
) -> None:
    """Archive what is due for this user, and say what happened."""
    if not archive.has_busy_guard:
        # Neither guard: no job manager in a CLI process, and no lock files
        # either. Only the age limit stands between this and a session
        # somebody is using right now.
        print("Careful: session_presence is off, so nothing here can see a "
              "running session. Only the age limit protects them.")
    from ..services.session_archive import ArchiveError

    try:
        report = await archive.archive_user(
            user_id, dry_run=dry_run, retention_days=retention_days)
    except ArchiveError as error:
        # A refusal is an answer, not a crash: the API's own sweep may be
        # running, and a traceback would read as a broken command.
        print(f"Nothing done: {error}")
        return
    days = archive.retention_days if retention_days is None else retention_days
    what = "Would archive" if dry_run else "Archived"
    print(f"{what} {report.trees} conversation(s) with {report.sessions} sessions "
          f"for {user_id} (older than {days} days)")
    if report.trees and not dry_run:
        print(f"  {report.bytes_live / 1e6:.1f} MB live -> "
              f"{report.bytes_archived / 1e6:.1f} MB archived")
    if report.skipped_young or report.skipped_busy:
        print(f"  left alone: {report.skipped_young} not old enough, "
              f"{report.skipped_busy} in use")
    if report.capped:
        # Without this a capped pass reads as the whole job: it prints the 200
        # it did and nothing about the rest waiting behind them.
        print(f"  stopped at {archive.max_trees_per_sweep} per pass -- "
              f"{report.remaining} conversation(s) still waiting, run it again")
    for error in report.errors:
        print(f"  error: {error}")


async def run_restore(archive: Any, user_id: str, root_session_id: str) -> None:
    """Put one archived conversation back, and say where it landed."""
    from ..services.session_archive import ArchiveError

    try:
        result = await archive.restore(user_id, root_session_id)
    except ArchiveError as error:
        print(f"Not restored: {error}")
        return
    print(f"Restored '{result['title']}' ({result['restored']} sessions)")
    print(f"Continue it with: --session {root_session_id}")
