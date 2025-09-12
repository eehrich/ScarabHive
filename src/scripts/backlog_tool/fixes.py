"""Auto-fix and normalization functionality for backlog maintenance.

This module contains functions for automatically fixing common issues
and normalizing backlog data.
"""
import re
from datetime import datetime
from typing import Any, Dict, Tuple, cast, Optional

from .models import Backlog
from . import values


def reassign_duplicate_task_ids(backlog: Backlog) -> list[Tuple[str, str]]:
    """Find duplicate task ids and reassign new unique ids.

    Returns list of tuples (old_id, new_id) for changed tasks.
    """
    # Build a list of all task ids and detect duplicates among tasks only
    all_task_ids: list[str] = []
    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.tasks:
            all_task_ids.append(t.id)
    dup = {i for i in all_task_ids if all_task_ids.count(i) > 1}
    changed: list[Tuple[str, str]] = []
    if not dup:
        return changed

    # existing pool must include epic ids as well to avoid collisions
    existing = {e.id for e in backlog.epics_open + backlog.epics_finished} | set(all_task_ids)

    # choose start = max numeric existing id + 1 for monotonic allocation
    # Only consider numeric ids that are 4 digits or less (canonical backlog ids).
    # Migration artifacts or auxiliary ids may be longer (e.g. '900100') and
    # should not be used to compute the next 4-digit id — they would push the
    # starting point beyond the 4-digit range and make allocation fail.
    numeric_existing = [int(x) for x in existing if x.isdigit() and len(x) <= 4]
    start = (max(numeric_existing) + 1) if numeric_existing else 0

    def next_id(start_idx=start):
        for i in range(start_idx, 10000):
            cand = f"{i:04d}"
            if cand not in existing:
                existing.add(cand)
                return cand
        raise RuntimeError("no available task ids")

    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.tasks:
            if t.id in dup:
                newid = next_id()
                changed.append((t.id, newid))
                t.id = newid
    return changed


def reassign_epic_task_collisions(backlog: Backlog) -> list[Tuple[str, str]]:
    """Detect ids used both for epics and tasks and reassign task ids to unique values.

    Returns list of (old_task_id, new_task_id).
    """
    changes: list[Tuple[str, str]] = []
    epic_ids = {e.id for e in backlog.epics_open + backlog.epics_finished}
    task_ids = [t.id for e in backlog.epics_open + backlog.epics_finished for t in e.tasks]
    collisions = {tid for tid in task_ids if tid in epic_ids}
    if not collisions:
        return changes

    existing = set(epic_ids) | set(task_ids)

    def next_id(start=0):
        for i in range(start, 10000):
            cand = f"{i:04d}"
            if cand not in existing:
                existing.add(cand)
                return cand
        raise RuntimeError("no available ids")

    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.tasks:
            if t.id in collisions:
                new = next_id()
                changes.append((t.id, new))
                t.id = new
    return changes


def normalize_backlog_format(backlog: Backlog) -> list[str]:
    """Normalize status tokens and empty/placeholder dates; returns list of change descriptions."""
    changes: list[str] = []
    for e in backlog.epics_open + backlog.epics_finished:
        # normalize epic status word
        if e.status:
            norm = e.status.strip()
            n = norm.lower()
            # map symbols or words
            mapped = None
            sym_map = cast(Dict[str, Any], values.get('symbol_map', {}) or {})
            for k, v in sym_map.items():
                if k == norm or k == norm.strip():
                    mapped = v
                    break
            if not mapped:
                word_map = cast(Dict[str, str], values.get('word_map', {}) or {})
                mapped = word_map.get(n, n)
            if mapped != e.status:
                changes.append(f"epic {e.id} status: {e.status} -> {mapped}")
                e.status = mapped
    for t in e.tasks:
            if t.status:
                n = t.status.strip().lower()
                word_map = cast(Dict[str, str], values.get('word_map', {}) or {})
                mapped = word_map.get(n, t.status)
                if mapped != t.status:
                    changes.append(f"task {t.id} status: {t.status} -> {mapped}")
                    t.status = mapped
            # normalize placeholder closed dates like em-dash '—' or dash
            if t.closed and t.closed.strip() in ('—', '-', '—'):
                changes.append(f"task {t.id} closed: {t.closed} -> ''")
                t.closed = None
    return changes


def auto_fix_date_formats(backlog: Backlog) -> list[str]:
    """Auto-fix common date format issues by converting to ISO format.

    Handles formats like:
    - MM/DD/YYYY -> YYYY-MM-DD
    - DD-MM-YYYY -> YYYY-MM-DD
    - YYYY/MM/DD -> YYYY-MM-DD
    - Month DD, YYYY -> YYYY-MM-DD

    Returns list of change descriptions.
    """
    changes: list[str] = []

    def parse_and_convert_date(date_str: str) -> Optional[str]:
        """Try to parse various date formats and return ISO format."""
        date_str = date_str.strip()

        # Already ISO format
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            return None

        # MM/DD/YYYY
        match = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', date_str)
        if match:
            month, day, year = match.groups()
            try:
                # Validate the date
                datetime(int(year), int(month), int(day))
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            except ValueError:
                return None

        # DD-MM-YYYY
        match = re.match(r'^(\d{1,2})-(\d{1,2})-(\d{4})$', date_str)
        if match:
            day, month, year = match.groups()
            try:
                datetime(int(year), int(month), int(day))
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            except ValueError:
                return None

        # YYYY/MM/DD
        match = re.match(r'^(\d{4})/(\d{1,2})/(\d{1,2})$', date_str)
        if match:
            year, month, day = match.groups()
            try:
                datetime(int(year), int(month), int(day))
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            except ValueError:
                return None

        # Try to parse with dateutil if available (more flexible)
        try:
            import dateutil.parser
            parsed = dateutil.parser.parse(date_str)
            return parsed.date().isoformat()
        except (ImportError, ValueError):
            pass

        return None

    for e in backlog.epics_open + backlog.epics_finished:
        if e.added:
            converted = parse_and_convert_date(e.added)
            if converted and converted != e.added:
                changes.append(f"epic {e.id} added date: {e.added} -> {converted}")
                e.added = converted

        if e.closed:
            converted = parse_and_convert_date(e.closed)
            if converted and converted != e.closed:
                changes.append(f"epic {e.id} closed date: {e.closed} -> {converted}")
                e.closed = converted

        for t in e.tasks:
            if t.added:
                converted = parse_and_convert_date(t.added)
                if converted and converted != t.added:
                    changes.append(f"task {t.id} added date: {t.added} -> {converted}")
                    t.added = converted

            if t.closed:
                converted = parse_and_convert_date(t.closed)
                if converted and converted != t.closed:
                    changes.append(f"task {t.id} closed date: {t.closed} -> {converted}")
                    t.closed = converted

    return changes


def auto_fix_id_formats(backlog: Backlog) -> list[str]:
    """Auto-fix ID format issues by converting non-4-digit numeric IDs to 4-digit format.

    Returns list of change descriptions.
    """
    changes: list[str] = []

    # Collect all existing IDs to avoid collisions
    existing_ids = {e.id for e in backlog.epics_open + backlog.epics_finished}
    for e in backlog.epics_open + backlog.epics_finished:
        for t in e.tasks:
            existing_ids.add(t.id)

    def normalize_id(id_str: str) -> Optional[str]:
        """Convert numeric ID to 4-digit format if needed."""
        if not id_str or not id_str.isdigit():
            return None
        if len(id_str) == 4:
            return None  # Already correct
        if len(id_str) > 4:
            return None  # Don't auto-fix long IDs

        # Pad with zeros
        normalized = id_str.zfill(4)

        # Check for collision
        if normalized in existing_ids and normalized != id_str:
            return None  # Can't fix due to collision

        return normalized

    for e in backlog.epics_open + backlog.epics_finished:
        normalized = normalize_id(e.id)
        if normalized and normalized != e.id:
            changes.append(f"epic id format: {e.id} -> {normalized}")
            existing_ids.remove(e.id)
            existing_ids.add(normalized)
            e.id = normalized

        for t in e.tasks:
            normalized = normalize_id(t.id)
            if normalized and normalized != t.id:
                changes.append(f"task id format: {t.id} -> {normalized}")
                existing_ids.remove(t.id)
                existing_ids.add(normalized)
                t.id = normalized

    return changes


def auto_complete_epics(backlog: Backlog) -> list[str]:
    """Auto-complete epics that have all tasks finished but epic not marked as finished.

    Returns list of change descriptions.
    """
    changes: list[str] = []

    finish_list = set(values.get('finish_statuses', ["done", "closed", "complete", "finished"]))

    for e in backlog.epics_open + backlog.epics_finished:
        if not getattr(e, 'tasks', None) or not e.tasks:
            continue

        # Check if all tasks are finished
        all_finished = True
        for t in e.tasks:
            st = (t.status or '').strip().lower()
            if st not in finish_list:
                all_finished = False
                break

        if all_finished:
            est = (e.status or '').strip().lower()
            if est not in finish_list:
                # Auto-complete the epic
                new_status = 'done'  # Default completion status
                changes.append(f"epic {e.id} auto-completed: {e.status or 'open'} -> {new_status}")
                e.status = new_status

                # Move to finished epics if it's currently in open
                if e in backlog.epics_open:
                    backlog.epics_open.remove(e)
                    backlog.epics_finished.append(e)
                    changes.append(f"epic {e.id} moved to finished section")

    return changes
